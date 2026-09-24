"""板块映射「审计结论定向回写」接口回归测试。

背景（本轮实测复现）：`scripts/push_sector_mappings_to_prod.py` 原本把 13 个字段的
审计清单逐行 `PUT /api/config/sector-mappings/{id}`，而该路由的请求模型
`MappingUpdate` 只有 fund_code/fund_name 两列 —— Pydantic 静默丢掉其余 11 个审计字段，
`service.update_mapping()` 还把每行标成 `reviewed=True / owner_locked=True /
reviewed_by='owner'`。结果：降级旗标从没到达生产，行却被永久锁定（从此免于体检与
agent），脚本却照样打印「[回写] 成功 N」。

因此新增 `POST /api/config/sector-mappings/-/audit-import`：按 sector_name 寻址、
逐列照搬审计结论、只创建/更新不删除、老板的行拒绝覆盖。
本文件的第一个用例就是**能抓住这个 bug 的那一个**。
"""
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_db
from src.models.database import Base


AUTH_HEADERS = {"X-Access-Password": "audit-import-test"}
CONFIRM = "WRITE-TO-PROD"
SECTOR = "__审计回写板块__"

# 一行完整的审计结论：形状与 scripts/export_repaired_mappings.py 导出的清单条目一致
# （含显式 null —— 那正是"退回未审查"的语义，不能被当成"没给这一列"）
AUDIT_ROW = {
    "sector_name": SECTOR,
    "fund_code": "159583",
    "fund_name": "富国通信设备ETF",
    "keywords": ["通信", "CPO"],
    "is_active": True,
    "reviewed": False,
    "match_source": "agent",
    "match_kind": "direct",
    "confidence": 0.61,
    "verified_at": "2026-09-21 09:32:19.609798",
    "verify_message": "代码在基金域的品种名与映射名不一致，降级待审",
    "llm_reason": "无对口标的，取关联度最大的替代",
    "is_fetchable": False,
    "evidence": json.dumps({"identity": {
        "verdict": "code_is_other_fund", "jaccard": 0.11,
        "official_name": "通信ETF富国", "reason": "同码撞上了另一只基金",
        "suggested_code": None, "suggestions": [], "relevance_low": True}},
        ensure_ascii=False),
    "reviewed_by": None,
    "owner_locked": False,
}


@pytest.fixture(autouse=True)
def _audit_import_enabled_and_offline(monkeypatch):
    """端点默认关闭（`ENABLE_SECTOR_AUDIT_IMPORT`，与 `ENABLE_DATABASE_IMPORT` 同惯例），
    且真写前要过基金域自证 —— 单测里两样都必须钉成确定状态，网络为零。
    """
    monkeypatch.setenv('ENABLE_SECTOR_AUDIT_IMPORT', 'true')
    import src.services.sector_fund_service as sfs
    monkeypatch.setattr(sfs, '_manual_identity_verdict',
                        lambda code, name, sector: (None, None))
    yield


class _NoopService:
    def ensure_fund_info_exists(self, *args, **kwargs):
        return False


def test_audit_switch_is_closed_by_default(monkeypatch):
    """没开总开关时，一列都不许写（生产默认就是这个状态）。"""
    from src.api.routes.config import _audit_apply_row
    monkeypatch.setenv('ENABLE_SECTOR_AUDIT_IMPORT', 'false')
    assert _audit_apply_row(None, None, None, 'T-开关',
                            {'fund_code': '159805'}) == \
        'import_disabled（服务端需设 ENABLE_SECTOR_AUDIT_IMPORT=true 才开这个口）'


def test_audit_import_cannot_mint_owner_immunity(test_db, monkeypatch):
    """清单里带 `owner_locked/reviewed_by` 也不生效：免疫只能由老板在页面上盖。"""
    from src.api.routes.config import _audit_apply_row
    from src.models.database import FundInfo, SectorFundMapping
    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda c, n, s: (None, None))
    test_db.add(FundInfo(fund_code='159805', fund_name='传媒ETF'))
    test_db.commit()
    assert _audit_apply_row(test_db, _NoopService(), None, 'T-免疫', {
        'fund_code': '159805', 'fund_name': '传媒ETF', 'reviewed': True,
        'owner_locked': True, 'reviewed_by': 'owner'}) is None
    row = test_db.query(SectorFundMapping).filter_by(sector_name='T-免疫').first()
    assert row.reviewed is True and not row.owner_locked
    assert row.reviewed_by is None


