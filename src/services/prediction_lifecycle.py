"""
预测生命周期（推导型单一事实来源）

状态机回答「到没到期 / 验没验过 / 是否已错过窗口」；
净值就绪规则回答「现在能不能验」。两者互不越界。

verified_* 只由 is_correct 推导，绝不由 status 推导
（status=failed 可能表示方向判错，也可能与历史脏数据混用；
 验证尝试失败不会写 is_correct，应保持 due_unverified）。

`unverifiable` 也**不是**日历推出来的：它是验证器上一次真问过数据源、
数据源答"这段区间给不出净值"之后写下的重问锁（`next_verify_date`），
到点自己回队 ⇒ 既不为"到期很久了"编造结论，也不让同一批白跑每一天。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Sequence

from sqlalchemy import or_
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
    except Exception as exc:                      # 容器里没装 tzdata 就会走到这里
        # 第 25 轮 B：这条回退**静默**把"截至日"退回系统时钟 ⇒ 本函数想修的东西又坏了，
        # 而没人知道。至少留一行日志，让"生产上这条修复到底生效没有"可查。
        logging.getLogger(__name__).warning(
            '[as_of] 取不到 Asia/Shanghai（%s），退回系统时钟 date.today()：'
            '容器缺 tzdata 时页面日期可能差一天', exc)
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
    """
    结束净值允许早于 target_date 的最大自然日数（陈旧度）。

    仅衡量「结束净值距目标日有多远」，与「当前距目标日多久」无关；
    到期预测不会因为今天离目标日很远而不可验证——只要区间内净值数据还在就能验证。
    """
    return int(getattr(config, "VERIFY_MAX_END_NAV_AGE_DAYS", 10))


def verify_window_end(target: date, max_age: Optional[int] = None) -> date:
    """
    合规 end NAV 的取数范围仍须 nav_date <= target（不用目标日之后的行情）；
    该值也允许比 target 早至多 max_age 天（周末/假日取前值）。

    注：这不是「今天还能不能验证」的截止——时间上没有截止。
    """
    age = max_end_nav_age_days() if max_age is None else int(max_age)
    return target + timedelta(days=age)


def unverifiable_retry_days() -> int:
    """结构性不可验的重问间隔（天）。

    **只有这一处定义它**，且它不是拍出来的数：数据源答"这段没有"的凭据 TTL 是
    `backfill_proofs.EMPTY_TTL_DAYS`（空答复只信 2 天，防限流页被当成事实），
    重问间隔取 TTL+1 ⇒ 凭据一旦过期，这条预测自己回到到期队列再问一次。
    写在这里而不写死，是为了让"锁多久"永远跟着"凭据可信多久"走。

    懒导入：本模块被 API 与脚本共读，顶层拉 `src.fund` 会把整个包 __init__ 带进来。
    """
    from src.fund import backfill_proofs

    return int(backfill_proofs.EMPTY_TTL_DAYS) + 1


def hold_until(prediction: Prediction) -> Optional[date]:
    """这条预测被压到哪天不再重问；没有被压 ⇒ None（不判到期与否，由调用方看）。"""
    return _as_date(getattr(prediction, "next_verify_date", None))


def is_held_unverifiable(prediction: Prediction, as_of: Optional[date] = None) -> bool:
    """已到目标日、但被验证器压着不再重问（＝结构性不可验中）。

    判据是**验证器上一次真问出来的结论**（见 `apply_unverifiable_hold`），
    不是日历推断：`next_verify_date` 由创建时的排期保证 ≤ 目标日，
    所以"晚于今天"这个形状只可能由结构性结论写出来。
    """
    today = _as_date(as_of) or current_as_of()
    target = _as_date(getattr(prediction, "target_date", None))
    hold = hold_until(prediction)
    return (target is not None and hold is not None
            and target <= today and hold > today
            and getattr(prediction, "is_correct", None) is None
            and not getattr(prediction, "is_deleted", False))


def apply_unverifiable_hold(prediction: Prediction, as_of: Optional[date] = None) -> date:
    """验证器判定"已问过数据源、它给不出这段净值"后，把这条压到重问日。

    返回压到的那一天。**不动 `is_correct`、不清结论** —— 它只回答"什么时候再问"，
    不回答"预测对不对"。
    """
    today = _as_date(as_of) or current_as_of()
    hold = today + timedelta(days=unverifiable_retry_days())
    prediction.next_verify_date = hold
    return hold


def release_unverifiable_hold(prediction: Prediction,
                              as_of: Optional[date] = None) -> bool:
    """数据又够用了（补拉成功、历史被回补）⇒ 撤掉重问锁，回到正常排期。

    只撤"由结构性结论写下的那一档"：`next_verify_date` 落在创建期排不出来的区间
    （晚于今天）才撤；已经在未来的正常排期不关这条路的事。返回是否真的撤了。
    """
    today = _as_date(as_of) or current_as_of()
    hold = hold_until(prediction)
    if hold is None or hold <= today:
        return False
    prediction.next_verify_date = None
    return True


def classify(
    prediction: Prediction,
    as_of: Optional[date] = None,
    max_age_days: Optional[int] = None,  # noqa: ARG001 保留签名兼容，不再参与时间闸门
) -> str:
    """
    推导单条预测生命周期。

    优先级：
    1. deleted
    2. incomplete（无 target_date）
    3. verified_*（仅 is_correct is not None）
    4. active / due_unverified / unverifiable（按 target 是否已过、是否被重问锁压着）

    注：**没有**"超过 N 天不可验证"这种日历推断 —— 净值数据在就能验。
    `unverifiable` 只由验证器真问出来的结构性结论写（`no_source_history`），
    并且到期自动重问（`unverifiable_retry_days`），所以它不是终态、是"今天问过了，别再白跑"。
    """
    as_of = _as_date(as_of) or current_as_of()

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

    # target <= as_of：到期未验证即可验，没有"过期不可验证"这一说；
    # 唯一的例外是验证器真问过、数据源答"这段给不出"⇒ 压到重问日之前不算白跑。
    if is_held_unverifiable(prediction, as_of=as_of):
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
    max_age_days: Optional[int] = None,  # noqa: ARG001 保留签名兼容，不再限制目标日下限
) -> List[Prediction]:
    """
    到期待验证队列：due_unverified。

    - target_date <= as_of
    - is_correct is null
    - 无时间上限：到期未验证即可验，再旧的也入队（按目标日升序，最旧最前）
    - 默认排除 flat（与现有 verify_all_pending 一致）
    """
    as_of = _as_date(as_of) or current_as_of()

    filters = [
        Prediction.is_deleted == False,
        Prediction.target_date.isnot(None),
        Prediction.is_correct.is_(None),
        Prediction.target_date <= as_of,
        # 被重问锁压着的先不入队（同 classify 那一支；SQL 先筛掉，省得整批拉回内存）
        or_(Prediction.next_verify_date.is_(None), Prediction.next_verify_date <= as_of),
    ]
    if exclude_flat:
        filters.append(Prediction.prediction_type != "flat")

    rows = (
        db.query(Prediction)
        .filter(*filters)
        .order_by(Prediction.target_date.asc())
        .all()
    )
    return [p for p in rows if classify(p, as_of=as_of) == DUE_UNVERIFIED]


def due_skip_reason(prediction: Prediction, as_of: Optional[date] = None) -> Optional[str]:
    """已到期但未进入验证队列的原因；可验证时返回 None。

    与 filter_due_for_verify 同口径，用于向用户解释"为什么不验证"。
    到期未验证没有"超过时间不可验证"一说——只有观望预测、以及**验证器真问过之后
    数据源答"这段没有"**的那批会被暂时压住（到重问日自动回队）。
    """
    today = _as_date(as_of) or current_as_of()
    if getattr(prediction, "prediction_type", None) == "flat":
        return "中性预测（观望）不参与验证"
    if is_held_unverifiable(prediction, as_of=today):
        hold = hold_until(prediction)
        return (f"已问过数据源，{prediction.fund_code} 在目标日那段给不出净值 ⇒ "
                f"属结构性不可验，{hold.isoformat()} 之前不再重问（到点自动回队再问一次）")
    return None


def filter_unverifiable(
    db: Session,
    as_of: Optional[date] = None,
    *,
    max_age_days: Optional[int] = None,  # noqa: ARG001 保留签名兼容
) -> List[Prediction]:
    """「结构性不可验、当前被压着重问」集合。

    判据不是日历（再旧的预测只要净值在就能验），而是**验证器上一次真问过、
    数据源答这段给不出** ⇒ 行上留下一个晚于今天的 `next_verify_date`。
    它到点自己回到到期队列再问一次，所以这里数的是"今天别再为它白跑"，不是终态。
    """
    today = _as_date(as_of) or current_as_of()

    rows = (
        db.query(Prediction)
        .filter(
            Prediction.is_deleted == False,
            Prediction.target_date.isnot(None),
            Prediction.is_correct.is_(None),
            Prediction.target_date <= today,
            Prediction.next_verify_date.isnot(None),
            Prediction.next_verify_date > today,
        )
        .order_by(Prediction.target_date.asc())
        .all()
    )
    return [p for p in rows if classify(p, as_of=today) == UNVERIFIABLE]


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
