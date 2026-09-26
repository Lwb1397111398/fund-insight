"""
Render 部署优化测试
"""
import json
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


def test_render_web_runs_migrations_before_starting_server():
    content = Path("render.yaml").read_text(encoding="utf-8")
    web_config, cron_config = content.split("  - type: cron", 1)

    assert "startCommand: python scripts/run_migrations.py && uvicorn" in web_config
    assert "run_migrations.py" not in cron_config


def test_scheduled_task_runner_runs_daily_jobs_once(monkeypatch):
    """一次性定时任务只执行一次统一预测验证。"""
    from scripts import run_scheduled_tasks

    calls = []

    class DummyScheduler:
        def _run_fund_update(self):
            calls.append("fund_update")

        def _run_prediction_verify(self):
            calls.append("prediction_verify")

        def _run_expired_verify(self):
            calls.append("expired_verify")

    monkeypatch.setattr(run_scheduled_tasks, "TaskScheduler", DummyScheduler)

    result = run_scheduled_tasks.run_daily_tasks()

    assert result["success"] is True
    assert calls == ["fund_update", "prediction_verify"]
    assert "expired_verify" not in result["tasks"]


def test_scheduled_task_runner_reports_followup_task_failure(monkeypatch):
    """Cron should fail visibly when a later scheduler step reports failure."""
    from scripts import run_scheduled_tasks

    class DummyScheduler:
        def _run_fund_update(self):
            return {"success": False, "error": "fund update failed"}

        def _run_prediction_verify(self):
            return {"success": True}

        def _run_expired_verify(self):
            return {"success": True}

    monkeypatch.setattr(run_scheduled_tasks, "TaskScheduler", DummyScheduler)

    result = run_scheduled_tasks.run_daily_tasks()

    assert result["success"] is False
    assert "fund_update" in result["failed_tasks"]
    assert result["tasks"]["fund_update"]["error"] == "fund update failed"


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
    """只有第三方那份不可变的资源允许强缓存，我们自己的页面与脚本一律要问服务器。"""
    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])

    from src.api.main import app

    client = TestClient(app)
    response = client.get("/web/axios.min.js")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=86400"

    # 会跟着页面一起改的不许强缓存：新 index.html + 旧 manager = 页面静默少一块数（第 34 轮 B-F-1）
    for url in ("/web/viewpoint-manager.js", "/web/common.css", "/web/index.html", "/", "/index.html"):
        got = client.get(url)
        assert got.status_code == 200, url
        cache = got.headers.get("Cache-Control", "")
        assert "max-age" not in cache, "%s 被强缓存了：%s" % (url, cache)
        assert "no-cache" in cache, "%s 至少要 no-cache，实际：%s" % (url, cache)


def test_health_detail_says_which_build_is_running(monkeypatch):
    """"线上跑的是哪一版"必须有接口能答，而不是靠人去比对页面指纹。

    2026-09-25 那天量出来线上是 8 月 6 日的构建、本地领先 113 个提交，而在此之前
    文档里每一句"页面上看得见"都没被送达过 —— 这个问题的代价是几轮返工。
    取不到提交号时必须老实写 `unknown`，**不许拿 `version: 2.0.0` 那种静态串充数**
    （那正是这次要修的毛病：一个看起来像版本号的字符串，谁看了都以为知道答案了）。
    """
    from datetime import datetime as _dt

    from fastapi.testclient import TestClient
    from src.api.main import app

    monkeypatch.setenv("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    client = TestClient(app)

    monkeypatch.delenv('RENDER_GIT_COMMIT', raising=False)
    monkeypatch.delenv('RENDER_GIT_BRANCH', raising=False)
    body = client.get("/api/health/detail", headers=AUTH_HEADERS).json()
    assert body['git_commit'] == 'unknown' and body['git_commit_source'] == 'unavailable', body
    assert body['git_branch'] == 'unknown'
    _dt.fromisoformat(body['started_at'])          # 必须是能解析的时刻
    assert body['uptime_seconds'] >= 0

    monkeypatch.setenv('RENDER_GIT_COMMIT', 'a9bdef30d409a4b64795e615fe5b32b47f8a93d8')
    monkeypatch.setenv('RENDER_GIT_BRANCH', 'main')
    body = client.get("/api/health/detail", headers=AUTH_HEADERS).json()
    assert body['git_commit'] == 'a9bdef30d409', body      # 截到 12 位，够用且不会被当成完整 sha
    assert body['git_commit_source'] == 'RENDER_GIT_COMMIT'
    assert body['git_branch'] == 'main'
    # 这条出口也不许变成泄密面：任何一处都不许出现连接串或口令
    blob = json.dumps(body, ensure_ascii=False)
    for needle in ('postgres', 'password', AUTH_HEADERS['X-Access-Password']):
        assert needle not in blob, '健康详情泄露了 %r' % needle
