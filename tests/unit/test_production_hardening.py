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


def test_test_data_preview_route_is_available_by_default(monkeypatch):
    """测试数据预览接口默认可用，但不开放硬删除。"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.delenv("ENABLE_TEST_DATA_CLEANUP", raising=False)

    from src.api.main import app

    client = TestClient(app)
    response = client.get("/api/test-data/find", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"]["cleanup_enabled"] is False


def test_test_data_cleanup_is_disabled_by_default(monkeypatch):
    """关键词匹配的硬删除默认禁用，避免误删真实数据。"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.delenv("ENABLE_TEST_DATA_CLEANUP", raising=False)

    from src.api.main import app

    client = TestClient(app)
    response = client.post("/api/test-data/cleanup", headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert "已禁用" in response.json()["detail"]


def test_test_data_cleanup_requires_confirmation_when_explicitly_enabled(monkeypatch):
    """隔离维护环境开启后，硬删除仍要求二次确认。"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.setenv("ENABLE_TEST_DATA_CLEANUP", "true")

    from src.api.main import app

    client = TestClient(app)
    response = client.post("/api/test-data/cleanup", headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert "确认" in response.json()["detail"]


def test_destructive_data_cleanup_routes_are_disabled_by_default(monkeypatch):
    """常规过期数据和观点批量清理不能在默认环境删除资料。"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.delenv("ENABLE_DATA_CLEANUP", raising=False)

    from src.api.main import app

    client = TestClient(app)
    requests = [
        ("/api/config/cleanup", None),
        ("/api/config/cleanup/oldest", None),
        ("/api/config/cleanup/orphan-funds", None),
        ("/api/posts/cleanup-low-quality?dry_run=false", None),
        ("/api/viewpoints/cleanup", {"days": 10}),
    ]
    for path, payload in requests:
        response = client.post(path, headers=AUTH_HEADERS, json=payload)
        assert response.status_code == 403, path
        assert "已禁用" in response.json()["detail"]


def test_destructive_data_cleanup_requires_confirmation_when_enabled(monkeypatch):
    """维护环境即使显式开启，仍必须提供统一确认头。"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")

    from src.api.main import app

    client = TestClient(app)
    response = client.post("/api/config/cleanup", headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert "确认" in response.json()["detail"]


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
