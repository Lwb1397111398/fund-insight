"""
Render 部署优化测试
"""
from pathlib import Path

from fastapi.testclient import TestClient


AUTH_HEADERS = {"X-Access-Password": "test_password_123"}


def test_render_yaml_defines_daily_cron_service():
    """Render 配置应包含独立 Cron 服务，避免 Web 进程承担定时任务"""
    content = Path("render.yaml").read_text(encoding="utf-8")

    assert "type: cron" in content
    assert "name: fund-insight-scheduler" in content
    assert "schedule:" in content
    assert "python scripts/run_scheduled_tasks.py daily" in content


def test_scheduled_task_runner_runs_daily_jobs_once(monkeypatch):
    """一次性定时任务脚本应按顺序执行基金更新和预测验证"""
    from scripts import run_scheduled_tasks

    calls = []

    class DummyScheduler:
        def _run_sector_flow(self, trigger="scheduler"):
            calls.append(f"sector_flow:{trigger}")
            return {"success": True}

        def _run_fund_update(self):
            calls.append("fund_update")

        def _run_prediction_verify(self):
            calls.append("prediction_verify")

        def _run_expired_verify(self):
            calls.append("expired_verify")

    monkeypatch.setattr(run_scheduled_tasks, "TaskScheduler", DummyScheduler)

    result = run_scheduled_tasks.run_daily_tasks()

    assert result["success"] is True
    assert calls == ["sector_flow:render_cron", "fund_update", "prediction_verify", "expired_verify"]


def test_startup_migrations_are_disabled_by_default(monkeypatch):
    """启动阶段 DDL 默认关闭，避免生产冷启动时自动改表"""
    from src.api import main

    monkeypatch.delenv("ENABLE_STARTUP_MIGRATIONS", raising=False)
    assert main._startup_migrations_enabled() is False

    monkeypatch.setenv("ENABLE_STARTUP_MIGRATIONS", "true")
    assert main._startup_migrations_enabled() is True


def test_health_detail_returns_sanitized_diagnostics(monkeypatch):
    """健康详情接口应返回只读诊断信息且不泄露密钥或连接串"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    monkeypatch.setenv("APP_ENV", "production")

    from src.api.main import app

    client = TestClient(app)
    response = client.get("/api/health/detail", headers=AUTH_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "db_type" in data
    assert "app_env" in data
    assert "llm_configured" in data
    assert "crawler_enabled" in data
    assert "startup_migrations_enabled" in data
    assert "scheduler_running" in data
    serialized = str(data).lower()
    assert "database_url" not in serialized
    assert "api_key" not in serialized
    assert "password" not in serialized


def test_static_assets_have_cache_headers(monkeypatch):
    """静态 JS 资源应带缓存头，降低 Render 静态资源重复传输"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])

    from src.api.main import app

    client = TestClient(app)
    response = client.get("/web/axios.min.js")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=86400"
