from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from src.models.database import SectorFundFlow, SectorFlowFetchRun
from src.services.sector_flow_service import SectorFlowService


def sample_item(name="测试板块", code="BK0001", category="industry"):
    return {
        "sector_code": code,
        "sector_name": name,
        "change_pct": 1.2,
        "turnover": 100.0,
        "main_net_flow": 5.0,
        "retail_net_flow": -2.0,
        "data_category": category,
    }


def test_behavior_thresholds():
    assert SectorFlowService.judge_behavior(3.0) == "grab"
    assert SectorFlowService.judge_behavior(1.0) == "build"
    assert SectorFlowService.judge_behavior(0.0) == "wash"
    assert SectorFlowService.judge_behavior(-1.0) == "sell"


def test_enrich_calculates_dark_pool_and_intensity(test_db):
    service = SectorFlowService(test_db)
    enriched = service.enrich(sample_item())

    assert enriched["dark_pool"] == 7.0
    assert enriched["main_intensity"] == pytest.approx(7.0)
    assert enriched["behavior"] == "grab"


def test_run_fetch_saves_run_log_and_records(test_db):
    service = SectorFlowService(test_db)
    service.crawler = Mock()
    service.crawler.fetch_sector_list.side_effect = [
        [sample_item("行业A", "BK1001", "industry")],
        [sample_item("概念A", "BK2001", "concept")],
    ]

    result = service.run_fetch(trigger="manual")

    assert result["success"] is True
    assert result["saved_count"] == 2
    assert test_db.query(SectorFundFlow).count() == 2
    run = test_db.query(SectorFlowFetchRun).one()
    assert run.trigger == "manual"
    assert run.status == "success"
    assert run.fetched_count == 2
    assert run.saved_count == 2


def test_run_fetch_is_idempotent_for_same_day(test_db):
    service = SectorFlowService(test_db)
    service.crawler = Mock()
    service.crawler.fetch_sector_list.return_value = [sample_item("行业A", "BK1001", "industry")]

    first = service.run_fetch(trigger="manual", categories=["industry"])
    second = service.run_fetch(trigger="manual", categories=["industry"])

    assert first["saved_count"] == 1
    assert second["saved_count"] == 1
    assert test_db.query(SectorFundFlow).count() == 1
    assert test_db.query(SectorFlowFetchRun).count() == 2


def test_run_fetch_failure_keeps_existing_data_and_logs_failure(test_db):
    existing = SectorFundFlow(
        flow_date=date.today(),
        sector_name="旧板块",
        sector_code="BKOLD",
        data_category="industry",
        main_net_flow=1.0,
        data_source="eastmoney",
    )
    test_db.add(existing)
    test_db.commit()

    service = SectorFlowService(test_db)
    service.crawler = Mock()
    service.crawler.fetch_sector_list.side_effect = RuntimeError("上游失败")

    result = service.run_fetch(trigger="manual", categories=["industry"])

    assert result["success"] is False
    assert test_db.query(SectorFundFlow).count() == 1
    run = test_db.query(SectorFlowFetchRun).one()
    assert run.status == "failed"
    assert "上游失败" in run.error_message


def test_get_fetch_status_reports_latest_run_and_data(test_db):
    service = SectorFlowService(test_db)
    test_db.add(SectorFundFlow(
        flow_date=date.today() - timedelta(days=1),
        sector_name="旧板块",
        sector_code="BKOLD",
        data_category="industry",
    ))
    test_db.add(SectorFlowFetchRun(
        trigger="manual",
        status="failed",
        flow_date=date.today(),
        error_message="上游失败",
    ))
    test_db.commit()

    status = service.get_fetch_status()

    assert status["latest_run"]["status"] == "failed"
    assert status["latest_run"]["error_message"] == "上游失败"
    assert status["latest_data_date"] == (date.today() - timedelta(days=1)).isoformat()
    assert status["today_data_count"] == 0
    assert status["displaying_stale_data"] is True


def test_cleanup_old_sector_flow_runs(test_db, monkeypatch):
    from src.tasks.cleanup_tasks import CleanupManager
    import src.tasks.cleanup_tasks as cleanup_tasks

    old_run = SectorFlowFetchRun(
        trigger="manual",
        status="success",
        flow_date=date.today() - timedelta(days=200),
    )
    new_run = SectorFlowFetchRun(
        trigger="manual",
        status="success",
        flow_date=date.today(),
    )
    test_db.add_all([old_run, new_run])
    test_db.commit()

    monkeypatch.setattr(cleanup_tasks, "SessionLocal", lambda: test_db)
    cleanup = CleanupManager()
    result = cleanup.cleanup_old_sector_flow_runs(keep_days=180)

    assert result["success"] is True
    assert result["deleted_sector_flow_runs"] == 1
