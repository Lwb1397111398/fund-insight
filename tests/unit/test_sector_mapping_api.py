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
    """补到了名字但身份体检指控（股票名挂在基金码上）⇒ **整行不建**，理由回给调用方。

    第 7 轮的旧契约是"落库但未审查 + 不可服务"，第 50 量出它做不到：档案被同一道门拒建，
    那一行的 `fund_code` 就是悬空引用 —— 镜像上 FK 直接拒（老板看到的是一句
    `IntegrityError` 原文），生产（实测没有这条 FK）静默留下一行指向查无此码的映射。
    "不建"同时满足这一条原本要钉的两件事：没有免疫可白送，也没有脏行可留。
    """
    from src.models.database import FundInfo, SectorFundMapping
    sf = _database(tmp_path)

    import importlib
    _FundAPI = importlib.import_module('src.fund.fund_api').FundAPI
    monkeypatch.setattr(_FundAPI, 'get_fund_domain_name',
                        lambda self, code, use_roster=False: {'status': 'ok', 'name': '贵州茅台'})
    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda code, name, sector='': ('名字对不上：600519 在基金域不是这只', None))
    app, client = _client(monkeypatch, sf)
    body = _post_create(client, {"sector_name": "测试白酒创建", "fund_code": "600519"}).json()
    assert body.get("success") is False, \
        '被身份指控的股票码还是建出了映射（这一族的原罪）：%s' % body
    assert '名字对不上' in (body.get('message') or ''), \
        '拒建必须把理由回给老板，而不是吞掉输入：%s' % body
    db = sf()
    try:
        assert db.query(SectorFundMapping).filter_by(
            sector_name='测试白酒创建').count() == 0, '回了 success:false，行还是落库了'
        assert db.query(FundInfo).filter_by(fund_code='600519').count() == 0, \
            '映射没建却先补了基金档案 ⇒ 垃圾码又回来了'
    finally:
        db.close()


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


def test_batch_review_route_forwards_the_owner_confirm(tmp_path, monkeypatch):
    """批量审查的"老板明确确认"必须在 **API 边界**上转发 —— 第 36 轮 B-MAJOR-2。

    单行那条路由早已有同名用例钉着，批量这条到今天零覆盖：
    把 `owner_confirm=req.owner_confirm` 删掉，全仓不红，而它的后果正是第 16 轮那个 bug 的形状
    —— 免疫只能由显式确认换来，路由不转发就等于"确认"只活在浏览器弹窗里。
    """
    from datetime import datetime
    from src.models.database import SectorFundMapping

    sf = _database(tmp_path)
    db = sf()
    db.add(SectorFundMapping(sector_name='批量无证据', fund_code='512481',
                             fund_name='半导体设备ETF', reviewed=False, is_active=True))
    db.add(SectorFundMapping(sector_name='批量有证据', fund_code='512482', fund_name='卫星ETF',
                             reviewed=False, is_active=True,
                             match_source='agent', verified_at=datetime(2026, 9, 1)))
    db.commit()
    no_ev = db.query(SectorFundMapping).filter_by(sector_name='批量无证据').one().id
    with_ev = db.query(SectorFundMapping).filter_by(sector_name='批量有证据').one().id
    db.close()

    app, client = _client(monkeypatch, sf)

    refused = client.post("/api/config/sector-mappings/batch-review",
                          json={'ids': [no_ev], 'reviewed': True}, headers=AUTH_HEADERS).json()
    assert refused['success'] is True and refused['data']['count'] == 0, \
        '没有老板确认，裸 POST 不该把无证据行标成已审查：%s' % refused

    granted = client.post("/api/config/sector-mappings/batch-review",
                          json={'ids': [no_ev], 'reviewed': True, 'owner_confirm': True},
                          headers=AUTH_HEADERS).json()
    assert granted['data']['count'] == 1, '转发了 owner_confirm 才该给免疫：%s' % granted
    check = sf()
    try:
        row = check.query(SectorFundMapping).get(no_ev)
        assert row.reviewed is True and row.owner_locked is True and row.reviewed_by == 'owner'

        quiet = check.query(SectorFundMapping).get(with_ev)
        assert not quiet.owner_locked and quiet.reviewed_by != 'owner'
    finally:
        check.close()

    lit = client.post("/api/config/sector-mappings/batch-review",
                      json={'ids': [with_ev], 'reviewed': True}, headers=AUTH_HEADERS).json()
    assert lit['data']['count'] == 1, '有机器证据的行不需要老板确认也该能批量看过'
    check = sf()
    try:
        row = check.query(SectorFundMapping).get(with_ev)
        assert row.reviewed is True and not row.owner_locked, \
            '批量"看过"绝不该顺带发免疫（第 14 轮 MAJOR-2）：%s/%s' % (row.reviewed_by, row.owner_locked)
    finally:
        check.close()


