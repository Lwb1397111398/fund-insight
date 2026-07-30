"""观点任务僵尸自愈：轮询即自愈、心跳不误杀、重试按类型分发。"""
from datetime import date, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.deps import get_db
from src.models.database import BatchAnalysisTask, Viewpoint
from src.services.viewpoint_workflow_service import (
    STALE_TASK_AFTER,
    ViewpointWorkflowService,
)


AUTH_HEADERS = {"X-Access-Password": "viewpoint-heal-test"}


def _database(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'viewpoint-heal.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    from src.models.database import Base

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


def _task(**overrides):
    values = {
        "task_type": "viewpoint_fetch",
        "status": "running",
        "total_count": 10,
        "processed_count": 3,
        "started_at": datetime.now() - timedelta(minutes=5),
        "task_params": {"sources": ["sina_blog"], "limit_per_source": 20, "mode": "fetch"},
        "result_summary": {},
    }
    values.update(overrides)
    return BatchAnalysisTask(**values)


# ---------- 服务层 heal 语义 ----------


def test_heal_marks_stale_running_task_failed(test_db):
    task = _task(updated_at=datetime.now() - STALE_TASK_AFTER - timedelta(minutes=1))
    test_db.add(task)
    test_db.commit()

    assert ViewpointWorkflowService.heal_stale_task(test_db, task) is True

    test_db.refresh(task)
    assert task.status == "failed"
    assert "中断" in (task.error_message or "")
    assert task.completed_at is not None


def test_heal_keeps_task_with_fresh_heartbeat_even_if_started_long_ago(test_db):
    """关键护栏：started_at 很老但 updated_at 新鲜（任务仍在推进）绝不能杀。"""
    task = _task(
        started_at=datetime.now() - timedelta(hours=2),
        updated_at=datetime.now() - timedelta(seconds=30),
    )
    test_db.add(task)
    test_db.commit()

    assert ViewpointWorkflowService.heal_stale_task(test_db, task) is False
    test_db.refresh(task)
    assert task.status == "running"


def test_heal_handles_pending_and_is_idempotent_on_terminal(test_db):
    stale_pending = _task(status="pending", updated_at=datetime.now() - timedelta(minutes=40))
    done = _task(status="succeeded", updated_at=datetime.now() - timedelta(hours=5))
    test_db.add_all([stale_pending, done])
    test_db.commit()

    assert ViewpointWorkflowService.heal_stale_task(test_db, stale_pending) is True
    assert ViewpointWorkflowService.heal_stale_task(test_db, done) is False
    # 已自愈的再调一次不重复处理
    assert ViewpointWorkflowService.heal_stale_task(test_db, stale_pending) is False
    assert done.status == "succeeded"


def test_heal_none_task_is_noop(test_db):
    assert ViewpointWorkflowService.heal_stale_task(test_db, None) is False


# ---------- 端点集成 ----------


def test_latest_task_endpoint_heals_zombie_and_returns_failed(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    with session_factory() as db:
        db.add(_task(updated_at=datetime.now() - timedelta(minutes=30)))
        db.commit()
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.get("/api/viewpoints/tasks/latest", headers=AUTH_HEADERS)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert "中断" in (data["error_message"] or "")
    with session_factory() as db:
        task = db.query(BatchAnalysisTask).first()
        assert task.status == "failed"


def test_latest_task_endpoint_keeps_healthy_running_task(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    with session_factory() as db:
        db.add(_task(updated_at=datetime.now() - timedelta(seconds=10)))
        db.commit()
    app, client = _client(monkeypatch, session_factory)

    try:
        data = client.get("/api/viewpoints/tasks/latest", headers=AUTH_HEADERS).json()["data"]
    finally:
        app.dependency_overrides.clear()

    assert data["status"] == "running"


def test_batch_analyze_over_zombie_creates_new_task(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    with session_factory() as db:
        # 僵尸 batch 任务：running 且心跳超时
        db.add(_task(
            task_type="viewpoint_batch",
            status="running",
            updated_at=datetime.now() - timedelta(minutes=30),
            task_params={"limit": 10, "source": "all", "viewpoint_ids": []},
        ))
        # 一条待深度分析的新观点
        db.add(Viewpoint(
            content="半导体板块景气回升，短期看涨。",
            author="t",
            source="sina_blog",
            viewpoint_date=date.today(),
            is_deleted=False,
            is_summary=False,
        ))
        db.commit()
    monkeypatch.setattr("src.models.database.SessionLocal", session_factory)
    # 深度分析走桩，避免真实 LLM
    monkeypatch.setattr(
        ViewpointWorkflowService,
        "_default_deep_analyzer",
        staticmethod(lambda article, source: {
            "market_direction": "bullish",
            "confidence": 70,
            "analysis": "桩分析",
            "reasoning": "桩理由",
            "summary": "桩摘要",
            "time_horizon": "short",
        }),
    )
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.post(
            "/api/viewpoints/batch-analyze",
            headers=AUTH_HEADERS,
            json={"limit": 10},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["in_progress"] is True
    assert payload["total"] == 1
    with session_factory() as db:
        tasks = db.query(BatchAnalysisTask).order_by(BatchAnalysisTask.id).all()
        assert len(tasks) == 2
        assert tasks[0].status == "failed"  # 僵尸被自愈
        assert tasks[1].task_type == "viewpoint_batch"
        # viewpoint_ids 必须落库，重试才恢复得了范围
        assert tasks[1].task_params["viewpoint_ids"]


def test_retry_batch_task_runs_deep_analysis_via_dispatch(monkeypatch, tmp_path):
    session_factory = _database(tmp_path)
    with session_factory() as db:
        viewpoint = Viewpoint(
            content="医药板块估值偏低，中期看好创新药。",
            author="t",
            source="sina_blog",
            viewpoint_date=date.today(),
            is_deleted=False,
            is_summary=False,
        )
        db.add(viewpoint)
        db.flush()
        db.add(_task(
            task_type="viewpoint_batch",
            status="failed",
            error_message="进程中断",
            task_params={"limit": 10, "source": "all", "viewpoint_ids": [viewpoint.id]},
        ))
        db.commit()
        viewpoint_id = viewpoint.id
    monkeypatch.setattr("src.models.database.SessionLocal", session_factory)
    monkeypatch.setattr(
        ViewpointWorkflowService,
        "_default_deep_analyzer",
        staticmethod(lambda article, source: {
            "market_direction": "bullish",
            "confidence": 72,
            "analysis": "桩分析",
            "reasoning": "桩理由",
            "summary": "桩摘要",
            "time_horizon": "medium",
        }),
    )
    app, client = _client(monkeypatch, session_factory)

    try:
        response = client.post("/api/viewpoints/tasks/1/retry", headers=AUTH_HEADERS)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    with session_factory() as db:
        task = db.query(BatchAnalysisTask).first()
        # 分发修复的回归断言：batch 任务重试后必须真正跑完，
        # 而不是停在 pending 等下一轮自愈（旧 bug：retry 只调 run_fetch_task）。
        assert task.status == "succeeded"
        assert task.success_count == 1
        viewpoint = db.get(Viewpoint, viewpoint_id)
        assert viewpoint.market_direction == "bullish"
        assert viewpoint.analysis_summary == "succeeded"
