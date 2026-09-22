import importlib

import pytest

from datetime import date, timedelta


@pytest.fixture(autouse=True)
def _no_on_demand_backfill(monkeypatch):
    """本文件的假基金码（AUDIT01）不许去打真实接口。

    第 16 轮：自动验证路径会按需补拉历史，而 `from src.fund import fund_api` 拿到的
    是 `FundAPI` **实例**（`src/fund/__init__.py` 把包属性重绑成了实例），在它身上打桩
    是空操作 ⇒ `test_automatic_verification_creates_change_log` 每跑一次就向东财请求一次
    AUDIT01（根 conftest 新加的零网络闸门实测抓到）。要桩得桩模块属性。
    """
    module = importlib.import_module('src.fund.fund_api')

    class _NoProbe:
        @staticmethod
        def backfill_history_range(*args, **kwargs):
            return 0

    monkeypatch.setattr(module, 'fund_data_manager', _NoProbe())


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
    # 人工改判只能落在**已有端点证据**的行上（见 `PredictionService.verify` 的说明）
    prediction.end_nav, prediction.end_nav_date = 1.08, date(2026, 7, 8)
    prediction.start_nav, prediction.start_nav_date = 1.05, date(2026, 7, 1)
    prediction.verify_count = 1
    test_db.commit()

    assert PredictionService(test_db).verify(
        prediction.id,
        actual_change=3.2,
        is_correct=True,
        ai_judgment="人工确认",
    ) is not None

    log = test_db.query(PredictionChangeLog).one()
    assert log.action == "verified"
    assert log.source == "manual"
    assert "status" in log.changed_fields
    assert log.before_state["status"] == "pending"
    # 人工确认与自动验证同口径：结论写 success/failed，不再写 status='verified'
    assert log.after_state["status"] == "success"


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


def test_manual_verify_refuses_a_row_without_endpoint_evidence(test_db):
    """第 24 轮：以前这个方法可以凭空写一条"页面显示正确、统计与体检都不认"的半结论。

    `blogger_stats` 按 `verify_count>0` 数、准确率区间按 `has_verdict`（需要端点净值）数，
    而它只写 `is_correct/status/actual_change` ⇒ 三套口径各说各话。
    现在没有证据就拒写，要下结论必须走 `verify_prediction`（那里有数据充分性门）。
    """
    from src.models.database import PredictionChangeLog
    from src.services.prediction_service import PredictionService
    from src.services.prediction_verify_service import has_verdict_trace
    from src.services.verdict_evidence import has_verdict

    prediction = _seed_prediction(test_db)
    assert has_verdict(prediction) is False

    result = PredictionService(test_db).verify(
        prediction.id, actual_change=9.9, is_correct=True, ai_judgment="凭空判对")

    assert result is None, '无证据的人工改判被写进了库'
    test_db.refresh(prediction)
    assert prediction.is_correct is None and prediction.status == 'pending'
    assert has_verdict_trace(prediction) is False, '写坏了一半：状态没变但结论字段落了'
    assert test_db.query(PredictionChangeLog).count() == 0, '拒写就不该留下"已验证"日志'
