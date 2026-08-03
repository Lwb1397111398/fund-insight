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
