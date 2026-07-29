from datetime import date

from src.models.database import FundHistory
from src.services import prediction_verify_service as pvs_module
from src.services.prediction_verify_service import PredictionVerifyService
from src.core.config import config as app_config


def _seed_prediction(db, target_date=date(2026, 1, 10), prediction_date=date(2026, 1, 1)):
    from src.models.database import Blogger, Post, Prediction

    blogger = Blogger(name="边界测试博主", platform="wechat")
    db.add(blogger)
    db.flush()
    post = Post(
        blogger_id=blogger.id,
        title="边界测试",
        content="边界测试",
        post_date=prediction_date,
    )
    db.add(post)
    db.flush()
    prediction = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TEST001",
        fund_name="测试基金",
        sector="白酒",
        prediction_type="up",
        prediction_date=prediction_date,
        prediction_period="1周",
        target_date=target_date,
        status="pending",
        is_deleted=False,
    )
    db.add(prediction)
    db.commit()
    return prediction


def _fix_today(monkeypatch, fixed: date):
    """只固定 today()，不污染 ORM 比较与 isinstance。"""

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(fixed.year, fixed.month, fixed.day)

    monkeypatch.setattr(pvs_module, "date", FixedDate)


def _add_nav(db, fund_code, fund_name, rows):
    db.add_all(
        [
            FundHistory(
                fund_code=fund_code,
                fund_name=fund_name,
                nav_date=nav_date,
                nav=nav,
            )
            for nav_date, nav in rows
        ]
    )
    db.commit()


def test_weekday_wait_period_keeps_pending(test_db, monkeypatch):
    """工作日 + 等待期内 + 缺目标日净值 => 不完成、不增 verify_count"""
    target = date(2026, 1, 9)  # 周五
    _fix_today(monkeypatch, target)
    prediction = _seed_prediction(test_db, target_date=target)

    _add_nav(
        test_db,
        prediction.fund_code,
        prediction.fund_name,
        [
            (date(2026, 1, 1), 1.00),
            (date(2026, 1, 7), 0.99),
            (date(2026, 1, 8), 1.00),  # 周四，缺周五目标日
        ],
    )

    service = PredictionVerifyService(test_db)
    result = service.verify_prediction(prediction.id)

    assert result["success"] is False
    assert "等待目标日期净值更新" in result["message"]
    assert result["data"]["data_status"]["reason"] == "waiting_target_nav"
    test_db.refresh(prediction)
    assert prediction.status == "pending"
    assert (prediction.verify_count or 0) == 0


def test_target_nav_filled_then_completes(test_db, monkeypatch):
    """目标日净值补齐后正常完成；评分数据不含 target 之后"""
    target = date(2026, 1, 9)  # 周五
    _fix_today(monkeypatch, date(2026, 1, 10))
    prediction = _seed_prediction(test_db, target_date=target)

    _add_nav(
        test_db,
        prediction.fund_code,
        prediction.fund_name,
        [
            (date(2026, 1, 1), 1.00),
            (date(2026, 1, 8), 1.01),
            (target, 1.03),
            (date(2026, 1, 12), 1.20),  # target 之后，不得进入评分
        ],
    )

    service = PredictionVerifyService(test_db)
    result = service.verify_prediction(prediction.id)

    assert result["success"] is True, result
    assert abs(result["data"]["start_nav"] - 1.00) < 1e-9
    assert abs(result["data"]["end_nav"] - 1.03) < 1e-9
    assert result["data"]["verify_end_date"] == target.isoformat()

    history = service.get_nav_history(
        prediction.fund_code, prediction.prediction_date, target
    )
    history_dates = [row["date"] for row in history]
    assert date(2026, 1, 12) not in history_dates
    assert all(d <= target for d in history_dates)

    test_db.refresh(prediction)
    assert prediction.status in ("success", "failed")
    assert abs(prediction.end_nav - 1.03) < 1e-9


def test_weekend_target_accepts_friday_nav(test_db, monkeypatch):
    """周末目标日 + 周五净值 + age 合规 => 可立即完成"""
    target = date(2026, 1, 10)  # 周六
    _fix_today(monkeypatch, target)
    prediction = _seed_prediction(test_db, target_date=target)

    _add_nav(
        test_db,
        prediction.fund_code,
        prediction.fund_name,
        [
            (date(2026, 1, 1), 1.00),
            (date(2026, 1, 8), 1.00),
            (date(2026, 1, 9), 1.02),  # 周五
            (date(2026, 1, 12), 1.30),  # 下周一，不得使用
        ],
    )

    service = PredictionVerifyService(test_db)
    result = service.verify_prediction(prediction.id)

    assert result["success"] is True, result
    assert abs(result["data"]["end_nav"] - 1.02) < 1e-9
    assert result["data"]["verify_end_date"] == target.isoformat()
    history_dates = [
        row["date"]
        for row in service.get_nav_history(
            prediction.fund_code, prediction.prediction_date, target
        )
    ]
    assert date(2026, 1, 12) not in history_dates


def test_after_wait_accepts_compliant_previous(test_db, monkeypatch):
    """工作日 + 等待期结束 + 最近前值 age 合规 => 可完成"""
    target = date(2026, 1, 9)  # 周五
    _fix_today(monkeypatch, date(2026, 1, 11))  # 已过 2 个自然日等待
    prediction = _seed_prediction(test_db, target_date=target)

    _add_nav(
        test_db,
        prediction.fund_code,
        prediction.fund_name,
        [
            (date(2026, 1, 1), 1.00),
            (date(2026, 1, 7), 1.00),
            (date(2026, 1, 8), 1.01),  # 周四，缺周五
        ],
    )

    service = PredictionVerifyService(test_db)
    result = service.verify_prediction(prediction.id)

    assert result["success"] is True, result
    assert abs(result["data"]["end_nav"] - 1.01) < 1e-9
    assert result["data"]["verify_end_date"] == target.isoformat()


