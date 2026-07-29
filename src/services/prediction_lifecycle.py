"""
预测生命周期（推导型单一事实来源）

状态机回答「到没到期 / 验没验过 / 是否已错过窗口」；
净值就绪规则回答「现在能不能验」。两者互不越界。

verified_* 只由 is_correct 推导，绝不由 status 推导
（status=failed 可能表示方向判错，也可能与历史脏数据混用；
 验证尝试失败不会写 is_correct，应保持 due_unverified）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session

from src.core.config import config
from src.models.database import Prediction

# 生命周期枚举（字符串常量，便于 JSON / API）
DELETED = "deleted"
INCOMPLETE = "incomplete"
ACTIVE = "active"
DUE_UNVERIFIED = "due_unverified"
UNVERIFIABLE = "unverifiable"
VERIFIED_CORRECT = "verified_correct"
VERIFIED_INCORRECT = "verified_incorrect"

ALL_LIFECYCLES = (
    DELETED,
    INCOMPLETE,
    ACTIVE,
    DUE_UNVERIFIED,
    UNVERIFIABLE,
    VERIFIED_CORRECT,
    VERIFIED_INCORRECT,
)


def current_as_of() -> date:
    """统一 as_of 入口（北京时间自然日，失败时回退 date.today()）。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).date()
    except Exception:
        return date.today()


def _as_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def max_end_nav_age_days() -> int:
    """与净值就绪规则共用同一配置，不另写魔法数。"""
    return int(getattr(config, "VERIFY_MAX_END_NAV_AGE_DAYS", 10))


def verify_window_end(target: date, max_age: Optional[int] = None) -> date:
    """
    合规 end NAV 仍须 nav_date <= target；
    但「是否还可能等到合规数据」的截止为 target + max_age（自然日）。
    """
    age = max_end_nav_age_days() if max_age is None else int(max_age)
    return target + timedelta(days=age)


def classify(
    prediction: Prediction,
    as_of: Optional[date] = None,
    max_age_days: Optional[int] = None,
) -> str:
    """
    推导单条预测生命周期。

    优先级：
    1. deleted
    2. incomplete（无 target_date）
    3. verified_*（仅 is_correct is not None）
    4. active / due_unverified / unverifiable（按 target 与窗口）
    """
    as_of = _as_date(as_of) or current_as_of()
    max_age = max_end_nav_age_days() if max_age_days is None else int(max_age_days)

    if getattr(prediction, "is_deleted", False):
        return DELETED

    target = _as_date(getattr(prediction, "target_date", None))
    if target is None:
        return INCOMPLETE

    is_correct = getattr(prediction, "is_correct", None)
    if is_correct is True:
        return VERIFIED_CORRECT
    if is_correct is False:
        return VERIFIED_INCORRECT

    # 以下均为未验证（is_correct is null）
    if target > as_of:
        return ACTIVE

    # target <= as_of
    if as_of > verify_window_end(target, max_age):
        return UNVERIFIABLE

    return DUE_UNVERIFIED


def is_expired_computed(prediction: Prediction, as_of: Optional[date] = None) -> bool:
    """
    API 兼容用的计算值：target_date <= as_of。
    不读存储列 is_expired（该列已被证明不可靠）。
    """
    as_of = _as_date(as_of) or current_as_of()
    target = _as_date(getattr(prediction, "target_date", None))
    if target is None:
        return False
    return target <= as_of


def filter_actionable_current(
    db: Session,
    as_of: Optional[date] = None,
    *,
    near_days: int = 7,
    mid_days: int = 30,
    mid_limit: int = 20,
    exclude_flat: bool = False,
) -> List[Prediction]:
    """
    当前可行动预测（建议方向信号）。

    语义：target_date > as_of（当天到期已退出方向信号，进入 due）。
    近端 (as_of, as_of+near_days]；中端 (as_of+near_days, as_of+mid_days]，中端有条数上限。
    """
    as_of = _as_date(as_of) or current_as_of()
    near_end = as_of + timedelta(days=near_days)
    mid_end = as_of + timedelta(days=mid_days)

    base = [
        Prediction.is_deleted == False,
        Prediction.target_date.isnot(None),
        Prediction.is_correct.is_(None),  # 未验证结论；已验证的不再当方向信号
        Prediction.target_date > as_of,
    ]
    if exclude_flat:
        base.append(Prediction.prediction_type != "flat")

    near = (
        db.query(Prediction)
        .filter(
            *base,
            Prediction.target_date <= near_end,
        )
        .order_by(Prediction.target_date.asc())
        .all()
    )

    mid = (
        db.query(Prediction)
        .filter(
            *base,
            Prediction.target_date > near_end,
            Prediction.target_date <= mid_end,
        )
        .order_by(Prediction.target_date.asc())
        .limit(mid_limit)
        .all()
    )

    # 防御：再用 classify 过滤（防止查询条件与推导漂移）
    out: List[Prediction] = []
    for p in near + mid:
        if classify(p, as_of=as_of) == ACTIVE:
            out.append(p)
    return out


def filter_due_for_verify(
    db: Session,
    as_of: Optional[date] = None,
    *,
    exclude_flat: bool = True,
    max_age_days: Optional[int] = None,
) -> List[Prediction]:
    """
    到期待验证队列：due_unverified。

    - target_date <= as_of
    - is_correct is null
    - 仍在验证窗口内（未进入 unverifiable）
    - 默认排除 flat（与现有 verify_all_pending 一致）
    """
    as_of = _as_date(as_of) or current_as_of()
    max_age = max_end_nav_age_days() if max_age_days is None else int(max_age_days)
    window_floor = as_of - timedelta(days=max_age)

    filters = [
        Prediction.is_deleted == False,
        Prediction.target_date.isnot(None),
        Prediction.is_correct.is_(None),
        Prediction.target_date <= as_of,
        # target + max_age >= as_of  ⟺  target >= as_of - max_age
        Prediction.target_date >= window_floor,
    ]
    if exclude_flat:
        filters.append(Prediction.prediction_type != "flat")

    rows = (
        db.query(Prediction)
        .filter(*filters)
        .order_by(Prediction.target_date.asc())
        .all()
    )
    return [p for p in rows if classify(p, as_of=as_of, max_age_days=max_age) == DUE_UNVERIFIED]


def filter_unverifiable(
    db: Session,
    as_of: Optional[date] = None,
    *,
    max_age_days: Optional[int] = None,
) -> List[Prediction]:
    """已错过验证窗口、永久不可验证。"""
    as_of = _as_date(as_of) or current_as_of()
    max_age = max_end_nav_age_days() if max_age_days is None else int(max_age_days)
    window_floor = as_of - timedelta(days=max_age)

    rows = (
        db.query(Prediction)
        .filter(
            Prediction.is_deleted == False,
            Prediction.target_date.isnot(None),
            Prediction.is_correct.is_(None),
            Prediction.target_date < window_floor,
        )
        .all()
    )
    return [
        p
        for p in rows
        if classify(p, as_of=as_of, max_age_days=max_age) == UNVERIFIABLE
    ]


def count_by_lifecycle(
    predictions: Iterable[Prediction],
    as_of: Optional[date] = None,
) -> dict:
    """对内存中的预测集合按 lifecycle 计数（测试/诊断用）。"""
    as_of = _as_date(as_of) or current_as_of()
    counts = {k: 0 for k in ALL_LIFECYCLES}
    for p in predictions:
        counts[classify(p, as_of=as_of)] = counts.get(classify(p, as_of=as_of), 0) + 1
    return counts