def test_audit_import_refuses_a_stock_and_a_resurrected_row(test_db, monkeypatch):
    """BLOCKER-1 的另外两条线：标的要先证身份；被判不可服务的行不许带着已审查进门。"""
    from src.api.routes.config import _audit_apply_row
    from src.models.database import FundInfo, SectorFundMapping
    test_db.add(FundInfo(fund_code='600519', fund_name='贵州茅台'))
    test_db.add(FundInfo(fund_code='159805', fund_name='传媒ETF'))
    test_db.commit()
    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda c, n, s: ('基金域查无此码：这是股票/已清盘', None))
    assert str(_audit_apply_row(test_db, _NoopService(), None, 'T-股票',
                                {'fund_code': '600519', 'fund_name': '贵州茅台'})) \
        .startswith('identity_unproven:')
    assert test_db.query(SectorFundMapping).filter_by(sector_name='T-股票').first() is None

    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda c, n, s: (None, None))
    # 体检说"同码撞了别的基金"，载荷却要 reviewed=True → 复活降级行，拒
    res = _audit_apply_row(test_db, _NoopService(), None, 'T-复活', {
        'fund_code': '159805', 'fund_name': '传媒ETF', 'reviewed': True,
        'is_fetchable': True,
        'evidence': json.dumps({'identity': {'verdict': 'code_is_other_fund'}})})
    assert res == 'unservable_but_reviewed'
    row = test_db.query(SectorFundMapping).filter_by(sector_name='T-复活').first()
    assert row is None or row.reviewed is not True


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'audit-import.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def _isolate_sector_cache():
    """映射缓存是类级的：回写会刷新它，用例之间必须清干净，禁止串库。"""
    from src.services.sector_fund_service import SectorFundService

    def _clear():
        SectorFundService._cache.clear()
        SectorFundService._cache_loaded = False
        SectorFundService._cache_at = 0.0

    _clear()
    yield
    _clear()


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


def _row(db, sector=SECTOR):
    from src.models.database import SectorFundMapping
    return db.query(SectorFundMapping).filter(
        SectorFundMapping.sector_name == sector).order_by(SectorFundMapping.id).first()


def _import(client, rows, dry_run=True, confirm=None):
    payload = {"mappings": rows, "dry_run": dry_run}
    if confirm is not None:
        payload["confirm"] = confirm
    return client.post("/api/config/sector-mappings/-/audit-import",
                       json=payload, headers=AUTH_HEADERS)


def _seed(session_factory, **kw):
    db = session_factory()
    try:
        from src.models.database import FundInfo, SectorFundMapping
        if not db.query(FundInfo).filter_by(
                fund_code=kw.get("fund_code", "512480")).first():
            db.add(FundInfo(fund_code=kw.get("fund_code", "512480"),
                            fund_name=kw.get("fund_name", "半导体ETF")))
        db.add(SectorFundMapping(**kw))
        db.commit()
    finally:
        db.close()


# ===== 缺陷复现：老 PUT 路由会静默丢掉审计字段（这就是新接口存在的理由） =====

def test_legacy_put_drops_audit_fields_and_never_locks_the_row(monkeypatch, tmp_path):
    """PUT /sector-mappings/{id} 只认 fund_code/fund_name：11 个审计字段全丢。

    不改这条语义（UI 在用），但必须有测试钉住"它不能用于审计回写"，
    否则下次又有人拿它回写生产。
    第 18 轮 MAJOR-1：以前它还会顺手给上 `reviewed_by='owner' + owner_locked=True`，
    等于"一次保存买到永久体检免疫"——审计回写拿不到锁定，逐行确认才行。
    """
    session_factory = _database(tmp_path)
    _seed(session_factory, sector_name=SECTOR, fund_code="512480",
          fund_name="半导体ETF", reviewed=True, is_active=True)
    app, client = _client(monkeypatch, session_factory)
    try:
        mid = _row(session_factory()).id
        payload = {k: v for k, v in AUDIT_ROW.items() if k != "sector_name"}
        res = client.put(f"/api/config/sector-mappings/{mid}", json=payload,
                         headers=AUTH_HEADERS)
        assert res.json()["success"] is True          # 接口回报"成功"
        db = session_factory()
        try:
            row = _row(db)
            assert row.fund_code == "159583"           # 只有代码/名字落地
            assert row.fund_name == "富国通信设备ETF"
            assert row.reviewed is True, "reviewed=False 被静默丢弃"
            assert row.is_fetchable is None, "降级旗标被静默丢弃"
            assert row.evidence is None, "证据链被静默丢弃"
            assert row.keywords is None, "关键词被静默丢弃"
            assert row.match_source != "agent"
            assert not row.owner_locked and row.reviewed_by != "owner", \
                "编辑保存不该送出免疫：署名与锁定只认显式 owner_confirm"
            # 生产读路径仍把这一行当"可服务"——降级等于没做
            from src.services.sector_identity_audit import row_unservable
            assert row_unservable(row) is False
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


# ===== 新接口：字段逐一落地 =====

