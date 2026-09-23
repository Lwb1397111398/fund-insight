"""
板块映射保存 API 回归测试

背景：create_sector_mapping 内曾出现函数内局部 `from src.models.database import
SectorFundMapping`，导致整个函数里的 SectorFundMapping 都变成局部变量，
保存内置映射时抛 UnboundLocalError（cannot access local variable ...）。
"""
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_db
from src.api.routes.config import create_sector_mapping
from src.models.database import Base


AUTH_HEADERS = {"X-Access-Password": "sector-mapping-api-test"}


@pytest.fixture(autouse=True)
def _probe_stubbed_as_no_conclusion(monkeypatch):
    """保存路径上的身份探针钉成"没结论"＝旧行为，但不许真打站。

    第 19 轮 MAJOR-3：以前有 3 条用例是真打了站、被 `_manual_identity_verdict` 的
    `except Exception` 吞成 unknown 才过的。守卫换成 BaseException 之后信号不再被吞，
    桩就得显式给：本文件测的是"保存/覆盖/补档案"，不是探针判定。
    """
    from src.services import sector_identity_audit as audit

    def fake(code, stored, sector='', **kw):
        return {'verdict': 'unknown', 'code': code, 'stored_name': stored,
                'official_name': None, 'jaccard': 0.0, 'status': 'ok',
                'suggested_code': None, 'suggested_name': None,
                'reason': '桩：本文件不测探针', 'evidence': {}}
    monkeypatch.setattr(audit, 'arbitrate_mapping', fake)


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
                          json={'sector_name': 'AI写入板块', 'apply': False},
                          headers=AUTH_HEADERS)
        assert res.status_code == 200, res.text
        token = res.json()['data']['decision_token']
        # 采纳必须回传预览的 token：apply=True 不带 token 会被拒（400），
        # 因为重跑一次 agent 可能给出别的基金，写进去的就不是老板看过的那份证据。
        res = client.post('/api/config/sector-mappings/ai-match',
                          json={'sector_name': 'AI写入板块', 'apply': True,
                                'decision_token': token},
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


def test_single_row_review_route_needs_explicit_owner_confirm(monkeypatch, tmp_path):
    """逐行"审查"给的免疫必须在 API 边界上也要求显式确认（第 16 轮 m-2）。

    上一版路由从不转发 `owner_confirm` ⇒ "明确确认"只存在于浏览器弹窗里，
    任何人直接 POST 一次就能拿到 owner 署名 + 体检锁定。
    失败原因也要分开说：一律回"映射不存在"会让老板对着一条真实存在的行反复点。
    """
    from src.models.database import FundInfo, SectorFundMapping

    session_factory = _database(tmp_path)
    db = session_factory()
    db.add(FundInfo(fund_code='512481', fund_name='半导体设备ETF'))
    db.add(SectorFundMapping(sector_name='逐行确认板块', fund_code='512481',
                             fund_name='半导体设备ETF', reviewed=False, is_active=True))
    db.commit()
    mapping_id = db.query(SectorFundMapping).first().id
    db.close()

    app, client = _client(monkeypatch, session_factory)

    refused = client.post(f"/api/config/sector-mappings/{mapping_id}/review",
                          headers=AUTH_HEADERS)
    assert refused.status_code == 200
    body = refused.json()
    assert body['success'] is False, '没有证据的行不该被一次裸 POST 买到免疫'
    assert '映射不存在' not in body['message'], body['message']
    assert 'owner_confirm' in body['message'], body['message']

    granted = client.post(f"/api/config/sector-mappings/{mapping_id}/review",
                          params={'owner_confirm': True}, headers=AUTH_HEADERS)
    assert granted.json()['success'] is True
    check = session_factory()
    try:
        row = check.query(SectorFundMapping).get(mapping_id)
        assert row.reviewed is True and row.owner_locked is True
        assert row.reviewed_by == 'owner'
    finally:
        check.close()

    missing = client.post("/api/config/sector-mappings/999999/review",
                          params={'owner_confirm': True}, headers=AUTH_HEADERS)
    assert '映射不存在' in missing.json()['message']


def test_fund_search_endpoint(monkeypatch, tmp_path):
    from src.fund.fund_api import fund_api as api_instance

    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    # 底层是混合证券搜索：结果里会混进股票（实测 000938→紫光股份）。
    # "人工换一只"的下拉框绝不能把股票端给老板，所以接口必须按 CATEGORYDESC 过滤。
    monkeypatch.setattr(api_instance, 'search_fund', lambda kw: [
        {'fund_code': '159995', 'fund_name': '芯片ETF', 'fund_type': '',
         'category_desc': '基金', 'is_fund': True},
        {'fund_code': '000938', 'fund_name': '紫光股份', 'fund_type': '',
         'category_desc': '深市', 'is_fund': False},
    ])
    try:
        res = client.get('/api/config/fund-search', params={'keyword': '芯片'},
                         headers=AUTH_HEADERS)
        assert res.status_code == 200
        body = res.json()
        assert [d['fund_code'] for d in body['data']] == ['159995']
        assert body['dropped_non_fund'] == 1
        assert client.get('/api/config/fund-search', params={'keyword': '  '},
                          headers=AUTH_HEADERS).status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_fund_search_endpoint_reports_upstream_failure(monkeypatch, tmp_path):
    """接口失败（None）不能伪装成"查无基金"（[]），否则老板以为真没有这只基金。"""
    from src.fund.fund_api import fund_api as api_instance
    from fastapi import HTTPException

    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    monkeypatch.setattr(api_instance, 'search_fund', lambda kw: None)
    try:
        res = client.get('/api/config/fund-search', params={'keyword': '芯片'},
                         headers=AUTH_HEADERS)
        assert res.status_code == 502
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
    if not secret:
        # 这条查的是"那把真实口令有没有被写进被跟踪的文件"。干净检出（新机器 / CI）没有 `.env`
        # 就没什么可查 —— 上一版在这里 `assert`，于是 `git archive HEAD` 出来的副本
        # 跑 `tests/unit` 必红一条（第 34 轮 A-MAJOR-1）。源码里的硬编码默认值另有 `test_main_no_hardcoded_password` 盯着。
        pytest.skip('本机 .env 没有 ACCESS_PASSWORD：没有真实口令可查，跳过泄漏比对'
                    '（源码硬编码默认值是另一条用例）')

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


# ---- 第 35 轮 B-MAJOR-1：创建路径那道"只允许基金不允许股票"的门，此前零覆盖 ----
# 复现（B 与我各做一次）：把 config.py:1797-1803 整段删掉、再把 1811-1813 改回
# `reviewed=True / verify_message=None / is_fetchable=None`，本文件与
# `test_sector_fund_manual_proof.py`、`test_sector_mapping_audit_import.py`
# 仍是 49 passed / 1 skipped —— 也就是这条门坏了没人报警。


def _post_create(client, payload):
    return client.post("/api/config/sector-mappings", json=payload, headers=AUTH_HEADERS)


def test_a_code_the_fund_domain_does_not_know_cannot_be_created(tmp_path, monkeypatch):
    """名字为空 + 基金域查无此码 ⇒ 必须**拒**并且**库里不留行**（第 7 轮 MAJOR-2 的原始形状）。"""
    from src.models.database import SectorFundMapping
    sf = _database(tmp_path)
    calls = []

    def fake_domain(self, code, use_roster=False):
        calls.append(code)
        return {'status': 'ok', 'name': ''}          # 名册里没有这个码（股票的典型结局）

    import importlib
    monkeypatch.setattr(importlib.import_module('src.fund.fund_api').FundAPI,
                        'get_fund_domain_name', fake_domain)
    app, client = _client(monkeypatch, sf)
    res = _post_create(client, {"sector_name": "测试白酒", "fund_code": "600519"})
    body = res.json()
    assert body.get("success") is False, '股票码被创建成功了：%s' % body
    assert '拒绝创建' in (body.get('message') or ''), body
    assert calls == ['600519'], '没有去基金域查过名字（探针根本没跑）：%s' % calls
    db = sf()
    try:
        left = db.query(SectorFundMapping).filter_by(sector_name='测试白酒').count()
    finally:
        db.close()
    assert left == 0, '虽然回了 success:false，行还是落库了（%d 行）' % left


def test_created_row_takes_no_immunity_when_the_probe_accuses(tmp_path, monkeypatch):
    """补到了名字但身份体检指控（股票名挂在基金码上）⇒ 落库必须是**未审查 + 不可服务 + 有理由**。"""
    from src.models.database import SectorFundMapping
    sf = _database(tmp_path)

    import importlib
    _FundAPI = importlib.import_module('src.fund.fund_api').FundAPI
    monkeypatch.setattr(_FundAPI, 'get_fund_domain_name',
                        lambda self, code, use_roster=False: {'status': 'ok', 'name': '贵州茅台'})
    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda code, name, sector='': ('名字对不上：600519 在基金域不是这只', None))
    app, client = _client(monkeypatch, sf)
    body = _post_create(client, {"sector_name": "测试白酒创建", "fund_code": "600519"}).json()
    assert body.get("success") is True, body
    db = sf()
    try:
        row = db.query(SectorFundMapping).filter_by(sector_name='测试白酒创建').one()
        got = (row.reviewed, row.is_fetchable, row.verify_message,
               getattr(row, 'owner_locked', None), getattr(row, 'reviewed_by', None))
    finally:
        db.close()
    assert got[0] is False, '被指控的行拿到 reviewed=True：%s' % (got,)
    assert got[1] is False, '被指控的行仍被当成可服务：%s' % (got,)
    assert got[2] and '名字对不上' in got[2], '理由没落库：%s' % (got,)
    assert not got[3] and got[4] != 'owner', '创建路径白送了老板免疫（#20 那条唯一入口）：%s' % (got,)


def test_a_clean_probe_creation_still_does_not_grant_owner_immunity(tmp_path, monkeypatch):
    """探针不指控时创建可以是"已审查"，但**绝不**顺带 `reviewed_by='owner'`/`owner_locked`。"""
    from src.models.database import SectorFundMapping
    sf = _database(tmp_path)
    import importlib
    _FundAPI = importlib.import_module('src.fund.fund_api').FundAPI
    monkeypatch.setattr(_FundAPI, 'get_fund_domain_name',
                        lambda self, code, use_roster=False: {'status': 'ok', 'name': '白酒ETF'})
    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda code, name, sector='': (None, {'verdict': 'unknown'}))
    app, client = _client(monkeypatch, sf)
    body = _post_create(client, {"sector_name": "测试干净创建", "fund_code": "512699"}).json()
    assert body.get("success") is True, body
    db = sf()
    try:
        row = db.query(SectorFundMapping).filter_by(sector_name='测试干净创建').one()
        assert row.fund_name == '白酒ETF', '空名字应由基金域补上，实际 %r' % row.fund_name
        assert not row.owner_locked and row.reviewed_by != 'owner',             '一次普通创建就盖上老板的章：%s / %s' % (row.reviewed_by, row.owner_locked)
    finally:
        db.close()
