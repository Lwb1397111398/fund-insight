import asyncio
from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.api.routes import predictions as prediction_routes
from src.models.database import (
    Blogger,
    FundInfo,
    Post,
    Prediction,
    PredictionChangeLog,
    SectorFundMapping,
)
from src.services.prediction_maintenance_service import PredictionMaintenanceService
from src.services.prediction_verify_service import PredictionVerifyService


def _prediction(db, blogger, post, *, fund_code="DUP01", sector="白酒", verified=False):
    value = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code=fund_code,
        fund_name="重复测试基金" if fund_code else None,
        sector=sector,
        prediction_type="up",
        prediction_content="未来一周上涨",
        confidence=80,
        prediction_date=post.post_date,
        prediction_period="1周",
        target_date=post.post_date + timedelta(days=7),
        status="success" if verified else "pending",
        is_expired=verified,
        is_correct=True if verified else None,
        verify_count=1 if verified else 0,
        verify_score=80 if verified else 0,
        verify_history=[{"date": "2026-07-08", "score": 80}] if verified else [],
        is_deleted=False,
    )
    db.add(value)
    db.flush()
    return value


def _blogger_post(db, name):
    blogger = Blogger(name=name, platform="wechat")
    db.add(blogger)
    db.flush()
    post = Post(
        blogger_id=blogger.id,
        title=f"{name}的帖子",
        content="用于测试预测维护操作。",
        post_date=date(2026, 7, 1),
        analyzed=True,
    )
    db.add(post)
    db.flush()
    return blogger, post


def test_duplicate_scan_never_groups_cross_blogger_or_missing_fund(test_db):
    first_blogger, first_post = _blogger_post(test_db, "博主甲")
    second_blogger, second_post = _blogger_post(test_db, "博主乙")
    first = _prediction(test_db, first_blogger, first_post)
    second = _prediction(test_db, first_blogger, first_post)
    cross_blogger = _prediction(test_db, second_blogger, second_post)
    _prediction(test_db, first_blogger, first_post, fund_code=None)
    _prediction(test_db, first_blogger, first_post, fund_code=None)
    test_db.commit()

    result = PredictionMaintenanceService(test_db).scan_duplicate_groups()

    assert result["duplicate_groups"] == 1
    assert result["candidate_predictions"] == 2
    assert result["groups"][0]["prediction_ids"] == [first.id, second.id]
    assert cross_blogger.id not in result["groups"][0]["prediction_ids"]
    assert test_db.query(Prediction).filter(Prediction.is_deleted == True).count() == 0


def test_sector_mapping_preview_uses_only_reviewed_mapping_and_does_not_write(test_db):
    old_fund = FundInfo(fund_code="OLD01", fund_name="旧基金")
    reviewed_fund = FundInfo(fund_code="NEW01", fund_name="已审核基金")
    unreviewed_fund = FundInfo(fund_code="NEW02", fund_name="未审核基金")
    test_db.add_all([old_fund, reviewed_fund, unreviewed_fund])
    blogger, post = _blogger_post(test_db, "映射博主")
    reviewed_prediction = _prediction(test_db, blogger, post, fund_code="OLD01", sector="白酒")
    ignored_prediction = _prediction(test_db, blogger, post, fund_code="OLD01", sector="医药")
    test_db.add_all([
        SectorFundMapping(
            sector_name="白酒", fund_code="NEW01", fund_name="已审核基金",
            reviewed=True, is_active=True,
        ),
        SectorFundMapping(
            sector_name="医药", fund_code="NEW02", fund_name="未审核基金",
            reviewed=False, is_active=True,
        ),
    ])
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=True)

    assert result["dry_run"] is True
    assert result["predictions_updated"] == 0
    assert result["would_update"] == 1
    assert result["details"][0]["prediction_id"] == reviewed_prediction.id
    test_db.refresh(reviewed_prediction)
    test_db.refresh(ignored_prediction)
    assert reviewed_prediction.fund_code == "OLD01"
    assert ignored_prediction.fund_code == "OLD01"
    assert test_db.query(PredictionChangeLog).count() == 0


def test_sector_mapping_execution_records_prediction_change(test_db):
    test_db.add_all([
        FundInfo(fund_code="OLD01", fund_name="旧基金"),
        FundInfo(fund_code="NEW01", fund_name="已审核基金"),
    ])
    blogger, post = _blogger_post(test_db, "正式映射博主")
    prediction = _prediction(
        test_db,
        blogger,
        post,
        fund_code="OLD01",
        sector="白酒",
        verified=True,
    )
    test_db.add(SectorFundMapping(
        sector_name="白酒",
        fund_code="NEW01",
        fund_name="已审核基金",
        reviewed=True,
        is_active=True,
    ))
    test_db.commit()

    result = PredictionMaintenanceService(test_db).sync_sector_mappings(dry_run=False)

    assert result["predictions_updated"] == 1
    log = test_db.query(PredictionChangeLog).one()
    assert log.action == "maintenance_sync"
    assert log.source == "sector_mapping"
    assert log.before_state["fund_code"] == "OLD01"
    assert log.after_state["fund_code"] == "NEW01"
    assert log.before_state["status"] == "success"
    assert log.after_state["status"] == "pending"


def test_rollback_invalid_dry_run_preserves_verification(monkeypatch, test_db):
    blogger, post = _blogger_post(test_db, "回溯博主")
    prediction = _prediction(test_db, blogger, post, verified=True)
    test_db.commit()
    service = PredictionVerifyService(test_db)
    monkeypatch.setattr(service, "match_fund_for_prediction", lambda value: ("DUP01", "重复测试基金"))
    monkeypatch.setattr(service, "_check_fund_data_availability", lambda **kwargs: {
        "available": False,
        "message": "净值数据不足",
        "data_points": 1,
    })

    result = service.rollback_invalid_verifications(min_data_points=2, dry_run=True)

    assert result["data"]["would_rollback"] == 1
    assert result["data"]["rolled_back"] == 0
    test_db.refresh(prediction)
    assert prediction.status == "success"
    assert prediction.verify_count == 1
    assert prediction.verify_history == [{"date": "2026-07-08", "score": 80}]


def test_rollback_invalid_execution_records_prediction_change(monkeypatch, test_db):
    blogger, post = _blogger_post(test_db, "正式回溯博主")
    prediction = _prediction(test_db, blogger, post, verified=True)
    test_db.commit()
    service = PredictionVerifyService(test_db)
    monkeypatch.setattr(
        service,
        "match_fund_for_prediction",
        lambda value: ("DUP01", "重复测试基金"),
    )
    monkeypatch.setattr(service, "_check_fund_data_availability", lambda **kwargs: {
        "available": False,
        "message": "净值数据不足",
        "data_points": 1,
    })

    result = service.rollback_invalid_verifications(min_data_points=2, dry_run=False)

    assert result["data"]["rolled_back"] == 1
    log = test_db.query(PredictionChangeLog).one()
    assert log.action == "verification_rollback"
    assert log.source == "maintenance"
    assert log.before_state["status"] == "success"
    assert log.after_state["status"] == "pending"


def _request(headers=None):
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw_headers})


def test_mapping_execute_route_requires_confirmation(test_db):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(prediction_routes.sync_sector_mapping(
            request=_request(),
            dry_run=False,
            db=test_db,
        ))

    assert exc.value.status_code == 403
