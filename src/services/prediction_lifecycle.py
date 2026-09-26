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
from typing import Dict, Iterable, List, Optional, Sequence

from sqlalchemy import func, or_
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


def should_close_as_stale_target(*, verdict_reason: Optional[str],
                                 previous_hold: Optional[date],
                                 local_latest_nav: Optional[date],
                                 window_start: Optional[date],
                                 today: Optional[date] = None) -> bool:
    """这条预测要不要从"重问锁"升级成**关闭**（标的已经停更，永远问不出答案）。

    两个条件必须同时成立，缺一个都不许关（关 = 从活跃列表消失，代价比锁大得多）：
    ① **同一个窗口已经问过两次、两次都被数据源答"没有"**：`previous_hold` 是上一轮留下的
       重问日，只有它已经过去（≤ 今天）才说明这次是回队之后的第二次答案。一次答"没有"
       可能撞上源端抽风；`no_source_history` 本身又只在"源端真答了 0 条"时才出现
       （传输失败/限流一律不记凭据，见 `backfill_proofs` 第 B-1 条），所以两次答案
       是两次独立的、来自源端的否定。
    ② **这只产品在我们库里连窗口开始之后都没发过一条净值**：`local_latest_nav < window_start`
       ⇒ 它不是"最近几天没同步"，是从头到尾就没 publish 过这段。少了这一条，
       我们自己同步掉几天就可能把一条本可验证的预测关掉（第 23 轮那种"把镜像坏了当产品坏了"的坑）。
    """
    if verdict_reason != 'no_source_history':
        return False
    today = _as_date(today) or current_as_of()
    prev = _as_date(previous_hold)
    if prev is None or prev > today:
        return False                      # 第一次判出来：只锁，不动行
    return nav_cannot_cover_window(local_latest_nav, window_start)


def nav_cannot_cover_window(local_latest_nav: Optional[date],
                            window_start: Optional[date]) -> bool:
    """这把标的的净值**覆盖不了**这段窗口 —— 库里末条净值早于窗口起点。

    只此一处实现这句话：验证器判"要不要关"、改标门判"这段窗口它给不给得出证据"
    （`target_cannot_evidence_window` 的第一道）、存量收口脚本判"这一行进不进计划"，
    三处问的都是同一句话。同步只补**没有的日期**、从不覆盖已有行，
    所以末条停在窗口之前 = 源端不再给这只产品发新行，等下去也不会有答案。
    两个日期任一说不清 ⇒ 返回 False（不敢下结论，交回给"继续问"那条路）。
    """
    latest, start = _as_date(local_latest_nav), _as_date(window_start)
    return latest is not None and start is not None and latest < start


def nav_calendar(db: Session, codes: Iterable[str]) -> Dict[str, List[date]]:
    """一次把若干标的的**净值日历**读出来：`代码 -> 升序净值日期列表`。

    为什么要有它而不是每条预测各查一次：判"绑过去之后判得出来吗"要问窗口里有几个点、
    终点离目标日几天，按行查就是第 51 轮"生产 100 秒不返回"那一族（那次是板块别名表）。
    调用方把整批要用的代码一次递进来，之后每条候选只在内存里比。
    """
    from src.models.database import FundHistory

    wanted = sorted({c for c in codes if c})
    out: Dict[str, List[date]] = {}
    if not wanted:
        return out
    for code, nav_date in db.query(FundHistory.fund_code, FundHistory.nav_date).filter(
            FundHistory.fund_code.in_(wanted)).order_by(
                    FundHistory.fund_code.asc(), FundHistory.nav_date.asc()).all():
        day = _as_date(nav_date)
        if day is not None:
            out.setdefault(code, []).append(day)
    return out


def window_evidence(db: Session, fund_code: Optional[str],
                    window_start: Optional[date],
                    window_end: Optional[date]) -> tuple:
    """单行版的取数：`(这段窗口里的净值日, 这只标的在库里最后一笔净值日)`。

    改标门一次只判一条时用它 —— 只把窗口内那段读进内存，不把整只标的的历史搬回来。
    批量判（「按板块对齐标的」的预览与执行）不许在循环里调它 ⇒ 用 `nav_calendar`
    读一次、在内存里切。两条路最后都交给 `target_cannot_evidence_window` 同一把尺子。
    """
    from src.models.database import FundHistory

    start, end = _as_date(window_start), _as_date(window_end)
    if not fund_code or start is None:
        return [], None
    latest = _as_date(db.query(func.max(FundHistory.nav_date)).filter(
        FundHistory.fund_code == fund_code).scalar())
    upper = end or latest
    if latest is None or upper is None or start > upper:
        return [], latest
    rows = db.query(FundHistory.nav_date).filter(
        FundHistory.fund_code == fund_code,
        FundHistory.nav_date >= start,
        FundHistory.nav_date <= upper,
    ).order_by(FundHistory.nav_date.asc()).all()
    return [r[0] for r in rows], latest


