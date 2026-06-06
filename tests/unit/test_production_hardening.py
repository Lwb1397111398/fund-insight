"""
生产环境加固测试
"""
import importlib
import os

from fastapi.testclient import TestClient


AUTH_HEADERS = {"X-Access-Password": os.getenv("ACCESS_PASSWORD", "test_password_123")}


def test_database_import_is_disabled_by_default(monkeypatch):
    """数据库导入接口默认禁用，避免误清空生产库"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.delenv("ENABLE_DATABASE_IMPORT", raising=False)

    from src.api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-database",
        headers=AUTH_HEADERS,
        files={"file": ("backup.db", b"not-a-real-db", "application/octet-stream")},
    )

    assert response.status_code == 403
    assert "已禁用" in response.json()["detail"]


def test_database_import_requires_confirmation_header_when_enabled(monkeypatch):
    """数据库导入启用后仍必须提供二次确认头"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.setenv("ENABLE_DATABASE_IMPORT", "true")

    from src.api.main import app

    client = TestClient(app)
    response = client.post(
        "/api/import-database",
        headers=AUTH_HEADERS,
        files={"file": ("backup.db", b"not-a-real-db", "application/octet-stream")},
    )

    assert response.status_code == 403
    assert "确认" in response.json()["detail"]


def test_test_data_cleanup_routes_are_hidden_by_default(monkeypatch):
    """测试数据清理接口默认隐藏，避免生产误删"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.delenv("ENABLE_TEST_DATA_ROUTES", raising=False)

    from src.api.main import app

    client = TestClient(app)
    response = client.get("/api/test-data/find", headers=AUTH_HEADERS)

    assert response.status_code == 404


def test_stats_error_hides_traceback_in_production(monkeypatch):
    """生产环境统计接口异常不返回 traceback"""
    from src.api.routes import stats

    class BrokenStatsService:
        def __init__(self, db):
            pass

        def get_all_stats(self):
            raise RuntimeError("boom")

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(stats, "StatsService", BrokenStatsService)

    result = importlib.import_module("asyncio").run(stats.get_stats(db=object()))

    assert result["success"] is False
    assert result["error"] == "统计数据获取失败"
    assert "traceback" not in result


def test_postgres_pool_settings_default_to_render_safe_values(monkeypatch):
    """PostgreSQL 连接池默认使用 Render/Supabase 友好的小连接数"""
    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("DB_MAX_OVERFLOW", raising=False)
    monkeypatch.delenv("DB_POOL_RECYCLE", raising=False)
    monkeypatch.delenv("DB_POOL_TIMEOUT", raising=False)

    from src.models import database

    settings = database._get_postgres_pool_settings()

    assert settings["pool_size"] == 3
    assert settings["max_overflow"] == 2
    assert settings["pool_recycle"] == 120
    assert settings["pool_timeout"] == 30