def test_audit_import_roundtrip_lands_every_field(monkeypatch, tmp_path):
    """POST 审计行 → 每一列都必须原样落地，尤其是 reviewed=False / is_fetchable。"""
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        res = _import(client, [AUDIT_ROW], dry_run=False, confirm=CONFIRM)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["dry_run"] is False and body["written"] == 1
        assert body["data"]["counts"]["created"] == 1

        db = session_factory()
        try:
            row = _row(db)
            assert row is not None, "审计行未落库"
            for field, want in AUDIT_ROW.items():
                if field == "sector_name":
                    assert row.sector_name == want
                elif field == "verified_at":
                    assert row.verified_at == datetime.fromisoformat(want), \
                        "ISO 串必须还原成 DateTime，否则 SQLite 直接 TypeError"
                else:
                    assert getattr(row, field) == want, f"{field} 未落地"
            # 显式 null 也要照搬：这三项决定"该行重新回到待审、且不锁定"
            assert row.reviewed is False
            assert row.reviewed_by is None
            assert row.owner_locked is False
            assert json.loads(row.evidence)["identity"]["verdict"] == "code_is_other_fund"
        finally:
            db.close()

        # 前端读的 GET 视图同样要看到降级结论（芯片要能渲染 identity）
        listed = client.get("/api/config/sector-mappings", headers=AUTH_HEADERS).json()
        view = [m for m in listed["data"]["mappings"] if m["sector_name"] == SECTOR]
        assert len(view) == 1
        assert view[0]["reviewed"] is False
        assert view[0]["is_fetchable"] is False
        assert view[0]["servable"] is False, "降级行仍被读路径当可服务 = 回写白做"
        assert view[0]["identity_verdict"] == "code_is_other_fund"
        assert view[0]["identity_official_name"] == "通信ETF富国"
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_dry_run_writes_nothing(monkeypatch, tmp_path):
    """默认 dry-run：只出计划，一行都不许写。"""
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        res = _import(client, [AUDIT_ROW])
        assert res.status_code == 200
        body = res.json()
        assert body["dry_run"] is True and body["written"] == 0
        assert body["data"]["counts"]["created"] == 1        # 计划里说要新建
        assert body["data"]["items"][0]["outcome"] == "created"
        db = session_factory()
        try:
            assert _row(db) is None
            from src.models.database import FundInfo
            assert db.query(FundInfo).filter_by(fund_code="159583").first() is None
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_apply_without_confirm_writes_nothing(monkeypatch, tmp_path):
    """dry_run=false 但没带 confirm 字面量：退化成计划，不落库。"""
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        res = _import(client, [AUDIT_ROW], dry_run=False)
        assert res.status_code == 200
        body = res.json()
        assert body["confirm_ok"] is False
        assert body["dry_run"] is True, "缺确认必须自动退回 dry-run"
        assert "WRITE-TO-PROD" in body["message"]
        db = session_factory()
        try:
            assert _row(db) is None
        finally:
            db.close()

        wrong = _import(client, [AUDIT_ROW], dry_run=False, confirm="write")
        assert wrong.json()["dry_run"] is True

        header_ok = client.post("/api/config/sector-mappings/-/audit-import",
                                json={"mappings": [AUDIT_ROW], "dry_run": False},
                                headers={"X-Access-Password": AUTH_HEADERS["X-Access-Password"],
                                         "X-Audit-Confirm": CONFIRM})
        assert header_ok.json()["dry_run"] is False
        assert header_ok.json()["written"] == 1
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_refuses_owner_rows(monkeypatch, tmp_path):
    """老板有意挑定的代理（债券→512000、SpaceX→159206）机器不得覆盖，且要说明原因。"""
    session_factory = _database(tmp_path)
    _seed(session_factory, sector_name="__老板锁定__", fund_code="512480",
          fund_name="半导体ETF", reviewed=True, is_active=True,
          owner_locked=True, reviewed_by="owner")
    _seed(session_factory, sector_name="__老板署名__", fund_code="512480",
          fund_name="半导体ETF", reviewed=True, is_active=True,
          owner_locked=False, reviewed_by="owner")
    app, client = _client(monkeypatch, session_factory)
    try:
        rows = []
        for sector in ("__老板锁定__", "__老板署名__"):
            r = dict(AUDIT_ROW)
            r["sector_name"] = sector
            rows.append(r)
        body = _import(client, rows, dry_run=False, confirm=CONFIRM).json()
        assert body["written"] == 0
        assert body["data"]["counts"]["refused"] == 2
        reasons = {i["sector_name"]: i["reason"] for i in body["data"]["items"]}
        assert reasons == {"__老板锁定__": "owner_locked",
                           "__老板署名__": "reviewed_by_owner"}
        db = session_factory()
        try:
            for sector in ("__老板锁定__", "__老板署名__"):
                row = _row(db, sector)
                assert row.fund_code == "512480", "被拒的行不得被改标"
                assert row.reviewed is True and row.is_fetchable is None
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_updates_in_place_and_never_deletes(monkeypatch, tmp_path):
    """已知板块更新原行（不产生第二条），已一致的行报 unchanged；再次回写幂等。"""
    session_factory = _database(tmp_path)
    _seed(session_factory, sector_name=SECTOR, fund_code="512480",
          fund_name="半导体ETF", reviewed=True, is_active=True,
          match_source="agent", confidence=0.9)
    app, client = _client(monkeypatch, session_factory)
    try:
        first = _import(client, [AUDIT_ROW], dry_run=False, confirm=CONFIRM).json()
        assert first["data"]["counts"]["updated"] == 1
        item = first["data"]["items"][0]
        assert item["matched_by"] == "exact"
        assert "reviewed" in item["changed_fields"] and "is_fetchable" in item["changed_fields"]

        again = _import(client, [AUDIT_ROW], dry_run=False, confirm=CONFIRM).json()
        assert again["data"]["counts"]["unchanged"] == 1, "重复回写应报已一致，不该空写"
        assert again["written"] == 0

        db = session_factory()
        try:
            from src.models.database import SectorFundMapping
            assert db.query(SectorFundMapping).filter_by(sector_name=SECTOR).count() == 1
            assert _row(db).fund_code == "159583"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_creates_missing_fund_archive(monkeypatch, tmp_path):
    """清单里的新代码在目标库没档案时先补最小档案，而不是 FK IntegrityError。"""
    from src.models.database import FundInfo

    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        assert session_factory().query(FundInfo).filter_by(fund_code="889900").first() is None
        row = dict(AUDIT_ROW)
        row["sector_name"] = "__缺档案板块__"
        row["fund_code"] = "889900"
        body = _import(client, [row], dry_run=False, confirm=CONFIRM).json()
        assert body["data"]["counts"]["created"] == 1, body["data"]["items"]
        assert body["data"]["items"][0]["reason"] is None
        db = session_factory()
        try:
            archive = db.query(FundInfo).filter_by(fund_code="889900").first()
            assert archive is not None and archive.fund_name == "富国通信设备ETF"
            assert _row(db, "__缺档案板块__").fund_code == "889900"
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_absent_column_keeps_value_explicit_null_clears_it(monkeypatch, tmp_path):
    """"没给这一列"不动现值；"显式给 null"必须照搬清空 —— 两者必须分得开。

    清单里的 `reviewed_by: null` 就是"退回未审查"的语义；若按"缺列即 null"实现，
    任何精简过的请求都会把生产的证据列悄悄抹平。
    """
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        first = dict(AUDIT_ROW)
        first["reviewed_by"] = "agent"
        assert _import(client, [first], dry_run=False,
                       confirm=CONFIRM).json()["written"] == 1

        partial = {"sector_name": SECTOR, "fund_code": "159583",
                   "reviewed_by": None}          # 其余列一律不给
        body = _import(client, [partial], dry_run=False, confirm=CONFIRM).json()
        item = body["data"]["items"][0]
        assert item["outcome"] == "updated" and item["changed_fields"] == ["reviewed_by"]
        db = session_factory()
        try:
            row = _row(db)
            assert row.reviewed_by is None, "显式 null 必须照搬"
            assert row.evidence == AUDIT_ROW["evidence"], "没给的列被抹平了"
            assert row.keywords == AUDIT_ROW["keywords"]
            assert row.reviewed is False and row.is_fetchable is False
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_cannot_wipe_the_fund_name(test_db, monkeypatch):
    """第 9 轮 BLOCKER：显式把 `fund_name` 抹空必须拒 —— 名字一空，体检就永远
    判"没法比对"（unknown = 不指控），这行等于永久免疫，还能带着 reviewed=True 服务。
    但"没给这一列"是合法的局部更新，必须保留现值。
    """
    from src.api.routes.config import _audit_apply_row
    from src.models.database import FundInfo, SectorFundMapping
    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda c, n, s: (None, None))
    test_db.add(FundInfo(fund_code='512000', fund_name='券商ETF华宝'))
    row = SectorFundMapping(sector_name='T-抹名', fund_code='512000',
                            fund_name='券商ETF华宝', reviewed=True, is_active=True,
                            match_source='agent', verified_at=datetime.now())
    test_db.add(row)
    test_db.commit()
    assert _audit_apply_row(test_db, _NoopService(), row, 'T-抹名',
                            {'fund_code': '512000', 'fund_name': None,
                             'reviewed': True}) == 'fund_name_wiped'
    test_db.refresh(row)
    assert row.fund_name == '券商ETF华宝', '名字被载荷洗掉了'
    # 没给 fund_name 这一列：保留现值，正常更新
    assert _audit_apply_row(test_db, _NoopService(), row, 'T-抹名',
                            {'fund_code': '512000', 'confidence': 0.42}) is None
    test_db.refresh(row)
    assert row.fund_name == '券商ETF华宝' and row.confidence == pytest.approx(0.42)
    # 库里本来就无名的行：先补名字才允许回写
    nameless = SectorFundMapping(sector_name='T-本就无名', fund_code='512000',
                                 fund_name='', reviewed=False, is_active=True)
    test_db.add(nameless)
    test_db.commit()
    assert _audit_apply_row(test_db, _NoopService(), nameless, 'T-本就无名',
                            {'fund_code': '512000', 'reviewed': True}) == \
        'row_has_no_fund_name'


