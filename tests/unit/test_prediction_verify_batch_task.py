from datetime import date, timedelta

from src.models.database import Blogger, FundHistory, Post, Prediction


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

    result = service.verify_all_pending()

    assert result["data"]["success_count"] == 2
    assert calls[old_prediction.id] is True
    assert calls[due_prediction.id] is False
