"""三桶保留策略：候选规则与 dry-run 默认。"""
from datetime import date, datetime, timedelta

from src.models.database import Blogger, CleanupItemLog, Post, Prediction
from src.services.retention_three_buckets import (
    CONFIRM_TOKEN,
    ThreeBucketPolicy,
    ThreeBucketRetentionService,
)


def _seed_blogger_post(db):
    b = Blogger(name="三桶测试博主", platform="test")
    db.add(b)
    db.flush()
    p = Post(
        blogger_id=b.id,
        content="three-bucket",
        post_date=date(2026, 1, 1),
        analyzed=True,
    )
    db.add(p)
    db.flush()
    return b, p


def test_deleted_bucket_selects_old_soft_deletes_only(test_db):
    blogger, post = _seed_blogger_post(test_db)
    old = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB001",
        prediction_type="bullish",
        prediction_content="old deleted",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
    )
    recent = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB002",
        prediction_type="bullish",
        prediction_content="recent deleted",
        prediction_date=date(2026, 7, 1),
        target_date=date(2026, 8, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 7, 20),
    )
    alive = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB003",
        prediction_type="bullish",
        prediction_content="alive",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=False,
    )
    test_db.add_all([old, recent, alive])
    test_db.commit()

    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    plan = svc.build_plan()
    assert old.id in plan.candidate_ids["deleted_predictions"]
    assert recent.id not in plan.candidate_ids["deleted_predictions"]
    assert alive.id not in plan.candidate_ids["deleted_predictions"]


def test_dry_run_does_not_delete(test_db):
    blogger, post = _seed_blogger_post(test_db)
    old = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB010",
        prediction_type="bullish",
        prediction_content="old deleted",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
    )
    test_db.add(old)
    test_db.commit()

    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    before = test_db.query(Prediction).count()
    result = svc.execute(dry_run=True)
    assert result["mode"] == "dry-run"
    assert result["deleted_counts"]["deleted_predictions"] == 0
    assert test_db.query(Prediction).count() == before


def test_execute_requires_confirm_token(test_db):
    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    try:
        svc.execute(dry_run=False, confirm_token="wrong")
        assert False, "should raise"
    except PermissionError:
        pass


def test_execute_hard_deletes_with_token(test_db):
    blogger, post = _seed_blogger_post(test_db)
    old = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB020",
        prediction_type="bullish",
        prediction_content="old deleted",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
    )
    test_db.add(old)
    test_db.commit()
    old_id = old.id

    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    result = svc.execute(dry_run=False, confirm_token=CONFIRM_TOKEN)
    assert result["mode"] == "execute"
    assert result["deleted_counts"]["deleted_predictions"] >= 1
    assert test_db.query(Prediction).filter(Prediction.id == old_id).first() is None


def test_cleanup_item_logs_bucket_age(test_db):
    old_log = CleanupItemLog(
        log_id=1,
        data_type="prediction",
        data_id=1,
        action="delete",
        reason="test",
        created_at=datetime(2026, 1, 1),
        deleted_at=datetime(2026, 1, 1),
    )
    new_log = CleanupItemLog(
        log_id=1,
        data_type="prediction",
        data_id=2,
        action="delete",
        reason="test",
        created_at=datetime(2026, 7, 28),
        deleted_at=datetime(2026, 7, 28),
    )
    test_db.add_all([old_log, new_log])
    test_db.commit()

    svc = ThreeBucketRetentionService(
        test_db,
        today=date(2026, 7, 29),
        policy=ThreeBucketPolicy(cleanup_item_log_days=90),
    )
    plan = svc.build_plan()
    assert old_log.id in plan.candidate_ids["cleanup_item_logs"]
    assert new_log.id not in plan.candidate_ids["cleanup_item_logs"]


def test_unverifiable_requires_full_age_window(test_db, monkeypatch):
    """未满 90 天的 unverifiable 不进桶（策略先行）。"""
    blogger, post = _seed_blogger_post(test_db)
    # target 55 天前：lifecycle 可为 unverifiable，但三桶要求 90
    p = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB030",
        prediction_type="bullish",
        prediction_content="stale",
        prediction_date=date(2026, 4, 1),
        target_date=date(2026, 6, 1),  # as_of 7-29 → 58 天
        status="pending",
        is_deleted=False,
        is_correct=None,
    )
    test_db.add(p)
    test_db.commit()

    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    plan = svc.build_plan()
    assert p.id not in plan.candidate_ids["unverifiable_predictions"]