def test_audit_import_guards_read_the_row_before_it_is_mutated(test_db, monkeypatch):
    """第 8 轮 MAJOR-2/4：判据必须取**改之前**的行，且拒行前不许先去补档案。

    以前的写法是 `setattr` 完再判 verdict / 机器换标的章 —— 载荷里带一份
    `evidence={"identity":{"verdict":"ok"}}` 就能把行上原有的未确认换标章连旧结论
    一起洗掉，然后 `reviewed=True` 顺利进门；而 `ensure_fund_info_exists` 内部会
    commit，拒掉的行照常留下一只没人认领的基金档案。
    """
    from src.api.routes.config import _audit_apply_row
    from src.models.database import SectorFundMapping
    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda c, n, s: (None, None))
    row = SectorFundMapping(sector_name='T-改前判据', fund_code='159877',
                            fund_name='医疗ETF', reviewed=False, is_active=True,
                            evidence=json.dumps({'etf_upgrade': {'code': '159877',
                                                                 'from_code': '162412'}}))
    test_db.add(row)
    test_db.commit()

    class _CountingService:
        def __init__(self):
            self.calls = 0

        def ensure_fund_info_exists(self, *a, **k):
            self.calls += 1
            return True

    svc = _CountingService()
    payload = {'fund_code': '159877', 'fund_name': '医疗ETF', 'reviewed': True,
               'is_fetchable': True,
               'evidence': json.dumps({'identity': {'verdict': 'ok'}})}
    assert _audit_apply_row(test_db, svc, row, 'T-改前判据', dict(payload)) == \
        'unacknowledged_machine_swap'
    assert svc.calls == 0, '拒行之前就去补档案 = 生产里多一只没人认领的基金'
    test_db.refresh(row)
    assert row.reviewed is False
    assert json.loads(row.evidence).get('etf_upgrade'), '换标章被载荷洗掉了'

    # 老板在页面上确认过（章被人工编辑摘掉）之后，同样的载荷就该放行
    row.evidence = json.dumps({'identity': {'verdict': 'ok'}})
    row.reviewed = True
    test_db.commit()
    assert _audit_apply_row(test_db, svc, row, 'T-改前判据',
                            dict(payload)) is None
    assert svc.calls == 1


