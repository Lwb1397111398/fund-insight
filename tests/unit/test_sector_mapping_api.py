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
