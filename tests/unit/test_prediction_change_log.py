from datetime import date, timedelta


def _seed_prediction(db):
    from src.models.database import Blogger, Post, Prediction

    blogger = Blogger(name="变更日志测试博主", platform="wechat")
    db.add(blogger)
    db.flush()
    post = Post(
        blogger_id=blogger.id,
        title="变更日志测试",
        content="验证预测修改可追溯。",
        post_date=date(2026, 7, 1),
    )
    db.add(post)
    db.flush()
    prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="AUDIT01",
        fund_name="审计测试基金",
        sector="白酒",
        prediction_type="up",
        prediction_date=post.post_date,
        prediction_period="1周",
        target_date=post.post_date + timedelta(days=7),
        status="pending",
        is_deleted=False,
    )
    db.add(prediction)
    db.commit()
    return prediction


def test_prediction_edit_archive_and_restore_create_change_logs(test_db):
    from src.models.database import PredictionChangeLog
    from src.services.prediction_service import PredictionService

    prediction = _seed_prediction(test_db)
    service = PredictionService(test_db)

    service.update_prediction_fields(prediction.id, {"confidence": 75})
    service.delete_prediction(prediction.id)
    service.restore_prediction(prediction.id)

    logs = (
        test_db.query(PredictionChangeLog)
        .filter(PredictionChangeLog.prediction_id == prediction.id)
        .order_by(PredictionChangeLog.id)
        .all()
    )
    assert [log.action for log in logs] == ["updated", "archived", "restored"]
    assert logs[0].changed_fields == ["confidence"]
    assert logs[0].before_state["confidence"] in (None, 50)
    assert logs[0].after_state["confidence"] == 75
    assert logs[1].before_state["is_deleted"] is False
    assert logs[1].after_state["is_deleted"] is True
    assert logs[2].before_state["is_deleted"] is True
    assert logs[2].after_state["is_deleted"] is False


def test_application_json_backup_includes_prediction_change_logs(test_db):
    from src.models.database import PredictionChangeLog
    from src.services.data_portability_service import DataPortabilityService
    from src.services.prediction_service import PredictionService

    prediction = _seed_prediction(test_db)
    PredictionService(test_db).update_prediction_fields(prediction.id, {"confidence": 60})

    exported = DataPortabilityService(test_db).export_data()

    assert exported["export_version"] == "1.3"
    assert exported["summary"]["prediction_change_logs"] == 1
    assert exported["prediction_change_logs"][0]["prediction_id"] == prediction.id
    assert test_db.query(PredictionChangeLog).count() == 1


def test_manual_verification_creates_change_log(test_db):
    from src.models.database import PredictionChangeLog
    from src.services.prediction_service import PredictionService

    prediction = _seed_prediction(test_db)

    PredictionService(test_db).verify(
        prediction.id,
        actual_change=3.2,
        is_correct=True,
        ai_judgment="人工确认",
    )

    log = test_db.query(PredictionChangeLog).one()
    assert log.action == "verified"
    assert log.source == "manual"
    assert "status" in log.changed_fields
    assert log.before_state["status"] == "pending"
    assert log.after_state["status"] == "verified"


def test_automatic_verification_creates_change_log(test_db, monkeypatch):
    from src.models.database import FundHistory, PredictionChangeLog
    from src.services import prediction_verify_service
    from src.services.prediction_verify_service import PredictionVerifyService

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 8)

    monkeypatch.setattr(prediction_verify_service, "date", FixedDate)
    prediction = _seed_prediction(test_db)
    test_db.add_all([
        FundHistory(
            fund_code=prediction.fund_code,
            fund_name=prediction.fund_name,
            nav_date=date(2026, 7, 1),
            nav=1.0,
        ),
        FundHistory(
            fund_code=prediction.fund_code,
            fund_name=prediction.fund_name,
            nav_date=date(2026, 7, 8),
            nav=1.1,
        ),
    ])
    test_db.commit()

    service = PredictionVerifyService(test_db)
    monkeypatch.setattr(
        service,
        "get_nav_history",
        lambda *args, **kwargs: [
            {"date": "2026-07-01", "nav": 1.0},
            {"date": "2026-07-08", "nav": 1.1},
        ],
    )
    monkeypatch.setattr(
        service,
        "comprehensive_verify",
        lambda **kwargs: {
            "is_correct": True,
            "verify_type": "rule",
            "score": 100,
            "analysis": "方向正确",
        },
    )

    result = service.verify_prediction(prediction.id)

    assert result["success"] is True
    log = test_db.query(PredictionChangeLog).one()
    assert log.action == "verified"
    assert log.source == "automatic"
    assert log.before_state["status"] == "pending"
    assert log.after_state["status"] == "success"