def calendar_gap(calendar: Dict[str, List[date]], code: Optional[str],
                 window_start: Optional[date], window_end: Optional[date],
                 today: Optional[date] = None) -> Optional[str]:
    """从 `nav_calendar` 那份日历里回答"绑到 `code` 之后验证器判得出来吗"。

    把"切片"也收在这一个地方：每个调用方自己抄一遍 `start <= d <= end`，
    就等于每人再造一把尺子（第 47 轮那族"同一件事的两套定义"）。
    """
    start, end = _as_date(window_start), _as_date(window_end)
    days = calendar.get(code) or []
    in_window = [d for d in days if start is not None and d >= start
                 and (end is None or d <= end)]
    return target_cannot_evidence_window(in_window, max(days) if days else None,
                                         window_start, window_end, today=today)


def target_cannot_evidence_window(in_window: Sequence[date],
                                  latest_nav: Optional[date],
                                  window_start: Optional[date],
                                  window_end: Optional[date],
                                  today: Optional[date] = None) -> Optional[str]:
    """把一条预测改标到某只标的之后，**验证器对这段窗口判得出来吗**。判得出来返回 None。

    为什么第 100 轮那道门不够（这一条是 2026-09-26 在生产上量出来的）：它只问
    "末笔净值不早于窗口起点"。`158038` 库里首笔净值是 2026-09-07、`012765` 是 2026-08-28，
    而压在它们身上的预测窗口起点在 08-28~09-14 / 07-01 ⇒ 末笔远晚于窗口起点，那道门点头
    放行，可验证器要的**两件事**当场就没有：窗口内 ≥ `VERIFY_MIN_DATA_POINTS` 个净值点、
    终点距目标日 ≤ `VERIFY_MAX_END_NAV_AGE_DAYS` 天（见
    `PredictionVerifyService._check_fund_data_availability`）。放过去的结果就是老板要清零
    的那一档：**一条到期了却永远判不出来的预测** —— 而且它报的是 `insufficient_points`，
    不是 `no_source_history`，所以任务 #8 那道"结构性不可验"的重问锁也不会接住它。

    两个阈值都从 `config` 取（验证器用的就是这两个常量），这里不立第二个数字。

    三种"不敢下结论"一律返回 None（放行，交回正常流程）：
    ① `latest_nav` 为空 ⇒ 这只标的刚建档、还没同步过，"库里没有"不等于"永远没有"；
    ② 窗口起点说不清 ⇒ 连要问哪段都不知道；
    ③ 窗口**还没到期** ⇒ 净值本来就该在后面几天才到，此时点数不足不是毛病
       （唯一例外是"末笔停在窗口开始之前"，那句才敢说它不会再来）。
    """
    start, end = _as_date(window_start), _as_date(window_end)
    end = end or start
    days = [d for d in (_as_date(x) for x in (in_window or [])) if d is not None]
    latest = _as_date(latest_nav)
    if start is None or latest is None:
        return None
    if nav_cannot_cover_window(latest, start):
        return ('它最后一笔净值停在 %s，早于这条预测的窗口起点 %s'
                ' ⇒ 那段净值不会再来，绑上去等于制造一条验不了的预测' % (latest, start))
    if end > (_as_date(today) or current_as_of()):
        return None                      # 还没到期：等到净值来就知道了
    min_points = config.VERIFY_MIN_DATA_POINTS
    if len(days) < min_points:
        return ('这段窗口（%s~%s）里它只发过 %d 笔净值，验证器至少要 %d 个比较点'
                ' ⇒ 绑过去会一直判不出来（挂在「到期未判」那一档）'
                % (start, end, len(days), min_points))
    gap = (end - max(days)).days
    max_age = config.VERIFY_MAX_END_NAV_AGE_DAYS
    if gap > max_age:
        return ('目标日 %s 已经过了 %d 天，可它在这段窗口里最后一笔净值是 %s'
                '（相差 %d 天 > %d 天上限）⇒ 终点取不到，绑过去会一直判不出来'
                % (end, (current_as_of() - end).days, max(days), gap, max_age))
    return None


def close_as_stale_target_note(prediction: Prediction, latest_nav: Optional[date],
                               window_start: Optional[date]) -> str:
    """关闭时写给老板看的那句话：说清为什么判不了、去哪找、对准确率有什么影响。"""
    return ('标的 %s 的数据源给不出这段净值（库里最后一条净值停在 %s，窗口从 %s 起），'
            '已问过两次仍无答案 ⇒ 无法判定，既不算判对也不算判错，不计入准确率；'
            '记录已放入回收站，可随时恢复'
            % (getattr(prediction, 'fund_code', '') or '未知',
               _as_date(latest_nav) or '未记录', _as_date(window_start) or '未记录'))


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
