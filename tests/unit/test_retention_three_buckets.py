"""三桶 v2：verified 护栏、观点锚点、全局上限、dry-run 默认。"""
from datetime import date, datetime, timedelta

from src.models.database import Blogger, CleanupItemLog, Post, Prediction, Viewpoint
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


def test_verified_ledger_never_in_deleted_bucket(test_db):
    blogger, post = _seed_blogger_post(test_db)
    bare = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB001",
        prediction_type="bullish",
        prediction_content="no conclusion",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
        is_correct=None,
    )
    verified = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB002",
        prediction_type="bullish",
        prediction_content="has conclusion",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
        is_correct=False,
        verify_count=1,
    )
    test_db.add_all([bare, verified])
    test_db.commit()

    plan = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29)).build_plan()
    assert bare.id in plan.candidate_ids["deleted_predictions"]
    assert verified.id not in plan.candidate_ids["deleted_predictions"]
    assert plan.protected_counts["verified_ledger_excluded"] >= 1


def test_execute_blocks_if_verified_slips_into_plan(test_db):
    blogger, post = _seed_blogger_post(test_db)
    verified = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="TB003",
        prediction_type="bullish",
        prediction_content="ledger",
        prediction_date=date(2026, 1, 1),
        target_date=date(2026, 2, 1),
        status="pending",
        is_deleted=True,
        deleted_at=datetime(2026, 1, 10),
        is_correct=True,
        verify_count=1,
    )
    test_db.add(verified)
    test_db.commit()

    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    plan = svc.build_plan()
    # 人为污染 plan
    plan.candidate_ids["deleted_predictions"] = [verified.id]
    try:
        svc.execute(dry_run=False, confirm_token=CONFIRM_TOKEN, plan=plan)
        assert False, "should block"
    except RuntimeError as exc:
        assert "verified ledger" in str(exc)


def test_global_cap_truncates_across_buckets(test_db):
    blogger, post = _seed_blogger_post(test_db)
    for i in range(5):
        test_db.add(
            Prediction(
                post_id=post.id,
                blogger_id=blogger.id,
                fund_code=f"CAP{i}",
                prediction_type="bullish",
                prediction_content="x",
                prediction_date=date(2026, 1, 1),
                target_date=date(2026, 2, 1),
                status="pending",
                is_deleted=True,
                deleted_at=datetime(2026, 1, 10),
                is_correct=None,
            )
        )
    test_db.commit()
    policy = ThreeBucketPolicy(max_total_per_run=2, max_per_bucket=10)
    plan = ThreeBucketRetentionService(
        test_db, today=date(2026, 7, 29), policy=policy
    ).build_plan()
    assert plan.total == 2
    assert plan.truncated is True


def test_soft_deleted_viewpoint_uses_deleted_at_not_valid_until(test_db):
    blogger, post = _seed_blogger_post(test_db)
    # valid_until 很远，但 deleted_at 已满 30 天 → 应进候选
    vp = Viewpoint(
        blogger_id=blogger.id,
        post_id=post.id,
        content="soft deleted vp",
        author="t",
        source="test",
        viewpoint_date=date(2026, 7, 1),
        valid_until=date(2026, 12, 31),
        is_deleted=True,
        deleted_at=datetime(2026, 6, 1),
        is_summary=False,
    )
    test_db.add(vp)
    test_db.commit()
    plan = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29)).build_plan()
    assert vp.id in plan.candidate_ids["deleted_viewpoints"]


def test_summary_uses_viewpoint_date_window(test_db):
    blogger, post = _seed_blogger_post(test_db)
    old = Viewpoint(
        blogger_id=blogger.id,
        content="old summary",
        author="系统",
        source="daily_summary",
        viewpoint_date=date(2026, 3, 1),
        valid_until=date(2026, 12, 31),  # 故意拉长
        is_summary=True,
        is_deleted=False,
    )
    recent = Viewpoint(
        blogger_id=blogger.id,
        content="recent summary",
        author="系统",
        source="daily_summary",
        viewpoint_date=date(2026, 7, 1),
        valid_until=date(2026, 12, 31),
        is_summary=True,
        is_deleted=False,
    )
    test_db.add_all([old, recent])
    test_db.commit()
    plan = ThreeBucketRetentionService(
        test_db,
        today=date(2026, 7, 29),
        policy=ThreeBucketPolicy(summary_viewpoint_days=90),
    ).build_plan()
    assert old.id in plan.candidate_ids["summary_viewpoints"]
    assert recent.id not in plan.candidate_ids["summary_viewpoints"]


def test_dry_run_default_and_token(test_db):
    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    r = svc.execute(dry_run=True)
    assert r["mode"] == "dry-run"
    try:
        svc.execute(dry_run=False, confirm_token="nope")
        assert False
    except PermissionError:
        pass


def test_execute_deletes_unverified_soft_delete_only(test_db):
    blogger, post = _seed_blogger_post(test_db)
    bare = Prediction(
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
        is_correct=None,
    )
    test_db.add(bare)
    test_db.commit()
    bare_id = bare.id
    svc = ThreeBucketRetentionService(test_db, today=date(2026, 7, 29))
    result = svc.execute(dry_run=False, confirm_token=CONFIRM_TOKEN)
    assert result["deleted_counts"]["deleted_predictions"] >= 1
    assert test_db.query(Prediction).filter(Prediction.id == bare_id).first() is None