def test_audit_import_reports_per_row_outcomes(monkeypatch, tmp_path):
    """混合批次逐行报 outcome：updated / created / unchanged / refused(原因)。"""
    session_factory = _database(tmp_path)
    _seed(session_factory, sector_name=SECTOR, fund_code="159583",
          fund_name="富国通信设备ETF", reviewed=False, is_active=True)
    app, client = _client(monkeypatch, session_factory)
    try:
        same = dict(AUDIT_ROW)                      # unchanged（先落一次）
        _import(client, [same], dry_run=False, confirm=CONFIRM)
        new = dict(AUDIT_ROW)
        new["sector_name"] = "__新板块__"
        bad = dict(AUDIT_ROW)
        bad["sector_name"] = "__坏证据__"
        bad["evidence"] = "{不是 JSON"
        nocode = dict(AUDIT_ROW)
        nocode["sector_name"] = "__空代码__"
        nocode["fund_code"] = "   "
        body = _import(client, [same, new, bad, {k: v for k, v in nocode.items()
                                                 if k != "fund_code"}],
                       dry_run=False, confirm=CONFIRM).json()
        outcomes = {i["sector_name"]: (i["outcome"], i["reason"])
                    for i in body["data"]["items"]}
        assert outcomes[SECTOR][0] == "unchanged"
        assert outcomes["__新板块__"][0] == "created"
        assert outcomes["__坏证据__"] == ("refused", "bad_evidence_json")
        # 整列缺 fund_code 也不该让 145 行的批次吃 422：逐行回执才有意义
        assert outcomes["__空代码__"] == ("refused", "empty_fund_code")
        assert body["data"]["counts"]["refused"] == 2
        assert body["data"]["refused_reasons"] == {"bad_evidence_json": 1,
                                                   "empty_fund_code": 1}
        assert body["written"] == 1     # 只有 __新板块__ 真落库
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_route_does_not_collide_with_mapping_id_routes(monkeypatch, tmp_path):
    """`/sector-mappings/-/audit-import` 不能被 `/sector-mappings/{mapping_id}` 吃掉。

    新路由注册在 `{mapping_id}` 之前；同时按 id 的 PUT/POST-review/DELETE 三条老路由
    的语义必须原样不动（UI 还在用）。
    """
    session_factory = _database(tmp_path)
    _seed(session_factory, sector_name=SECTOR, fund_code="512480",
          fund_name="半导体ETF", reviewed=True, is_active=True)
    app, client = _client(monkeypatch, session_factory)
    try:
        res = _import(client, [])
        assert res.status_code == 200, (res.status_code, res.text[:200])
        assert res.json()["success"] is True
        assert res.json()["data"]["total"] == 0

        mid = _row(session_factory()).id
        put = client.put(f"/api/config/sector-mappings/{mid}",
                         json={"fund_name": "改名了"}, headers=AUTH_HEADERS)
        assert put.json()["success"] is True, put.text
        review = client.post(f"/api/config/sector-mappings/{mid}/review",
                             headers=AUTH_HEADERS)
        assert review.status_code == 200, review.text      # 命中的是 review 路由，不是 422
        assert "success" in review.json()
        assert client.delete(f"/api/config/sector-mappings/{mid}",
                             headers=AUTH_HEADERS).status_code == 200
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_requires_access_password(tmp_path):
    """新接口同样受 /api/ 中间件保护，且错误口令不能变成放行。"""
    import os
    session_factory = _database(tmp_path)
    from src.api.main import app

    def override():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    old = os.environ.get("ACCESS_PASSWORD")
    os.environ["ACCESS_PASSWORD"] = "audit-import-nopass"
    try:
        client = TestClient(app)
        assert client.post("/api/config/sector-mappings/-/audit-import",
                           json={"mappings": []}).status_code == 401
        assert client.post("/api/config/sector-mappings/-/audit-import",
                           json={"mappings": []},
                           headers={"X-Access-Password": "audit-import-nopass"}
                           ).status_code == 200
    finally:
        os.environ.pop("ACCESS_PASSWORD", None)
        if old is not None:
            os.environ["ACCESS_PASSWORD"] = old
        app.dependency_overrides.pop(get_db, None)


