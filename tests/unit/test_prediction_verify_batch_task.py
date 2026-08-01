from datetime import date, datetime, timedelta

import pytest

from src.models.database import BatchAnalysisTask, Blogger, FundHistory, Post, Prediction


def test_prediction_verify_task_keeps_running_until_finished():
    from src.services.prediction_verify_task import PredictionVerifyTask

    task = PredictionVerifyTask()

    first = task.start(total=3)
    second = task.start(total=1)

    assert first["success"] is True
    assert first["data"]["in_progress"] is True
    assert first["data"]["total"] == 3
    assert second["success"] is False

    task.finish({"success": True, "message": "验证完成"})
    status = task.status()

    assert status["in_progress"] is False
    assert status["last_result"]["message"] == "验证完成"
    assert status["finished_at"] is not None


def test_prediction_verify_task_status_survives_service_recreation(test_db):
    from src.services.prediction_verify_task import PredictionVerifyTask

    first_service = PredictionVerifyTask()
    started = first_service.start(total=4, db=test_db)

    assert started["success"] is True
    assert started["data"]["task_id"] is not None

    second_service = PredictionVerifyTask()
    status = second_service.status(db=test_db)
    duplicate = second_service.start(total=2, db=test_db)

    assert status["in_progress"] is True
    assert status["total"] == 4
    assert duplicate["success"] is False
    assert duplicate["data"]["task_id"] == started["data"]["task_id"]


def test_prediction_verify_task_finish_is_persisted(test_db):
    from src.services.prediction_verify_task import PredictionVerifyTask

    service = PredictionVerifyTask()
    started = service.start(total=3, db=test_db)
    task_id = started["data"]["task_id"]
    service.finish(
        {"success": True, "message": "验证完成", "data": {"success_count": 2, "failed_count": 1}},
        db=test_db,
        task_id=task_id,
    )

    status = PredictionVerifyTask().status(db=test_db)
    stored = test_db.get(BatchAnalysisTask, task_id)

    assert status["in_progress"] is False
    assert status["last_result"]["message"] == "验证完成"
    assert status["success_count"] == 2
    assert status["failed_count"] == 1
    assert stored.status == "completed"
    assert stored.completed_at is not None


def test_prediction_verify_task_replaces_stale_running_task(test_db):
    from src.services.prediction_verify_task import PredictionVerifyTask

    stale = BatchAnalysisTask(
        task_type="predictions",
        status="running",
        total_count=5,
        started_at=datetime.now() - timedelta(minutes=31),
    )
    test_db.add(stale)
    test_db.commit()

    started = PredictionVerifyTask(stale_after=timedelta(minutes=30)).start(total=2, db=test_db)
    test_db.refresh(stale)

    assert started["success"] is True
    assert started["data"]["task_id"] != stale.id
    assert stale.status == "failed"
    assert "超时" in stale.error_message


def test_count_due_predictions_excludes_future_targets(test_db):
    from src.api.routes.predictions import _count_due_predictions

    today = date(2026, 7, 3)
    blogger = Blogger(name="测试博主", platform="eastmoney")
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, title="测试", content="内容", post_date=today)
    test_db.add(post)
    test_db.flush()

    test_db.add_all([
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            prediction_type="up",
            prediction_date=today - timedelta(days=30),
            target_date=today,
            status="pending",
            is_deleted=False,
        ),
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            prediction_type="up",
            prediction_date=today,
            target_date=today + timedelta(days=1),
            status="pending",
            is_deleted=False,
        ),
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            prediction_type="flat",
            prediction_date=today - timedelta(days=30),
            target_date=today,
            status="pending",
            is_deleted=False,
        ),
    ])
    test_db.commit()

    assert _count_due_predictions(test_db, today) == 1


def test_verification_status_rejects_prediction_before_target_date(test_db, monkeypatch):
    from src.services import prediction_verify_service
    from src.services.prediction_verify_service import PredictionVerifyService

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 7, 3)

    monkeypatch.setattr(prediction_verify_service, "date", FixedDate)

    today = date(2026, 7, 3)
    blogger = Blogger(name="测试博主", platform="eastmoney")
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, title="测试", content="内容", post_date=today)
    test_db.add(post)
    test_db.flush()
    prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="000001",
        fund_name="测试基金",
        prediction_type="up",
        prediction_date=today - timedelta(days=29),
        prediction_period="1个月",
        target_date=today + timedelta(days=1),
        status="pending",
        is_deleted=False,
    )
    test_db.add(prediction)
    test_db.add_all([
        FundHistory(fund_code="000001", fund_name="测试基金", nav_date=today, nav=1.0),
        FundHistory(fund_code="000001", fund_name="测试基金", nav_date=today + timedelta(days=1), nav=1.1),
    ])
    test_db.commit()

    result = PredictionVerifyService(test_db).get_verification_status(prediction.id)

    assert result["can_verify"] is False
    assert "预测周期尚未结束" in result["reason"]


