from datetime import date, timedelta

from src.services.prediction_verify_service import PredictionVerifyService


def _service():
    return PredictionVerifyService.__new__(PredictionVerifyService)


def _history(values):
    start = date(2026, 1, 1)
    return [
        {"date": start + timedelta(days=index), "nav": value}
        for index, value in enumerate(values)
    ]


def test_process_metrics_uses_start_relative_days_as_primary_direction_metric():
    service = _service()

    metrics = service.calculate_process_metrics(
        nav_history=_history([100.0, 99.0, 99.5, 98.5, 99.2, 98.0]),
        start_nav=100.0,
        prediction_type="up",
    )

    assert metrics["max_change"] == 0
    assert metrics["final_change"] == -2
    assert metrics["peak_hit_days"] == 0
    assert metrics["total_days"] == 6
    assert metrics["peak_hit_ratio"] == 0
    assert metrics["daily_direction_hit_days"] == 2
    assert metrics["daily_direction_total_days"] == 5
    assert metrics["daily_direction_hit_ratio"] == 0.4


def test_comprehensive_verify_does_not_award_score_for_only_daily_rebounds():
    service = _service()

    result = service.comprehensive_verify(
        prediction_type="up",
        final_change=-2.0,
        process_metrics={
            "data_sufficient": True,
            "max_change": 0,
            "min_change": -2,
            "peak_hit_days": 0,
            "total_days": 6,
            "peak_hit_ratio": 0,
            "daily_direction_hit_days": 2,
            "daily_direction_total_days": 5,
            "daily_direction_hit_ratio": 0.4,
        },
    )

    assert result["is_correct"] is False
    assert result["verify_type"] == "failed"
    assert result["score"] == 0


def test_comprehensive_verify_accepts_process_when_most_start_relative_days_match_direction():
    service = _service()

    result = service.comprehensive_verify(
        prediction_type="up",
        final_change=-1.0,
        process_metrics={
            "data_sufficient": True,
            "max_change": 2,
            "min_change": -3,
            "peak_hit_days": 3,
            "total_days": 5,
            "peak_hit_ratio": 0.6,
            "daily_direction_hit_days": 2,
            "daily_direction_total_days": 5,
            "daily_direction_hit_ratio": 0.4,
        },
    )

    assert result["is_correct"] is True
    assert result["verify_type"] == "process"
    assert result["score"] >= 60


def test_comprehensive_verify_does_not_pass_on_brief_start_relative_spike():
    service = _service()

    result = service.comprehensive_verify(
        prediction_type="up",
        final_change=-2.0,
        process_metrics={
            "data_sufficient": True,
            "max_change": 1,
            "min_change": -3,
            "peak_hit_days": 1,
            "total_days": 10,
            "peak_hit_ratio": 0.1,
        },
    )

    assert result["is_correct"] is False
    assert result["verify_type"] == "failed"
    assert result["score"] == 0