# ===== 采纳必须带 decision_token（否则重跑 agent 会写进老板没看过的证据） =====

def _fake_decision(sector, code="159995", name="芯片ETF", conf=0.92):
    from src.services.sector_fund_agent import FundCandidate, SectorDecision
    cand = FundCandidate(code=code, name=name, source="llm", official_name=name,
                         t3_suitable=True, t3_proxy=False, t3_score=95,
                         confidence=conf, verify={"is_strict_ok": True}, kind="etf")
    return SectorDecision(sector=sector, chosen=cand, status="matched", confidence=conf,
                          rounds=1, evidence=[{"stage": "T3", "code": code,
                                               "suitable": True, "score": 95}])


def test_ai_match_apply_without_token_is_refused_before_running_agent(monkeypatch, tmp_path):
    """apply=true 不带 token 必须直接拒绝，且**不能**重跑 agent（重跑=写没看过的证据）。"""
    from src.models.database import SectorFundMapping
    from src.services import sector_fund_agent as agent_mod

    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    ran = {}
    monkeypatch.setattr(agent_mod, "resolve_sector_fund",
                        lambda sector, **kw: ran.setdefault("ran", True) or _fake_decision(sector))
    try:
        res = client.post("/api/config/sector-mappings/ai-match",
                          json={"sector_name": "AI板块", "apply": True},
                          headers=AUTH_HEADERS)
        assert res.status_code == 400, res.text
        assert "decision_token" in res.json()["detail"]
        assert "ran" not in ran, "被拒的请求不许白烧一次 LLM/agent"
        db = session_factory()
        try:
            assert db.query(SectorFundMapping).count() == 0
        finally:
            db.close()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_ai_match_preview_then_apply_with_token_writes(monkeypatch, tmp_path):
    """预览拿 token → 带 token 采纳：写进去的就是看过的那份证据。"""
    from src.models.database import SectorFundMapping
    from src.services import sector_fund_agent as agent_mod

    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    monkeypatch.setattr(agent_mod, "resolve_sector_fund",
                        lambda sector, **kw: _fake_decision(sector))
    try:
        preview = client.post("/api/config/sector-mappings/ai-match",
                              json={"sector_name": "AI板块", "apply": False},
                              headers=AUTH_HEADERS)
        assert preview.status_code == 200, preview.text
        token = preview.json()["data"]["decision_token"]
        assert preview.json()["data"]["apply"]["applied"] is False

        applied = client.post("/api/config/sector-mappings/ai-match",
                              json={"sector_name": "AI板块", "apply": True,
                                    "decision_token": token}, headers=AUTH_HEADERS)
        assert applied.status_code == 200, applied.text
        assert applied.json()["data"]["apply"]["applied"] is True
        db = session_factory()
        try:
            row = db.query(SectorFundMapping).filter_by(sector_name="AI板块").first()
            assert row is not None and row.fund_code == "159995"
            assert row.match_source == "agent"
        finally:
            db.close()

        stale = client.post("/api/config/sector-mappings/ai-match",
                            json={"sector_name": "别的板块", "apply": True,
                                  "decision_token": token}, headers=AUTH_HEADERS)
        assert stale.status_code == 410, "token 与板块不符必须拒绝，而不是重跑"
    finally:
        app.dependency_overrides.pop(get_db, None)