def test_verify_all_pending_uses_force_for_old_pending_predictions(test_db, monkeypatch):
    from src.services.prediction_verify_service import PredictionVerifyService

    # 直接用 as_of 固定"当前日期"；force 的阈值是 target_date 距今 > 30 天。
    # 验证队列无时间上限：31 天前的预测同样入队（force=True）。
    today = date(2026, 7, 3)
    blogger = Blogger(name="测试博主", platform="eastmoney")
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, title="测试", content="内容", post_date=today)
    test_db.add(post)
    test_db.flush()
    old_prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        prediction_type="up",
        prediction_date=today - timedelta(days=60),
        target_date=today - timedelta(days=31),
        status="pending",
        is_deleted=False,
    )
    due_prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        prediction_type="up",
        prediction_date=today - timedelta(days=30),
        target_date=today,
        status="pending",
        is_deleted=False,
    )
    test_db.add_all([old_prediction, due_prediction])
    test_db.commit()

    calls = {}
    service = PredictionVerifyService.__new__(PredictionVerifyService)
    service.db = test_db
    service._nav_cache = {}
    service._cache_order = []
    service._warm_cache = lambda predictions, today: None

    def fake_verify(prediction_id, force=False):
        calls[prediction_id] = force
        return {"success": True, "message": "ok"}

    service.verify_prediction = fake_verify

    result = service.verify_all_pending(as_of=today)

    assert result["data"]["success_count"] == 2
    assert calls[old_prediction.id] is True
    assert calls[due_prediction.id] is False


def test_expired_verification_entry_delegates_to_unified_scan():
    from src.services.prediction_verify_service import PredictionVerifyService

    expected = {"success": True, "data": {"total": 2}}
    service = PredictionVerifyService.__new__(PredictionVerifyService)
    calls = []

    def fake_verify_all_pending():
        calls.append("verify_all_pending")
        return expected

    service.verify_all_pending = fake_verify_all_pending

    assert service.verify_expired_pending() is expected
    assert calls == ["verify_all_pending"]


def test_prediction_verify_task_progress_updates_are_visible(test_db):
    from src.services.prediction_verify_task import PredictionVerifyTask

    service = PredictionVerifyTask()
    started = service.start(total=3, db=test_db)
    task_id = started["data"]["task_id"]

    service.update_progress(1, 1, 0, db=test_db, task_id=task_id)
    status = PredictionVerifyTask().status(db=test_db)

    assert status["in_progress"] is True
    assert status["processed_count"] == 1
    assert status["success_count"] == 1
    assert status["failed_count"] == 0
    assert status["progress"] == pytest.approx(33.3)

    service.update_progress(3, 2, 1, db=test_db, task_id=task_id)
    status = PredictionVerifyTask().status(db=test_db)

    assert status["processed_count"] == 3
    assert status["progress"] == 100.0


def test_verify_all_pending_reports_progress_via_callback(test_db, monkeypatch):
    from src.services import prediction_verify_service
    from src.services.prediction_verify_service import PredictionVerifyService

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 7, 3)

    monkeypatch.setattr(prediction_verify_service, "date", FixedDate)

    today = date(2026, 7, 3)
    blogger = Blogger(name="进度回调博主", platform="eastmoney")
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, title="测试", content="内容", post_date=today)
    test_db.add(post)
    test_db.flush()
    predictions = [
        Prediction(
            post_id=post.id,
            blogger_id=blogger.id,
            prediction_type="up",
            prediction_date=today - timedelta(days=30),
            target_date=today,
            status="pending",
            is_deleted=False,
        )
        for _ in range(3)
    ]
    test_db.add_all(predictions)
    test_db.commit()

    service = PredictionVerifyService.__new__(PredictionVerifyService)
    service.db = test_db
    service._nav_cache = {}
    service._cache_order = []
    service._warm_cache = lambda predictions, today: None

    outcomes = iter([True, False, True])
    service.verify_prediction = lambda prediction_id, force=False: {
        "success": next(outcomes),
        "message": "ok",
    }

    calls = []
    result = service.verify_all_pending(
        as_of=today,
        progress_callback=lambda *args: calls.append(args),
    )

    assert result["data"]["success_count"] == 2
    assert result["data"]["failed_count"] == 1
    assert len(calls) == 3
    assert [c[0] for c in calls] == [1, 2, 3]
    assert calls[-1][1] == 2  # success_count
    assert calls[-1][2] == 1  # failed_count
    assert [c[3] for c in calls] == [p.id for p in predictions]