def test_verify_fund_endpoint_reports_what_the_probe_said(tmp_path, monkeypatch):
    """`GET /api/config/verify-fund` 是页面上"这只基金能不能抓"的那支探针（①链）。

    第 38 轮 A 席 MAJOR-2 把我上一轮写的这条打回原形：桩当时发明了 `is_fetchable` / `status`
    两个**真接口压根不返回**的键（真返回是 `ok` / `api_name` / `message` / `kind` / …），
    而路由是纯 pass-through ⇒ "桩给什么、断言收到什么"＝同义反复，怎么改路由它都绿。
    现在三件事分开钉：① 桩的键集合必须逐字等于真函数的返回键（AST 读，不靠我抄）；
    ② 路由必须**原样**透传（改了形就红）；③ 页面读的到底是哪个键 —— 从 `web/index.html` 里取，
    因为契约的另一端是页面，不是我的想象。
    """
    import ast
    from pathlib import Path as _P
    from src.fund.fund_api import fund_api as instance

    root = _P(__file__).resolve().parents[2]
    src = (root / 'src' / 'fund' / 'fund_api.py').read_text(encoding='utf-8')
    real_keys = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == 'verify_fund_fetchable':
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    real_keys = {k.value for k in sub.value.keys if isinstance(k, ast.Constant)}
    assert real_keys, '找不到真探针的返回形状 ⇒ 它变了，本用例要跟着重核，不许跳过'
    assert 'is_fetchable' not in real_keys and 'status' not in real_keys, \
        '真探针现在有 is_fetchable/status 了？那"页面读什么"这条得重新查，别再拿旧结论写桩'

    web = (root / 'web' / 'index.html').read_text(encoding='utf-8')
    assert 'd.ok' in web, '页面不再读 `d.ok` 了 ⇒ 探针的契约改了，这条用例与路由话术都要重写'

    probe_return = {k: None for k in real_keys}
    probe_return.update({'code': '513100', 'ok': False, 'api_name': '', 'message': '桩：数据源没这只',
                         'kind': 'unknown', 'history_count': 0, 'is_strict_ok': False})
    seen = {}

    def fake(code, name=None, **kw):
        seen['code'] = code
        return dict(probe_return)

    monkeypatch.setattr(instance, 'verify_fund_fetchable', fake)
    app, client = _client(monkeypatch, _database(tmp_path))
    res = client.get("/api/config/verify-fund", params={'fund_code': '513100'},
                     headers=AUTH_HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body['success'] is True
    assert seen['code'] == '513100', '代码没传进探针 ⇒ 页面报的是别只基金'
    assert set(body['data']) == real_keys, \
        '路由改了形（少了/多了键）：页面按 `d.ok` 读，改形就等于把两个验证按钮永久变成"验证失败"'
    assert body['data'] == probe_return, '路由不该加工探针的结论：%s' % body['data']


def test_the_create_route_grants_immunity_only_with_the_explicit_token(tmp_path, monkeypatch):
    """新建映射这一支的老板免疫只认 `owner_confirm`，两档都得钉（第 51 轮 B-13）。

    上一版它把参数**静默丢掉**了：不白送免疫（方向保守），但老板明确确认过一次却什么都不落、
    回执还写着"已创建映射"。现在与 `update_mapping` 同口径：署名与锁定一起给、一起不给。
    姿势：直接打路由函数（`TestClient` 那一层要 `X-Access-Password`，判据挂在外面的话
    拿到的会是 401/503 的 body，测的就不是这一条规矩了）。
    """
    from src.api.routes import config as cfg
    from src.models.database import FundInfo, SectorFundMapping

    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda code, name, sector='': (None, None))
    sf = _database(tmp_path)

    def _make(sector, owner_confirm):
        db = sf()
        try:
            if not db.query(FundInfo).filter_by(fund_code='512480').first():
                db.add(FundInfo(fund_code='512480', fund_name='半导体ETF'))
                db.commit()
            res = cfg.create_sector_mapping(
                cfg.MappingCreate(sector_name=sector, fund_code='512480',
                                  fund_name='半导体ETF'),
                owner_confirm=owner_confirm, db=db)
            assert res['success'] is True, res
            row = db.query(SectorFundMapping).filter_by(sector_name=sector).one()
            return row.reviewed, bool(row.owner_locked), row.reviewed_by
        finally:
            db.close()

    got = _make('T-新建无令牌', False)
    assert got[0] is True and not got[1] and got[2] != 'owner',         '没给显式令牌却落了老板署名/锁定：%s' % (got,)
    got2 = _make('T-新建带令牌', True)
    assert got2[1] is True and got2[2] == 'owner',         '老板显式确认过新建这一行，参数却被静默丢掉（回执还写着"已创建映射"）：%s' % (got2,)
    assert got2[0] is True, '给了令牌却不审查：%s' % (got2,)
