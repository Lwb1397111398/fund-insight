"""预测生命周期推导与统一查询入口测试。"""
from datetime import date, timedelta

from src.models.database import Blogger, Post, Prediction
from src.services import prediction_lifecycle as lc
from src.services.prediction_lifecycle import (
    ACTIVE,
    DUE_UNVERIFIED,
    UNVERIFIABLE,
    VERIFIED_CORRECT,
    VERIFIED_INCORRECT,
    INCOMPLETE,
    DELETED,
    classify,
    is_expired_computed,
    filter_actionable_current,
    filter_due_for_verify,
    filter_unverifiable,
    max_end_nav_age_days,
)


def _seed_pred(
    db,
    *,
    target,
    is_correct=None,
    status="pending",
    is_deleted=False,
    verify_count=0,
    pred_type="up",
    fund_code="LC001",
):
    blogger = db.query(Blogger).filter(Blogger.name == "生命周期博主").first()
    if not blogger:
        blogger = Blogger(name="生命周期博主", platform="wechat")
        db.add(blogger)
        db.flush()
    post = Post(
        blogger_id=blogger.id,
        title="lc",
        content="lc",
        post_date=target - timedelta(days=7) if target else date(2026, 1, 1),
    )
    db.add(post)
    db.flush()
    p = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code=fund_code,
        fund_name="生命周期基金",
        sector="测试",
        prediction_type=pred_type,
        prediction_date=post.post_date,
        prediction_period="1周",
        target_date=target,
        status=status,
        is_correct=is_correct,
        verify_count=verify_count,
        is_deleted=is_deleted,
        is_expired=False,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_classify_verified_only_from_is_correct(test_db):
    """status=failed 但 is_correct null 不得当成 verified_incorrect。"""
    as_of = date(2026, 2, 1)
    # 验证尝试失败类：status 可能被误标，但无结论
    p_bad_status = _seed_pred(
        test_db,
        target=date(2026, 1, 28),  # 距 as_of=2/1 仅 4 天，仍在窗口内
        is_correct=None,
        status="failed",
        verify_count=3,
    )
    assert classify(p_bad_status, as_of=as_of) == DUE_UNVERIFIED

    p_wrong = _seed_pred(
        test_db,
        target=date(2026, 1, 15),
        is_correct=False,
        status="failed",
        verify_count=1,
    )
    assert classify(p_wrong, as_of=as_of) == VERIFIED_INCORRECT

    p_right = _seed_pred(
        test_db,
        target=date(2026, 1, 15),
        is_correct=True,
        status="success",
        verify_count=1,
    )
    assert classify(p_right, as_of=as_of) == VERIFIED_CORRECT


def test_target_equals_as_of_is_due_not_active(test_db):
    as_of = date(2026, 3, 10)
    p = _seed_pred(test_db, target=as_of, is_correct=None)
    assert classify(p, as_of=as_of) == DUE_UNVERIFIED
    assert is_expired_computed(p, as_of=as_of) is True


def test_active_and_actionable_filters(test_db, monkeypatch):
    as_of = date(2026, 3, 1)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)

    active_near = _seed_pred(test_db, target=as_of + timedelta(days=3))
    active_mid = _seed_pred(test_db, target=as_of + timedelta(days=15))
    due_today = _seed_pred(test_db, target=as_of)
    future_far = _seed_pred(test_db, target=as_of + timedelta(days=40))

    assert classify(active_near, as_of=as_of) == ACTIVE
    assert classify(due_today, as_of=as_of) == DUE_UNVERIFIED

    actionable = filter_actionable_current(test_db, as_of=as_of)
    ids = {p.id for p in actionable}
    assert active_near.id in ids
    assert active_mid.id in ids
    assert due_today.id not in ids
    assert future_far.id not in ids  # 超过 mid 30 天窗口


def test_due_queue_has_no_time_ceiling(test_db, monkeypatch):
    """到期未验证即可验：再旧的预测也是 due_unverified，不存在「超过窗口不可验证」。"""
    as_of = date(2026, 4, 20)
    max_age = max_end_nav_age_days()
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)

    due = _seed_pred(test_db, target=as_of - timedelta(days=2))
    very_old = _seed_pred(
        test_db, target=as_of - timedelta(days=max_age + 100)
    )  # 远超历史窗口下限，仍可验证
    # 数据不足多次尝试：is_correct 仍 null，status 即使 failed 也是 due
    attempted = _seed_pred(
        test_db,
        target=as_of - timedelta(days=1),
        is_correct=None,
        status="failed",
        verify_count=5,
    )

    assert classify(due, as_of=as_of) == DUE_UNVERIFIED
    assert classify(very_old, as_of=as_of) == DUE_UNVERIFIED
    assert classify(attempted, as_of=as_of) == DUE_UNVERIFIED

    due_ids = {p.id for p in filter_due_for_verify(test_db, as_of=as_of)}
    assert due.id in due_ids
    assert attempted.id in due_ids
    assert very_old.id in due_ids  # 时间上限已取消

    assert filter_unverifiable(test_db, as_of=as_of) == []


def test_incomplete_and_deleted(test_db):
    as_of = date(2026, 5, 1)
    incomplete = _seed_pred(test_db, target=None)
    deleted = _seed_pred(test_db, target=as_of + timedelta(days=2), is_deleted=True)
    assert classify(incomplete, as_of=as_of) == INCOMPLETE
    assert classify(deleted, as_of=as_of) == DELETED


def test_advice_and_verify_share_lifecycle_boundary(test_db, monkeypatch):
    """建议选数与验证待办消费同一推导边界，不交叉。"""
    as_of = date(2026, 6, 1)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)

    active = _seed_pred(test_db, target=as_of + timedelta(days=5))
    due = _seed_pred(test_db, target=as_of - timedelta(days=1))

    actionable = {p.id for p in filter_actionable_current(test_db, as_of=as_of)}
    due_set = {p.id for p in filter_due_for_verify(test_db, as_of=as_of)}

    assert active.id in actionable and active.id not in due_set
    assert due.id in due_set and due.id not in actionable


def test_is_expired_computed_ignores_stored_flag(test_db):
    as_of = date(2026, 7, 1)
    p = _seed_pred(test_db, target=as_of + timedelta(days=3))
    p.is_expired = True  # 脏标志
    test_db.commit()
    # 计算值看 target，不看列
    assert is_expired_computed(p, as_of=as_of) is False
    assert classify(p, as_of=as_of) == ACTIVE
