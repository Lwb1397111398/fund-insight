"""
板块映射保存 API 回归测试

背景：create_sector_mapping 内曾出现函数内局部 `from src.models.database import
SectorFundMapping`，导致整个函数里的 SectorFundMapping 都变成局部变量，
保存内置映射时抛 UnboundLocalError（cannot access local variable ...）。
"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_db
from src.api.routes.config import create_sector_mapping
from src.models.database import Base


AUTH_HEADERS = {"X-Access-Password": "sector-mapping-api-test"}


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'sector-mapping-api.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _client(monkeypatch, session_factory):
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    from src.api.main import app

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return app, TestClient(app)


def test_create_endpoint_has_no_local_sectorfundmapping_import():
    """函数内局部 import 会让 SectorFundMapping 变成局部变量，必须防回归"""
    assert 'SectorFundMapping' not in create_sector_mapping.__code__.co_varnames


def test_create_sector_mapping_succeeds(monkeypatch, tmp_path):
    """POST 创建新板块映射应成功（此前该路径必抛 UnboundLocalError）"""
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        sector = "__回归测试板块__"
        res = client.post(
            "/api/config/sector-mappings",
            json={"sector_name": sector, "fund_code": "512480", "fund_name": "半导体ETF"},
            headers=AUTH_HEADERS,
        )
        body = res.json()
        assert res.status_code == 200
        assert body["success"] is True, body.get("message")
        assert body["data"]["sector_name"] == sector
        assert body["data"]["fund_code"] == "512480"

        # 同板块再次保存（不同基金）：走"已存在转更新"分支，同样不应报错
        res2 = client.post(
            "/api/config/sector-mappings",
            json={"sector_name": sector, "fund_code": "510300", "fund_name": "沪深300ETF"},
            headers=AUTH_HEADERS,
        )
        body2 = res2.json()
        assert res2.status_code == 200
        assert body2["success"] is True, body2.get("message")
        assert body2["data"]["fund_code"] == "510300"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_save_sector_with_inactive_duplicate_stays_visible(monkeypatch, tmp_path):
    """同板块存在历史遗留的 inactive 记录时，保存不能把 active 记录弄丢

    场景：级联清理等原因留下同板块一 active 一 inactive 两条记录，
    用户再次保存该板块。若 existing 查询不按 active 优先、且更新不恢复
    is_active，会把 inactive 行更新、把 active 行级联停用，导致该板块
    从列表里凭空消失（用户视角=保存丢失）。
    """
    from src.models.database import SectorFundMapping

    session_factory = _database(tmp_path)
    seed_db = session_factory()
    # 先插 inactive（更小的 id），再插 active，模拟历史遗留
    inactive = SectorFundMapping(
        sector_name="__遗留板块__", fund_code="111111", fund_name="遗留记录",
        is_active=False, reviewed=True,
    )
    seed_db.add(inactive)
    seed_db.flush()
    active = SectorFundMapping(
        sector_name="__遗留板块__", fund_code="222222", fund_name="现行记录",
        is_active=True, reviewed=True,
    )
    seed_db.add(active)
    seed_db.commit()
    seed_db.close()

    app, client = _client(monkeypatch, session_factory)
    try:
        res = client.post(
            "/api/config/sector-mappings",
            json={"sector_name": "__遗留板块__", "fund_code": "333333", "fund_name": "新基金"},
            headers=AUTH_HEADERS,
        ).json()
        assert res["success"] is True, res.get("message")

        listed = client.get("/api/config/sector-mappings", headers=AUTH_HEADERS).json()
        rows = [m for m in listed["data"]["mappings"] if m["sector_name"] == "__遗留板块__"]
        assert len(rows) == 1, f"该板块应只剩 1 条可见映射，实际 {len(rows)}"
        assert rows[0]["fund_code"] == "333333", "保存后的新基金代码应可见"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_save_mapping_auto_creates_missing_fund_info(monkeypatch, tmp_path):
    """基金代码不在 fund_info 时保存映射应自动补档案，而不是 FK 报错

    sector_fund_mapping.fund_code 有外键指向 fund_info.fund_code，
    生产 SQLite/PostgreSQL 均开启外键约束，缺档案会直接保存失败。
    """
    from src.models.database import FundInfo

    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        res = client.post(
            "/api/config/sector-mappings",
            json={"sector_name": "__外键测试板块__", "fund_code": "888888", "fund_name": "新基金888888"},
            headers=AUTH_HEADERS,
        ).json()
        assert res["success"] is True, res.get("message")

        db = session_factory()
        try:
            row = db.query(FundInfo).filter(FundInfo.fund_code == "888888").first()
            assert row is not None, "fund_info 应自动创建 888888 的最小档案"
            assert row.fund_name == "新基金888888"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_update_sector_mapping_succeeds(monkeypatch, tmp_path):
    """PUT 更新已有 DB 映射应成功并标记已审查"""
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        sector = "__回归测试板块2__"
        created = client.post(
            "/api/config/sector-mappings",
            json={"sector_name": sector, "fund_code": "512480", "fund_name": "半导体ETF"},
            headers=AUTH_HEADERS,
        ).json()
        assert created["success"] is True, created.get("message")
        mapping_id = created["data"]["id"]

        res = client.put(
            f"/api/config/sector-mappings/{mapping_id}",
            json={"fund_code": "510050", "fund_name": "50ETF"},
            headers=AUTH_HEADERS,
        )
        body = res.json()
        assert res.status_code == 200
        assert body["success"] is True, body.get("message")
        assert body["data"]["fund_code"] == "510050"
    finally:
        app.dependency_overrides.pop(get_db, None)


# ===== AI 匹配接口（板块→基金 agent） =====

def _fake_decision(sector, code='159995', name='芯片ETF', status='matched', conf=0.92):
    from src.services.sector_fund_agent import FundCandidate, SectorDecision
    cand = FundCandidate(code=code, name=name, source='llm', official_name=name,
                         t3_suitable=True, t3_proxy=False, t3_score=95,
                         confidence=conf, verify={'is_strict_ok': True}, kind='etf')
    return SectorDecision(sector=sector, chosen=cand, status=status, confidence=conf,
                          rounds=1, evidence=[
                              {'stage': 'T1', 'candidates': [code]},
                              {'stage': 'T2', 'code': code, 'verdict': 'pass'},
                              {'stage': 'T3', 'code': code, 'suitable': True, 'score': 95}])


def test_ai_match_preview_does_not_write(monkeypatch, tmp_path):
    """apply=false 只返回证据，绝不写库——老板要能先看再决定。"""
    from src.models.database import SectorFundMapping
    from src.services import sector_fund_agent as agent_mod

    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    monkeypatch.setattr(agent_mod, 'resolve_sector_fund',
                        lambda sector, **kw: _fake_decision(sector))
    captured = {}
    monkeypatch.setattr(agent_mod, 'apply_decision',
                        lambda db, decision, **kw: captured.setdefault('called', True))
    try:
        res = client.post('/api/config/sector-mappings/ai-match',
                          json={'sector_name': 'AI测试板块', 'apply': False},
                          headers=AUTH_HEADERS)
        body = res.json()
        assert res.status_code == 200
        assert body['data']['decision']['chosen']['code'] == '159995'
        assert body['data']['apply']['applied'] is False
        assert 'called' not in captured
        db = session_factory()
        try:
            assert db.query(SectorFundMapping).count() == 0
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()


def test_ai_match_apply_writes_evidence(monkeypatch, tmp_path):
    """apply=true 落库带 match_source/confidence/verified_at，供审查门禁复核。"""
    from src.models.database import FundInfo, SectorFundMapping
    from src.services import sector_fund_agent as agent_mod

    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    monkeypatch.setattr(agent_mod, 'resolve_sector_fund',
                        lambda sector, **kw: _fake_decision(sector))
    try:
        seed = session_factory()
        seed.add(FundInfo(fund_code='159995', fund_name='芯片ETF'))
        seed.commit()
        seed.close()

        res = client.post('/api/config/sector-mappings/ai-match',
                          json={'sector_name': 'AI写入板块', 'apply': True},
                          headers=AUTH_HEADERS)
        assert res.status_code == 200, res.text
        assert res.json()['data']['apply']['applied'] is True
        db = session_factory()
        try:
            row = db.query(SectorFundMapping).filter_by(sector_name='AI写入板块').first()
            assert row is not None and row.match_source == 'agent'
            assert row.confidence and row.confidence > 0.8
            assert row.verified_at is not None
            assert row.reviewed is True   # 证据齐才允许自动已审查
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()


def test_batch_review_refuses_rows_without_evidence(monkeypatch, tmp_path):
    """一键"全部标记已审查"不得把没证据的行变成已审查（门禁唯一护栏）。"""
    from src.models.database import FundInfo, SectorFundMapping
    from src.services.sector_fund_service import SectorFundService

    session_factory = _database(tmp_path)
    db = session_factory()
    db.add(FundInfo(fund_code='512480', fund_name='半导体ETF'))
    db.add(SectorFundMapping(sector_name='无证据板块', fund_code='512480',
                             fund_name='半导体ETF', reviewed=False, is_active=True))
    db.commit()
    mapping_id = db.query(SectorFundMapping).first().id
    db.close()

    db_for_service = session_factory()
    service = SectorFundService(db_for_service)
    assert service.batch_mark_reviewed([mapping_id], reviewed=True) == 0
    check = session_factory()
    try:
        assert check.query(SectorFundMapping).get(mapping_id).reviewed is False
    finally:
        check.close()
    assert service.batch_mark_reviewed([mapping_id], reviewed=True,
                                       owner_confirm=True) == 1
    check = session_factory()
    try:
        row = check.query(SectorFundMapping).get(mapping_id)
        assert row.reviewed is True and row.owner_locked is True
        assert row.reviewed_by == 'owner'
    finally:
        check.close()


def test_fund_search_endpoint(monkeypatch, tmp_path):
    from src.fund.fund_api import fund_api as api_instance

    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    monkeypatch.setattr(api_instance, 'search_fund',
                        lambda kw: [{'fund_code': '159995', 'fund_name': '芯片ETF',
                                     'fund_type': ''}])
    try:
        res = client.get('/api/config/fund-search', params={'keyword': '芯片'},
                         headers=AUTH_HEADERS)
        assert res.status_code == 200
        assert res.json()['data'][0]['fund_code'] == '159995'
        assert client.get('/api/config/fund-search', params={'keyword': '  '},
                          headers=AUTH_HEADERS).status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_ai_endpoints_require_password(tmp_path):
    session_factory = _database(tmp_path)
    app, client = _client(__import__('pytest').MonkeyPatch(), session_factory) \
        if False else (None, None)
    from src.api.main import app as real_app
    from src.api.deps import get_db as _get_db
    from fastapi.testclient import TestClient as _TC

    def override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    real_app.dependency_overrides[_get_db] = override
    try:
        monkeyenv = {'ACCESS_PASSWORD': 'secteur-nopass'}
        import os
        old = os.environ.get('ACCESS_PASSWORD')
        os.environ['ACCESS_PASSWORD'] = 'secteur-nopass'
        client = _TC(real_app)
        try:
            assert client.post('/api/config/sector-mappings/ai-match',
                               json={'sector_name': 'x'}).status_code == 401
            assert client.get('/api/config/sector-mappings/ai-batch/status').status_code == 401
        finally:
            os.environ.pop('ACCESS_PASSWORD', None)
            if old is not None:
                os.environ['ACCESS_PASSWORD'] = old
    finally:
        real_app.dependency_overrides.clear()


def test_access_password_fail_closed_when_unset(monkeypatch, tmp_path):
    """ACCESS_PASSWORD 不再有硬编码默认值：未配置时 /api/ 必须拒绝服务而不是放行。"""
    import os
    from fastapi.testclient import TestClient
    from src.api.main import app

    session_factory = _database(tmp_path)

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    monkeypatch.delenv("ACCESS_PASSWORD", raising=False)
    try:
        client = TestClient(app)
        res = client.get("/api/config/sector-mappings")
        assert res.status_code == 503
        assert "ACCESS_PASSWORD" in res.text
    finally:
        app.dependency_overrides.clear()


def test_source_has_no_hardcoded_password():
    """推送 GitHub 前的硬门禁：真实口令不得出现在**被 git 跟踪**的文件里。

    用 `git grep` 而不是遍历文件系统：仓库里可能有别的 worktree/临时目录，
    那些不属于要推送的内容，扫到只会产生假警报。
    """
    import io
    import os
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    secret = ''
    env_path = os.path.join(root, '.env')
    if os.path.exists(env_path):
        for line in io.open(env_path, encoding='utf-8', errors='replace'):
            if line.strip().startswith('ACCESS_PASSWORD='):
                secret = line.split('=', 1)[1].strip()
                break
    assert secret, '本地 .env 缺少 ACCESS_PASSWORD，无法执行口令泄漏检查'

    out = subprocess.run(['git', 'grep', '-l', '-F', secret], cwd=root,
                         capture_output=True, text=True, encoding='utf-8',
                         errors='replace')
    assert out.stdout.strip() == '', '被跟踪文件里仍残留真实口令：%s' % out.stdout.strip()


def test_source_secret_not_in_planned_docs():
    """计划文档里也不许写出真实口令（本轮就发生过一次）。"""
    import io
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    secret = ''
    env_path = os.path.join(root, '.env')
    if os.path.exists(env_path):
        for line in io.open(env_path, encoding='utf-8', errors='replace'):
            if line.strip().startswith('ACCESS_PASSWORD='):
                secret = line.split('=', 1)[1].strip()
                break
    hits = []
    for base, dirs, files in os.walk(os.path.join(root, 'docs')):
        for name in files:
            if not name.endswith('.md'):
                continue
            path = os.path.join(base, name)
            text = io.open(path, encoding='utf-8', errors='replace').read()
            if secret and secret in text:
                hits.append(os.path.relpath(path, root))
    assert hits == [], '文档里泄漏了真实口令：%s' % hits