def test_old_end_nav_rejected_even_with_enough_points(test_db, monkeypatch):
    """数据点足够但 latest age 超限 => 明确拒绝，不完成"""
    target = date(2026, 1, 20)
    _fix_today(monkeypatch, date(2026, 1, 25))
    prediction = _seed_prediction(test_db, target_date=target)

    # age = 20 - 2 = 18 > 默认 10
    _add_nav(
        test_db,
        prediction.fund_code,
        prediction.fund_name,
        [
            (date(2026, 1, 1), 1.00),
            (date(2026, 1, 2), 1.01),
        ],
    )

    service = PredictionVerifyService(test_db)
    result = service.verify_prediction(prediction.id)

    assert result["success"] is False
    assert "超过允许陈旧期限" in result["message"]
    assert result["data"]["data_status"]["reason"] == "end_nav_too_old"
    test_db.refresh(prediction)
    assert prediction.status == "pending"
    assert (prediction.verify_count or 0) == 0


def test_force_still_rejects_too_old_nav(test_db, monkeypatch):
    """force=True 也不能接受超龄净值（只跳过等待期）"""
    target = date(2026, 1, 20)
    _fix_today(monkeypatch, date(2026, 2, 25))  # 远超 grace，需 force
    prediction = _seed_prediction(test_db, target_date=target)

    _add_nav(
        test_db,
        prediction.fund_code,
        prediction.fund_name,
        [
            (date(2026, 1, 1), 1.00),
            (date(2026, 1, 2), 1.01),
        ],
    )

    service = PredictionVerifyService(test_db)
    result = service.verify_prediction(prediction.id, force=True)

    assert result["success"] is False
    assert "超过允许陈旧期限" in result["message"]
    assert result["data"]["data_status"]["reason"] == "end_nav_too_old"


def test_get_nav_history_real_orm_excludes_after_target(test_db):
    """真实 ORM：库内同时有 target 前后记录时，查询不含 target 之后；不 mock"""
    fund_code = "BOUND01"
    start = date(2026, 1, 1)
    target = date(2026, 1, 10)
    _add_nav(
        test_db,
        fund_code,
        "边界",
        [
            (start, 1.0),
            (date(2026, 1, 5), 1.05),
            (target, 1.1),
            (date(2026, 1, 15), 1.5),
        ],
    )

    service = PredictionVerifyService(test_db)
    history = service.get_nav_history(fund_code, start, target)
    dates = [row["date"] for row in history]

    assert start in dates
    assert target in dates
    assert date(2026, 1, 15) not in dates
    assert all(d <= target for d in dates)


def test_strict_api_fallback_rejects_future_and_missing_date(test_db):
    """strict_as_of：API 未来日期或缺失日期不得当作历史净值"""
    service = PredictionVerifyService(test_db)
    request_day = date(2026, 1, 10)

    class FakeAPI:
        def __init__(self, payload):
            self.payload = payload

        def get_fund_info(self, fund_code):
            return self.payload

    service.fund_api = FakeAPI({"nav": 1.88, "nav_date": "2026-01-30"})
    assert service.get_nav_by_date("X", request_day, strict_as_of=True) is None

    service._nav_cache.clear()
    service._cache_order.clear()
    service.fund_api = FakeAPI({"nav": 1.88, "nav_date": ""})
    assert service.get_nav_by_date("X", request_day, strict_as_of=True) is None

    service._nav_cache.clear()
    service._cache_order.clear()
    service.fund_api = FakeAPI({"nav": 1.77, "nav_date": "2026-01-09"})
    assert service.get_nav_by_date("X", request_day, strict_as_of=True) == 1.77


def test_config_overrides_wait_and_max_age(test_db, monkeypatch):
    """VERIFY_DATA_WAIT_DAYS / VERIFY_MAX_END_NAV_AGE_DAYS 可覆盖默认值"""
    target = date(2026, 1, 9)
    _fix_today(monkeypatch, date(2026, 1, 10))  # waited=1
    monkeypatch.setattr(app_config, "VERIFY_DATA_WAIT_DAYS", 5)
    monkeypatch.setattr(app_config, "VERIFY_MAX_END_NAV_AGE_DAYS", 1)

    prediction = _seed_prediction(test_db, target_date=target)
    _add_nav(
        test_db,
        prediction.fund_code,
        prediction.fund_name,
        [
            (date(2026, 1, 1), 1.00),
            (date(2026, 1, 8), 1.01),  # age=1；wait=5 应仍等待
        ],
    )

    service = PredictionVerifyService(test_db)
    result = service.verify_prediction(prediction.id)
    assert result["success"] is False
    assert "等待目标日期净值更新" in result["message"]

    monkeypatch.setattr(app_config, "VERIFY_DATA_WAIT_DAYS", 0)
    monkeypatch.setattr(app_config, "VERIFY_MAX_END_NAV_AGE_DAYS", 0)
    result2 = service.verify_prediction(prediction.id)
    assert result2["success"] is False
    assert "超过允许陈旧期限" in result2["message"]