def test_verify_all_pending_reports_due_but_skipped_with_reasons(test_db):
    """到期但不进入验证队列的只有观望预测；再旧的预测也照常入队验证。"""
    from src.services.prediction_verify_service import PredictionVerifyService

    today = date(2026, 7, 3)
    blogger = Blogger(name="跳过原因博主", platform="eastmoney")
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, title="测试", content="内容", post_date=today)
    test_db.add(post)
    test_db.flush()
    flat_prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        prediction_type="flat",
        prediction_date=today - timedelta(days=30),
        target_date=today,
        status="pending",
        is_deleted=False,
    )
    stale_prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        prediction_type="up",
        prediction_date=today - timedelta(days=60),
        target_date=today - timedelta(days=31),
        status="pending",
        is_deleted=False,
    )
    test_db.add_all([flat_prediction, stale_prediction])
    test_db.commit()

    service = PredictionVerifyService.__new__(PredictionVerifyService)
    service.db = test_db
    service._nav_cache = {}
    service._cache_order = []
    service._warm_cache = lambda predictions, today: None
    service.verify_prediction = lambda prediction_id, force=False: {
        "success": True, "message": "ok",
    }

    result = service.verify_all_pending(as_of=today)

    skipped = {item["prediction_id"]: item["reason"] for item in result["data"]["skipped"]}
    # 观望预测跳过并给原因；陈旧预测照常入队（无时间上限）
    assert result["data"]["total"] == 1
    assert result["data"]["results"][0]["prediction_id"] == stale_prediction.id
    assert "观望" in skipped[flat_prediction.id]
    assert stale_prediction.id not in skipped
    assert "另有 1 个到期不验证" in result["message"]


def test_verify_all_status_surfaces_failure_summary(test_db):
    """批量验证结束后，状态接口应给出按原因合并的失败汇总。"""
    from src.services.prediction_verify_task import PredictionVerifyTask

    service = PredictionVerifyTask()
    started = service.start(total=3, db=test_db)
    task_id = started["data"]["task_id"]
    service.finish(
        {
            "success": True,
            "message": "验证完成：成功 1 个，失败 1 个，另有 1 个到期不验证",
            "data": {
                "total": 2,
                "success_count": 1,
                "failed_count": 1,
                "results": [
                    {"prediction_id": 1, "success": True, "message": "验证成功"},
                    {"prediction_id": 2, "success": False, "message": "基金 000001 无历史数据，请先更新基金数据"},
                ],
                "skipped": [
                    {"prediction_id": 3, "reason": "中性预测（观望）不参与验证"},
                ],
            },
        },
        db=test_db,
        task_id=task_id,
    )

    status = PredictionVerifyTask().status(db=test_db)

    assert status["in_progress"] is False
    assert "基金 000001 无历史数据" in status["failure_summary"]
    assert "不参与验证" in status["failure_summary"]
    assert "（1 条）" in status["failure_summary"]


def test_prediction_and_blogger_stats_commit_atomically(test_db, monkeypatch):
    from src.services import prediction_verify_service
    from src.services.prediction_verify_service import PredictionVerifyService

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 3)

    monkeypatch.setattr(prediction_verify_service, "date", FixedDate)
    today = FixedDate.today()
    blogger = Blogger(name="事务测试博主", platform="eastmoney")
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, title="事务测试", content="内容", post_date=today)
    test_db.add(post)
    test_db.flush()
    prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="000001",
        fund_name="事务测试基金",
        prediction_type="up",
        prediction_date=today - timedelta(days=1),
        prediction_period="1天",
        target_date=today,
        status="pending",
        is_deleted=False,
    )
    test_db.add(prediction)
    test_db.add_all([
        FundHistory(fund_code="000001", fund_name="事务测试基金", nav_date=today - timedelta(days=1), nav=1.0),
        FundHistory(fund_code="000001", fund_name="事务测试基金", nav_date=today, nav=1.1),
    ])
    test_db.commit()

    service = PredictionVerifyService(test_db)
    monkeypatch.setattr(
        service,
        "get_nav_history",
        lambda *args, **kwargs: [
            {"date": "2026-07-02", "nav": 1.0},
            {"date": "2026-07-03", "nav": 1.1},
        ],
    )
    monkeypatch.setattr(
        service,
        "comprehensive_verify",
        lambda **kwargs: {
            "is_correct": True,
            "verify_type": "rule",
            "score": 100,
            "analysis": "正确",
        },
    )

    def fail_stats(*args, **kwargs):
        raise RuntimeError("统计更新失败")

    monkeypatch.setattr(service, "_update_blogger_accuracy", fail_stats)

    with pytest.raises(RuntimeError, match="统计更新失败"):
        service.verify_prediction(prediction.id)
    test_db.rollback()
    test_db.refresh(prediction)

    assert prediction.status == "pending"
    assert prediction.verify_count in (None, 0)
