import os

from fastapi.testclient import TestClient

AUTH_HEADERS = {"X-Access-Password": os.getenv("ACCESS_PASSWORD", "test_password_123")}


def test_update_all_funds_starts_background_task(monkeypatch):
    os.environ.setdefault("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    from src.api.main import app
    from src.api.routes import funds as funds_routes

    def fake_start(runner, run_inline=False):
        return {"success": True, "message": "基金更新任务已启动", "data": {"in_progress": True}}

    monkeypatch.setattr(funds_routes.fund_update_task, "start", fake_start)

    client = TestClient(app)
    response = client.post("/api/funds/update-all", headers=AUTH_HEADERS)

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["in_progress"] is True


def test_get_update_status_returns_task_state(monkeypatch):
    os.environ.setdefault("ACCESS_PASSWORD", AUTH_HEADERS["X-Access-Password"])
    from src.api.main import app
    from src.api.routes import funds as funds_routes

    monkeypatch.setattr(
        funds_routes.fund_update_task,
        "status",
        lambda: {"in_progress": False, "started_at": None, "finished_at": None, "last_result": None},
    )

    client = TestClient(app)
    response = client.get("/api/funds/update-status", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["data"]["in_progress"] is False
