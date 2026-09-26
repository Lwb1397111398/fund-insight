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


def test_destructive_data_cleanup_routes_are_disabled_when_explicitly_off(monkeypatch):
    """显式 ENABLE_DATA_CLEANUP=false 时禁止批量硬删除。"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "false")

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


def test_destructive_cleanup_defaults_on(monkeypatch):
    """未配置时清理默认开启（仍需确认头）。"""
    monkeypatch.delenv("ENABLE_DATA_CLEANUP", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    from src.core.safety import destructive_cleanup_enabled

    assert destructive_cleanup_enabled() is True


def test_destructive_cleanup_explicit_false_overrides_default(monkeypatch):
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "false")
    from src.core.safety import destructive_cleanup_enabled

    assert destructive_cleanup_enabled() is False


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

    # 第 49 轮 #88：这道门从此**问连的是哪个库**，不再问 `APP_ENV`
    # （Render 没设那个变量 ⇒ 线上 `app_env=development`，旧判据在生产恒为假）。
    # 两件事一起钉：给不给堆栈只看 DB 类型；把 `APP_ENV` 故意写反也不许改变结论。
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr('src.models.database.DB_TYPE', 'postgresql')
    monkeypatch.setattr(stats, "StatsService", BrokenStatsService)

    result = stats.get_stats(db=object())

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


def _exploding_stats(monkeypatch):
    """让 `GET /api/stats` 抛一条"异常文本里带连接串与口令"的错 —— 用来验它会不会被发出去。"""
    import src.api.routes.stats as st

    class Boom:
        def __init__(self, db):
            pass

        def get_all_stats(self):
            raise RuntimeError('boom postgresql://svc:%s@db.invalid/proddb' % 'S3cr3tPW')

    monkeypatch.setattr(st, 'StatsService', Boom)
    return st


def test_a_crash_on_the_live_database_never_ships_a_traceback(monkeypatch):
    """第 49 轮 #88：那道门以前问 `APP_ENV`，而 **Render 没设它** —— 线上
    `/api/health/detail` 实测 `app_env=development` ⇒ 判据恒为假，一次抛错就把
    `str(e)` + 完整堆栈（含文件路径、SQL、可能含连接串）发进老板的手机 WebView。
    现在问的是**连的是哪个库**：本地 sqlite 才给堆栈（开发要用），PostgreSQL 不给。
    """
    st = _exploding_stats(monkeypatch)

    monkeypatch.setattr('src.models.database.DB_TYPE', 'postgresql')
    out = st.get_stats(db=None)
    assert out['success'] is False
    assert 'traceback' not in out, '生产把堆栈发出去了'
    assert 'S3cr3tPW' not in str(out) and 'db.invalid' not in str(out), \
        '错误响应里带着异常原文 ⇒ 连接串/口令会随它进浏览器：%s' % out

    monkeypatch.setattr('src.models.database.DB_TYPE', 'sqlite')
    dev = st.get_stats(db=None)
    assert 'traceback' in dev and 'boom' in dev['error'], \
        '本地排查被一起关掉了 ⇒ 这句"生产才藏"变成了谁也拿不到细节'


def test_boot_identity_carries_an_explicit_offset():
    """`started_at` 唯一的用途是拿它对"部署生效没有"的时刻 ⇒ 不带偏移的裸墙上时钟
    在 Render 上就是 UTC，读的人会差八小时（同仓为 `date.today()` 付过一次账）。"""
    detail_time = __import__('src.api.main', fromlist=['_build_identity'])._build_identity()['started_at']
    assert detail_time.endswith('+08:00'), \
        'started_at 没带时区 ⇒ 线上印的比北京早 8 小时：%s' % detail_time
