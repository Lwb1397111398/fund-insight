from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_db
from src.models.database import Base, Blogger, CleanupTask, Post, Prediction


AUTH_HEADERS = {"X-Access-Password": "cleanup-api-test"}


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'cleanup-api.db').as_posix()}",
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


def test_cleanup_preview_exposes_policy_fingerprint_and_protection(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    test_db = session_factory()
    blogger = Blogger(name="API 清理测试", platform="test")
    test_db.add(blogger)
    test_db.flush()
    post = Post(
        blogger_id=blogger.id,
        content="长期预测",
        post_date=date.today(),
        analyzed=True,
    )
    test_db.add(post)
    test_db.flush()
    test_db.add(Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        prediction_type="bullish",
        prediction_date=date.today(),
        target_date=date(date.today().year + 1, 12, 31),
        status="pending",
        is_deleted=False,
    ))
    test_db.commit()
    test_db.close()
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.get("/api/config/cleanup/preview", headers=AUTH_HEADERS)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["rule_version"] == "retention-v2"
    assert len(data["preview_fingerprint"]) == 64
    assert data["protected_counts"]["pending_predictions"] == 1
    assert data["protected_counts"]["long_term_predictions"] == 1
    assert all(len(samples) <= 20 for samples in data["samples"].values())


def test_cleanup_execute_rejects_stale_fingerprint_before_creating_task(
    monkeypatch, tmp_path
):
    session_factory = _database(tmp_path)
    monkeypatch.setenv("ENABLE_DATA_CLEANUP", "true")
    app, client = _client(monkeypatch, session_factory)
    headers = {**AUTH_HEADERS, "X-Danger-Confirm": "cleanup-data"}

    try:
        response = client.post(
            "/api/config/cleanup",
            headers=headers,
            json={"preview_fingerprint": "stale"},
        )
    finally:
        app.dependency_overrides.clear()

    # 旧执行器硬删已下线：直接 403，不再走到指纹校验
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail.get("hard_delete_disabled") is True or "hard_delete" in str(detail).lower()
    with session_factory() as db:
        assert db.query(CleanupTask).count() == 0


def test_cleanup_task_status_returns_404_for_unknown_task(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    app, client = _client(monkeypatch, session_factory)
    try:
        response = client.get(
            "/api/config/cleanup/tasks/missing", headers=AUTH_HEADERS
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_cleanup_status_marks_stale_pending_task_failed(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    with session_factory() as db:
        db.add(CleanupTask(
            task_id="stale-pending",
            status="pending",
            progress=0,
            created_at=datetime.now() - timedelta(minutes=6),
        ))
        db.commit()
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.get(
            "/api/config/cleanup/tasks/stale-pending", headers=AUTH_HEADERS
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert "中断" in data["error"] or "超时" in data["error"]
    assert data["completed_at"] is not None


def test_cleanup_status_keeps_recent_pending_task(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    with session_factory() as db:
        db.add(CleanupTask(
            task_id="recent-pending",
            status="pending",
            progress=0,
            created_at=datetime.now(),
        ))
        db.commit()
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.get(
            "/api/config/cleanup/tasks/recent-pending", headers=AUTH_HEADERS
        )
    finally:
        app.dependency_overrides.clear()

    assert response.json()["data"]["status"] == "pending"


def test_cleanup_background_task_reaches_completed(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    from src.api.routes.config import _run_cleanup_background
    from src.services import retention_cleanup_service as rcs
    from src.services.retention_cleanup_service import RetentionCleanupService

    # 单测临时打开旧硬删，验证后台任务链路仍可用（生产默认关闭）
    monkeypatch.setattr(rcs, "HARD_DELETE_DISABLED", False)

    with session_factory() as db:
        fingerprint = RetentionCleanupService(db).build_plan().fingerprint
        db.add(CleanupTask(
            task_id="background-test",
            status="pending",
            progress=0,
            current_item=0,
            total_items=0,
            cleanup_params={"preview_fingerprint": fingerprint},
        ))
        db.commit()

    monkeypatch.setattr("src.models.database.SessionLocal", session_factory)
    monkeypatch.setattr(RetentionCleanupService, "_create_backup", lambda self: None)
    _run_cleanup_background("background-test", fingerprint)

    with session_factory() as db:
        task = db.query(CleanupTask).filter_by(task_id="background-test").one()
        assert task.status == "completed"
        assert task.progress == 100
        assert task.result["success"] is True