def _seed_nav(session_factory, code, rows):
    """给"这只标的在本库定不定得了价"准备证据：档案 + N 行净值。"""
    from datetime import date, timedelta

    from src.models.database import FundHistory, FundInfo
    db = session_factory()
    try:
        db.add(FundInfo(fund_code=code, fund_name='测试基金'))
        for i in range(rows):
            db.add(FundHistory(fund_code=code,
                               nav_date=date(2026, 1, 1) + timedelta(days=i),
                               nav=1.0 + i / 100.0))
        db.commit()
    finally:
        db.close()


def test_audit_import_reports_rows_this_db_cannot_price(monkeypatch, tmp_path):
    """清单上的 `is_fetchable` 是在**镜像**算的 —— 服务端必须自己说这句话。

    第 27 轮两份复评共同抓到：回写清单 145 行里有 31 行在生产连 `fund_info` 档案都没有
    （压着 249 条活预测），而清单里的 `is_fetchable` 全写着 True。
    这一条只**报告**不拒收（改公共接口的拒收规则是老板的决定项，见检查单 §7 的 A/B）。
    """
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        res = _import(client, [AUDIT_ROW])
        body = res.json()
        item = body["data"]["items"][0]
        assert item["nav_priced_here"] is False, item
        assert "fund_info" in item["nav_priced_here_note"], item
        assert body["data"]["no_nav_priced_in_this_db"] == 1, body["data"]
        assert "定不了价" in body["message"], body["message"]
        # 契约不变：仍然只出计划、仍然算 created（要拒收得显式改规则）
        assert item["outcome"] == "created" and body["written"] == 0
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_audit_import_says_priced_when_this_db_has_the_nav_history(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    _seed_nav(session_factory, AUDIT_ROW["fund_code"], 31)
    try:
        body = _import(client, [AUDIT_ROW]).json()
        item = body["data"]["items"][0]
        assert item["nav_priced_here"] is True, item
        assert "nav_priced_here_note" not in item, item
        assert body["data"]["no_nav_priced_in_this_db"] == 0, body["data"]
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_thin_nav_is_reported_as_a_note_not_a_refusal(monkeypatch, tmp_path):
    """档案有、净值只有几行：算"能定价"，但要把薄厚说出来（长窗口预测会验不了）。"""
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    _seed_nav(session_factory, AUDIT_ROW["fund_code"], 5)
    try:
        body = _import(client, [AUDIT_ROW]).json()
        item = body["data"]["items"][0]
        assert item["nav_priced_here"] is True, item
        assert "5 行" in item["nav_priced_here_note"], item
        assert body["data"]["no_nav_priced_in_this_db"] == 0, body["data"]
    finally:
        app.dependency_overrides.pop(get_db, None)


class _Req:
    """路由只要 `request.headers.get(...)`，不必起整个 TestClient。"""

    def __init__(self, headers=None):
        self.headers = headers or {}


def test_a_dry_run_plan_says_when_the_switch_is_closed(tmp_path, monkeypatch):
    """总开关没开时，dry-run 那份"计划"必须自己说出来（第 40 轮 B 的 M-4）。

    `import_disabled` 以前只在 `_audit_apply_row` 里判，而那个函数只在**真写**分支才被调用
    ⇒ 计划结构上不可能带出这个信息：拿计划的人看到"更新 118、新建 27"，按下去撞上 145 行全拒。
    上面那条 `test_audit_switch_is_closed_by_default` 走的是内部函数，看不见这一层，
    所以这条走**路由**、且不借助夹具把开关打开。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.models.database import Base
    from src.api.routes import config as cfg

    engine = create_engine('sqlite:///%s' % (tmp_path / 'audit.db').as_posix())
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        monkeypatch.delenv('ENABLE_SECTOR_AUDIT_IMPORT', raising=False)
        payload = cfg.AuditImportRequest(
            mappings=[{'sector_name': '半导体', 'fund_code': '512480'}], dry_run=True)
        res = cfg.import_sector_mapping_audit(payload, _Req(), db)
        assert res['success'] is True and res['dry_run'] is True
        assert '没开' in res['message'] and 'import_disabled' in res['message'], res['message']
        monkeypatch.setenv('ENABLE_SECTOR_AUDIT_IMPORT', 'true')
        ok = cfg.import_sector_mapping_audit(payload, _Req(), db)
        assert '没开' not in ok['message'], '开关开了还警告 ⇒ 这句红字会变成噪音'
    finally:
        db.close()


def test_a_row_that_was_never_written_is_not_counted_as_written_anyway(monkeypatch, tmp_path):
    """写失败的行**不许**同时进"定不了价"那个桶（第 41 轮 B-MINOR-3）。

    上一版 `unservable += 1` 排在 `_audit_apply_row` **之前**，于是"本库定不了价 +
    服务端又拒了"这一族行同时落进 `refused` 和 `no_nav_priced_in_this_db`，
    而回执那句话是"其中 N 行……**本次已照样写入**"—— 把没写进去的行说成了写了。
    老板照那句话判断"生产已经有这三行了"，就会跳过去查生产。
    """
    session_factory = _database(tmp_path)
    _seed(session_factory, sector_name="__定不了价又被拒__", fund_code="600519",
          fund_name="贵州茅台", reviewed=False, is_active=True)
    app, client = _client(monkeypatch, session_factory)
    # 基金域自证说"这是股票" ⇒ 写入必被拒；本库又没有这只的档案 ⇒ 同时是"定不了价"
    monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                        lambda c, n, s: ('基金域查无此码：这是股票/已清盘', None))
    row = dict(AUDIT_ROW)
    row['sector_name'] = '__定不了价又被拒__'
    row['fund_code'] = '600519'
    row['fund_name'] = '贵州茅台'
    try:
        body = _import(client, [row], dry_run=False, confirm=CONFIRM).json()
        data = body['data']
        assert data['counts']['refused'] == 1, data['counts']
        assert data['no_nav_priced_in_this_db'] == 0, \
            '一行都没写进去，却被计进"本次已照样写入"的定不了价行数：%s' % data
        assert '本次已照样写入' not in body['message'], body['message']
        # 反向对照：把服务端那道拒拿掉 ⇒ 同一行真的写进去了，这时**必须**报出定不了价
        monkeypatch.setattr('src.services.sector_fund_service._manual_identity_verdict',
                            lambda c, n, s: (None, None))
        again = _import(client, [dict(row, sector_name='__定不了价但写成了__')],
                        dry_run=False, confirm=CONFIRM).json()
        assert again['data']['no_nav_priced_in_this_db'] == 1, again['data']
        assert '本次已照样写入' in again['message'], again['message']
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_an_unchanged_row_is_not_reported_as_written_anyway(monkeypatch, tmp_path):
    """库里本来就是这个值（`unchanged`）的行不许进"定不了价且照样写入"那个桶。

    第 41 轮 B-(a) 的残格：上一版把条件写成"只要没被拒就算"，于是**幂等重放**
    （第二次回写同一份清单，145 行全是 unchanged）会报"其中 N 行本次已照样写入"，
    而那一次连一个字节都没改。判据收紧成"这一次真的写了 / 计划要写"。
    """
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        first = _import(client, [dict(AUDIT_ROW)], dry_run=False, confirm=CONFIRM).json()
        assert first["data"]["counts"]["created"] == 1, first["data"]
        assert first["data"]["no_nav_priced_in_this_db"] == 1, \
            "新建 + 本库定不了价 ⇒ 这一格必须报出来（反向对照）：%s" % first["data"]
        assert "本次已照样写入" in first["message"], first["message"]

        again = _import(client, [dict(AUDIT_ROW)], dry_run=False, confirm=CONFIRM).json()
        assert again["data"]["counts"]["unchanged"] == 1, again["data"]["counts"]
        assert again["data"]["no_nav_priced_in_this_db"] == 0, \
            "一个字都没写的幂等重放，被说成「本次已照样写入」：%s" % again["data"]
        assert "本次已照样写入" not in again["message"], again["message"]

        plan = _import(client, [dict(AUDIT_ROW, sector_name="__计划里新板块__")]).json()
        assert plan["data"]["counts"]["created"] == 1 and plan["data"]["no_nav_priced_in_this_db"] == 1, \
            "dry-run 计划里「要新建且定不了价」的行仍要报出来（否则闸又变成只会说没有）：%s" % plan["data"]
    finally:
        app.dependency_overrides.pop(get_db, None)
