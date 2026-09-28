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


def beijing_now() -> datetime:
    """时间戳版的"现在"：日期出自 `current_as_of()` 那一把北京钟，时分秒取同一把钟。

    为什么不让调用方直接写 `datetime.now()`（第 53 轮 A-11）：Render 的容器在 UTC，
    北京 00:00~08:00 归档的行，时间戳的**日期**会比页面上的"截至日"少一天 ——
    而回收站里那句"保留到 X 日"正是拿这个日期算的。两件事必须出自同一把钟，
    所以"今天"这一层仍然只由 `current_as_of()` 回答（全仓唯一的今日出口）。
    """
    try:
        from zoneinfo import ZoneInfo

        clock = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:
        clock = datetime.now()          # 取不到时区时 `current_as_of()` 自己会留一行日志
    return datetime.combine(current_as_of(), clock.time())


def archive_stamp(retention_days: int = 30):
    """归档一行的那一对时间戳：`(归档时刻, 可恢复到哪天)`。

    为什么要有这个函数（第 54 轮 A-1 / B-2）：会把一行放进回收站的有手动归档、系统关闭、
    合并相似预测三条路，而"少一天"那个缺陷上一批只修了第一条 —— 同一件事的第二、第三条
    活路不会自己长出来。写侧登记见 `tests/unit/test_one_ruler_per_question.py`
    里那把棘轮：它按 (文件, 函数) 收站点，并且**数每个函数里有几处写**（第 56 轮 M-4）。
    认哪些拼法**别在这里抄** —— 清单就是那份判据里的控制样品表，加一种拼法就在那加一格样品
    （第 57~59 轮三轮往里补，写死"五种"的那句话当场过期过一次）。
    """
    return beijing_now(), current_as_of() + timedelta(days=retention_days)


def conclusion_conditions(status: str):
    """「待验证 / 已验证」这两档在 SQL 里到底问什么 —— 全仓只此一处（第 54 轮 A-2 / B-3）。

    问的是**有没有结论**（`is_correct` 是否为空），不是遗留列 `predictions.status`：
    `classify`、验证器、改标咽喉都按 `is_correct` 说话，而 `status` 由老代码写。
    今天两库实测两把尺子逐档相同（镜像未判 422 ↔ `status='pending'` 422；生产同向），
    **那是巧合不是等价** —— 写侧与读侧一旦分叉，页面上的数就会与点进去的列表打脸。
    """
    if status == 'verified':
        return [Prediction.is_correct.isnot(None)]
    if status in ('pending', 'unverified'):
        return [Prediction.is_correct.is_(None)]
    raise ValueError('这一档不归这把尺子管：%r' % status)


def has_conclusion(prediction) -> bool:
    """Python 侧的同一句话：这一行已经有结论了吗（与 `conclusion_conditions` 同一把尺子）。"""
    return getattr(prediction, 'is_correct', None) is not None


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


def unverifiable_retry_days(verdict_reason: Optional[str] = None) -> int:
    """结构性不可验的重问间隔（天）—— **只有一个出处**，但两档各问各的期限。

    - `no_source_history`（数据源答"这段没有"）：这个数不是拍出来的。那条答复存成凭据，
      TTL 是 `backfill_proofs.EMPTY_TTL_DAYS`（空答复只信 2 天，防限流页被当成事实），
      重问间隔取 TTL+1 ⇒ 凭据一旦过期，这条预测自己回到到期队列再问一次。
      判据 `test_the_lock_expires_and_the_prediction_asks_again` 钉的就是这层关系。
    - `same_nav_endpoint`（起点与终点是同一条净值）：**这一档没有凭据可跟**，所以拿凭据
      TTL 当节奏是错的（第 54 轮 A-5 的账）：镜像上 5 行因此每三天弹回「待验证到期」
      被问一次、再弹回去，永远没有一个终局 —— 而老板的验收条件正是"打开后看不到过期
      了却没验证的预测"。这一档唯一会变的东西是**这段窗口里的净值行**，而常规同步只往回
      拉 `config.NAV_HISTORY_LOOKBACK_DAYS` 天 ⇒ 间隔取"回补范围 + 1"，
      含义是"等一次真的可能有新行落进这段窗口的机会，再问一次"。
      当场解掉这把锁的路有四条：每日同步补到**落在这段窗口里**的新行
      （`release_holds_after_nav_commit`，第 55 轮 M-2 —— 这一条以前不存在）、
      人工按区间重放补拉（会写凭据）、改标、编辑目标日；不必等到那天。

    不传 `verdict_reason` ⇒ 按有凭据那一档答（调用方没说要问哪件事时，用更短的那个）。
    懒导入：本模块被 API 与脚本共读，顶层拉 `src.fund` 会把整个包 __init__ 带进来。
    """
    if verdict_reason in LOCK_ONLY_VERDICT_REASONS:
        return nav_backfill_days() + 1
    from src.fund import backfill_proofs

    return int(backfill_proofs.EMPTY_TTL_DAYS) + 1


def nav_backfill_days(days: Optional[int] = None) -> int:
    """常规同步一次往回拉多少天净值 —— 这句话只许有一处实现。

    第 55 轮 M-1：上面那句"跟着回补范围走"当时是假的。`NAV_HISTORY_LOOKBACK_DAYS` 只喂到
    `FundAPI.get_fund_history` 的**签名默认值**，而真跑同步的 `update_fund_history` 一路
    有 **8 处**把 30 写死（复核：`git grep -n "update_fund_history" 4ef48ce -- src/ | grep days`
    ⇒ 两个默认值 + 五处调用实参 + 一处 demo）⇒ 两个数今天都是 30，那是**巧合不是等价**
    （本仓自己立的规矩）：把键改成 47，重问间隔变 48 而同步仍只拉 30 天，
    "等一次真可能有新行落进这段窗口的机会"当场失效。

    两件事缺一不可，判据 `test_the_nav_lookback_has_one_home_for_both_questions` 两腿都问：
    ① 这条路只许从这里取数（AST 扫 `src/`，谁再往 `days=` 塞字面量就点名）；
    ② 必须**在调用时**读那个键 —— 签名默认值在导入时就算死了，改了键它不动。
    """
    from src.core.config import config

    return int(config.NAV_HISTORY_LOOKBACK_DAYS) if days is None else int(days)


def hold_until(prediction: Prediction) -> Optional[date]:
    """这条预测被压到哪天不再重问；没有被压 ⇒ None（不判到期与否，由调用方看）。"""
    return _as_date(getattr(prediction, "next_verify_date", None))


def is_held_unverifiable(prediction: Prediction, as_of: Optional[date] = None) -> bool:
    """已到目标日、但被验证器压着不再重问（＝结构性不可验中）。

    判据是**验证器上一次真问出来的结论**（见 `apply_unverifiable_hold`），
    不是日历推断：`next_verify_date` 由创建时的排期保证 ≤ 目标日，
    所以"晚于今天"这个形状只可能由结构性结论写出来。
    **两档的节奏不共用一个数**（第 54 轮 A-5 / 任务 #121 的决定）：有凭据的那档跟凭据 TTL，
    没有凭据的那档跟净值回补范围，否则它每三天弹回「待验证到期」而没有终局 ——
    处置记录在 `docs/模块总览/预测验证与准确率统计.md` 的 2e 段（任务 #121）。
    """
    today = _as_date(as_of) or current_as_of()
    target = _as_date(getattr(prediction, "target_date", None))
    hold = hold_until(prediction)
    return (target is not None and hold is not None
            and target <= today and hold > today
            and getattr(prediction, "is_correct", None) is None
            and not getattr(prediction, "is_deleted", False))


STRUCTURAL_VERDICT_REASONS = ('no_source_history', 'same_nav_endpoint')
# 「问过两次 ⇒ 收进回收站」这条升级只对登记过的档位开放；其余结构性结论**只锁不关**。
# 两张名单合起来必须逐字等于上面那条（判据 `test_the_structural_reasons_are_all_dispositioned`）
# ⇒ 加一档不登记就红，别再让"要不要关"这句话散落到 if 里。
CLOSABLE_VERDICT_REASONS = ('no_source_history',)
LOCK_ONLY_VERDICT_REASONS = ('same_nav_endpoint',)


def is_structural_verdict(verdict_reason: Optional[str]) -> bool:
    """这条失败结论是不是"已经真问过，今天再问一遍也不会换个答案"—— 配重问锁的唯一一档。

    两档都出自 `_check_fund_data_availability` 里**必须建立在证据上**的分支：
    `no_source_history` 要求"已按区间问过数据源"（传输失败/限流一律不记凭据），
    `same_nav_endpoint` 要求起点与终点取到同一条净值 ⇒ 涨跌幅恒为 0，方向判不出来。
    两档**只共用"配不配锁"这一句**，不共用"能不能关"（见 `CLOSABLE_VERDICT_REASONS`）。
    其余失败（点数不够、没档案、端点太旧、在等目标日净值）明天可能就自愈，
    锁了它们等于亲手把一条可验的预测藏出到期队列。
    """
    return verdict_reason in STRUCTURAL_VERDICT_REASONS


def was_locked_previously(previous_hold: Optional[date],
                          target_date: Optional[date]) -> bool:
    """这一行**以前**有没有被结构性结论锁过 —— 只有一把尺子，两处共用。

    认的不是"那根日期过去了没有"，而是"它落在自己的目标日**之后**"：创建排期被
    `test_the_creation_schedule_never_writes_a_date_after_the_target` 夹在目标日之前 ⇒
    晚于目标日的那一天只可能由 `apply_unverifiable_hold` 写下。
    调用方：验证器判"要不要从锁升级成关"（`should_close_as_stale_target` ①）、
    存量收口脚本判"这句『已问过两次』到底能不能写"（第 52 轮 A-4 实测：脚本收掉的
    5 行 `next_verify_date` 全部 ≤ 目标日，那句"两次"当时是写多的）。
    """
    hold, target = _as_date(previous_hold), _as_date(target_date)
    return hold is not None and target is not None and hold > target


def stale_close_evidence(*, local_latest_nav: Optional[date] = None,
                         window_start: Optional[date] = None,
                         local_first_nav: Optional[date] = None,
                         window_end: Optional[date] = None) -> Optional[str]:
    """这句"永远问不出答案"拿不拿得出**只属于它自己的**证据 —— 两处共用（验证器、收口脚本）。

    两种永久形状，各问各的，谁都不许替谁说：
    - `'stopped'`：`local_latest_nav < window_start` ⇒ 这只产品从窗口开始之前就没再发过一条
      净值。少了它，我们自己同步掉几天就可能把一条本可验证的预测关掉（第 23 轮那种
      "把镜像坏了当产品坏了"的坑）。
    - `'pre_inception'`：`local_first_nav > window_end` ⇒ **窗口整段早于这只标的首笔净值**
      （第 53 轮 B-1 的 BLOCKER；2026-09-27 镜像实测这一档 **7** 行，复核命令写在 `AGENTS.md`
      「当前测试基线」那一条的第①点）。新基金先被拿来发预测、净值从成立
      那天才开始记 ⇒ 同步永远补不出它成立之前的历史，但末条净值活得好好的、远晚于窗口起点，
      所以 `stopped` 那把尺子对它恒为 False ⇒ 只锁不关，每到一个重问日弹回「待验证到期」再被
      踢出去一次。这一档要单独认，别拿 `stopped` 凑。
    说不清（日期缺失）⇒ None，交回给"继续问"那条路。两种同时成立的形状不存在
    （`first <= latest` 且 `start <= end`）。
    """
    if nav_cannot_cover_window(local_latest_nav, window_start):
        return 'stopped'
    if nav_started_after_window(local_first_nav, window_end):
        return 'pre_inception'
    return None


def should_close_as_stale_target(*, verdict_reason: Optional[str],
                                 previous_hold: Optional[date],
                                 target_date: Optional[date],
                                 local_latest_nav: Optional[date],
                                 window_start: Optional[date],
                                 local_first_nav: Optional[date] = None,
                                 window_end: Optional[date] = None,
                                 today: Optional[date] = None) -> bool:
    """这条预测要不要从"重问锁"升级成**关闭**（永远问不出答案，代价比锁大得多）。

    两个条件必须同时成立，缺一个都不许关（关 = 从活跃列表消失）：
    ① 同一个窗口**上一轮真的被锁过、锁今天到点**：认的不是"这根日期过去了没有"，
       而是"这根日期落在**自己的目标日之后**"。到期队列里的行，那根日期本来就是创建时
       排出来的（必然 ≤ 目标日 ≤ 今天），拿"它过去了"当"问过两次"的证据 ⇒ 第一次判出
       结构性结论就会直接进回收站，"问过两次才关"当场成谎（2026-09-27 在镜像上真跑一次批量
       验证，那两条"周六目标日"就是这么一跳进回收站的 —— 是我自己跑出来的，不是评审发现的）。
       创建排期被 `test_the_creation_schedule_never_writes_a_date_after_the_target`
       夹在目标日之前 ⇒ 只有 `apply_unverifiable_hold` 会写下晚于目标日的那一天。
    ② 这一档在 `CLOSABLE_VERDICT_REASONS` 里，且 `stale_close_evidence()` 答得出永久形状
       （`'stopped'` 停更 / `'pre_inception'` 窗口早于首笔净值）。
       `same_nav_endpoint` **一律不关**（第 52 轮 A-1，实测过才敢这么写）：那句"起点与终点
       是同一条净值"有两种来路 —— 目标日确实不是交易日，**或这只标的自己有数据洞**（补拉能填）。
       库里分不开这两种：2026-09-27 在镜像上逐日数过行数，真休市的 2026-07-11（周六）
       全库 **1** 行（货币基金照发），交易日的 2026-09-08 有 **202** 行 ⇒
       "目标日当天全库零行"这把尺子会把周六读成洞、把洞读成休市，两种都会写进回收站。
       关错的代价是"永久消失 + 一句假原因"，锁的代价只是"隔几天再问一次" ⇒ 只锁不关。
    """
    if verdict_reason not in CLOSABLE_VERDICT_REASONS:
        return False
    today = _as_date(today) or current_as_of()
    prev = _as_date(previous_hold)
    if not was_locked_previously(prev, target_date):
        return False                      # 没锁过（那根日期是排期写的）：这是第一次问出来
    if prev > today:
        return False                      # 锁还没到点：不该被问到
    return stale_close_evidence(
        local_latest_nav=local_latest_nav, window_start=window_start,
        local_first_nav=local_first_nav, window_end=window_end) is not None


def nav_started_after_window(local_first_nav: Optional[date],
                             window_end: Optional[date]) -> bool:
    """这段窗口**整段早于**这只标的的第一笔净值 —— 那几天它还没有净值可发。

    与 `nav_cannot_cover_window` 是一对：一个问"末条太早（已经停了）"，一个问"首笔太晚
    （那时候还没开始）"。两边都是**永久**事实，也都只此一处实现这句话。
    两个日期任一说不清 ⇒ False（不敢下结论，继续走"再问一次"）。
    """
    first, end = _as_date(local_first_nav), _as_date(window_end)
    return first is not None and end is not None and first > end


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


def window_from_calendar(calendar: Dict[str, List[date]], code: Optional[str],
                         window_start: Optional[date],
                         window_end: Optional[date]) -> tuple:
    """从 `nav_calendar` 那份日历里切出 `(这段窗口里的净值日, 库里最后一笔净值日)`。

    切片这件事**只许在这一处做**：`window_evidence` 是单行版（自己去库里按窗口查），
    批量版如果每个调用方各抄一遍 `start <= d <= end`，就会出现"同一把尺子两腿两种喂法"
    —— 第 66 轮复评 MA-4/MI-4 量到的正是这一格：预览那一腿走 `calendar_gap`（切片），
    实跑那一腿把**全量**历史直接递给 `retag_prediction` 当"窗口内证据"，于是点数那一臂
    收到超集、只会更松，"预览与实跑同数"从构造成立退化成偶然成立。
    """
    start, end = _as_date(window_start), _as_date(window_end)
    days = calendar.get(code) or []
    in_window = [d for d in days if start is not None and d >= start
                 and (end is None or d <= end)]
    return in_window, (max(days) if days else None)


def calendar_gap(calendar: Dict[str, List[date]], code: Optional[str],
                 window_start: Optional[date], window_end: Optional[date],
                 today: Optional[date] = None) -> Optional[str]:
    """从 `nav_calendar` 那份日历里回答"绑到 `code` 之后验证器判得出来吗"。

    把"切片"也收在这一个地方：每个调用方自己抄一遍 `start <= d <= end`，
    就等于每人再造一把尺子（第 47 轮那族"同一件事的两套定义"）。
    切片那一腿现在在 `window_from_calendar` 里 —— 判"够不够证据"与交给改标门的那份证据
    必须出自同一次切片，否则两条路各拿一份超集/子集。
    """
    in_window, latest = window_from_calendar(calendar, code, window_start, window_end)
    return target_cannot_evidence_window(in_window, latest,
                                         window_start, window_end, today=today)


def calendar_answer(calendar: Dict[str, List[date]], code: Optional[str],
                    window_start: Optional[date], window_end: Optional[date],
                    today: Optional[date] = None) -> tuple:
    """`calendar_gap` 的同一把尺子，但把"为什么放行"那一种也交出来（见 `evidence_answer`）。

    与 `calendar_gap` 共用**同一次切片**（`window_from_calendar`），这里不比任何新数字。
    """
    in_window, latest = window_from_calendar(calendar, code, window_start, window_end)
    return evidence_answer(in_window, latest, window_start, window_end, today=today)


def evidence_answer(in_window: Sequence[date],
                    latest_nav: Optional[date],
                    window_start: Optional[date],
                    window_end: Optional[date],
                    today: Optional[date] = None) -> tuple:
    """`target_cannot_evidence_window` 的**同一把尺子**，但把"为什么放行"也交出来。

    返回 `(种类, 原因)`：
      · `'cannot'`   判得出来吗的答案是"判不出来" ⇒ 原因就是那句人话；
      · `'evidenced'` 这段窗口**已经过了**、而这只标的当时就给得出验证器要的那两样；
      · `'not_due'`  窗口还没到期 ⇒ 现在问不出结果，**这不等于"给得出"**；
      · `'unknown'`  窗口起点说不清、或这只标的在库里一笔净值都没有（刚建档还没同步过）。

    为什么要拆这一层（第 67 轮复评 MAJOR-8，镜像现数）：调用方以前只问 `calendar_gap()` 是不是空，
    于是三种"还不知道"和一种"真给得出"被数进同一个键，页面上那句话就成了
    "它们自己那只标的就给得出这段窗口的净值" —— 镜像补标那一路 830 条候选里 **189 条（22.8%）**
    只是因为**还没到期**。放行这件事不用改（没到期当然不动它），**说出口的话必须分开**。
    阈值仍然只有 `config` 那两个，这里一个新数字都不立。
    """
    start, end = _as_date(window_start), _as_date(window_end)
    end = end or start
    days = [d for d in (_as_date(x) for x in (in_window or [])) if d is not None]
    latest = _as_date(latest_nav)
    if start is None or latest is None:
        return 'unknown', None
    if nav_cannot_cover_window(latest, start):
        return 'cannot', ('它最后一笔净值停在 %s，早于这条预测的窗口起点 %s'
                          ' ⇒ 那段净值不会再来，绑上去等于制造一条验不了的预测' % (latest, start))
    if end > (_as_date(today) or current_as_of()):
        return 'not_due', None            # 还没到期：等到净值来就知道了
    min_points = config.VERIFY_MIN_DATA_POINTS
    if len(days) < min_points:
        return 'cannot', ('这段窗口（%s~%s）里它只发过 %d 笔净值，验证器至少要 %d 个比较点'
                          ' ⇒ 绑过去会一直判不出来（挂在「到期未判」那一档）'
                          % (start, end, len(days), min_points))
    gap = (end - max(days)).days
    max_age = config.VERIFY_MAX_END_NAV_AGE_DAYS
    if gap > max_age:
        return 'cannot', ('目标日 %s 已经过了 %d 天，可它在这段窗口里最后一笔净值是 %s'
                          '（相差 %d 天 > %d 天上限）⇒ 终点取不到，绑过去会一直判不出来'
                          % (end, (current_as_of() - end).days, max(days), gap, max_age))
    return 'evidenced', None


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
    ⚠ 这三档与"真的给得出"在**放行**这一件事上同等待遇，在**说出口**上不是同一件事 ——
    要区分就按 `evidence_answer(...)` 的种类问，别在这里比大小（第 67 轮复评 MAJOR-8）。
    """
    return evidence_answer(in_window, latest_nav, window_start, window_end,
                           today=today)[1]


def close_as_stale_target_note(prediction: Prediction, latest_nav: Optional[date],
                               window_start: Optional[date],
                               *, first_nav: Optional[date] = None,
                               window_end: Optional[date] = None) -> str:
    """关闭时写给老板看的那句话：说清为什么判不了、去哪找、对准确率有什么影响。

    原因那半句由 `stale_close_evidence()` 现算（**同一把尺子**，不是两处各抄一句）：
    "停更"与"那几天还没开始发净值"是两种相反的来路，说反了会让人去查一只没毛病的基金。
    答不出永久形状时只许说中性事实，不许挑一个原因写上去。

    那句"已问过两次"**不需要参数**：能走到这里的唯一门是 `should_close_as_stale_target`，
    它要求"上一轮真的被锁过、且锁已到点"，而本次是这一行的第二次问 ⇒ 这句话由门保证，
    不由调用方自报（第 53 轮 A-4：原来那个 `asked_times` 只能被测试走到，是一条死路参数）。
    """
    code = getattr(prediction, 'fund_code', '') or '未知'
    tail = ('已问过两次仍无答案 ⇒ 无法判定，既不算判对也不算判错，不计入准确率；'
            '记录已放入回收站，可随时恢复')
    evidence = stale_close_evidence(local_latest_nav=latest_nav,
                                    window_start=window_start,
                                    local_first_nav=first_nav,
                                    window_end=window_end)
    if evidence == 'pre_inception':
        return ('标的 %s 在那段窗口还没有开始发净值（库里第一笔净值始于 %s，窗口到 %s 就结束'
                '了，早于它），%s' % (code, _as_date(first_nav) or '未记录',
                                     _as_date(window_end) or '未记录', tail))
    if evidence == 'stopped':
        return ('标的 %s 的数据源给不出这段净值（库里最后一条净值停在 %s，窗口从 %s 起），%s'
                % (code, _as_date(latest_nav) or '未记录',
                   _as_date(window_start) or '未记录', tail))
    # 走到这里说明调用方没把证据递全（或者它本来就不该关）⇒ 只报事实，不报原因
    return ('标的 %s 的这段窗口判不出结论（库里净值区间：%s 至 %s；窗口从 %s 起、到 %s 止），%s'
            % (code, _as_date(first_nav) or '未记录', _as_date(latest_nav) or '未记录',
               _as_date(window_start) or '未记录', _as_date(window_end) or '未记录', tail))


def apply_unverifiable_hold(prediction: Prediction, as_of: Optional[date] = None,
                            verdict_reason: Optional[str] = None) -> date:
    """验证器判定"这段今天问不出答案"后，把这条压到重问日。

    返回压到的那一天。**不动 `is_correct`、不清结论** —— 它只回答"什么时候再问"，
    不回答"预测对不对"。`verdict_reason` 决定问的是哪一档的节奏（见
    `unverifiable_retry_days`）：有凭据可跟的按凭据 TTL，没有凭据的按净值回补范围。
    """
    today = _as_date(as_of) or current_as_of()
    hold = today + timedelta(days=unverifiable_retry_days(verdict_reason))
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


def release_holds_after_nav_update(db: Session, fund_code: Optional[str],
                                   changed_dates: Optional[Iterable] = None,
                                   as_of: Optional[date] = None) -> List[int]:
    """净值真的补进了这段窗口 ⇒ 把压着的那把重问锁当场撤掉，回「待验证到期」。

    第 55 轮 M-2：§2e 给老板指的三条出路里，"补拉成功当场解锁"这一条**当时不存在**。
    `release_unverifiable_hold` 全仓只有一个调用方（`verify_prediction`），而被锁的行在
    `filter_due_for_verify` 就被减出到期队列 ⇒ 没人会对它跑验证 ⇒ 第二天就补到的净值
    也要白等一整个重问间隔（端点档 31 天，旧行为 3 天）。这句承诺写在文档里、
    入口却点不到，比不写更坏。

    两道闸，缺一不算"补到了这段"：
    ① 新落的行**落在这条预测的窗口里**（`changed_dates`）—— 少了这条，每天同步在别处
      补到的行会把锁一次次撤掉、验证器再一次次锁回去，第 54 轮 A-5 刚修的"每三天弹一回
      到期队列"就换个频率复现；
    ② 问的还是改标门与验证器共用的那把尺子（`target_cannot_evidence_window` 答 None
      ⇒ 验证器判得出来），不另立第二个"够不够"。
    只碰**由结构性锁压着**的行（`next_verify_date > target_date`，见 `was_locked_previously`），
    正常排期的行一个字都不动。返回被撤锁的预测 id；**不 commit**，事务边界留给调用方。
    """
    today = _as_date(as_of) or current_as_of()
    if not fund_code:
        return []
    changed = {d for d in (_as_date(x) for x in (changed_dates or [])) if d is not None}
    held = (
        db.query(Prediction)
        .filter(Prediction.is_deleted == False,               # noqa: E712
                Prediction.is_correct.is_(None),
                Prediction.fund_code == fund_code,
                Prediction.target_date.isnot(None),
                Prediction.target_date <= today)
        .all()
    )
    released: List[int] = []
    for p in held:
        if not was_locked_previously(p.next_verify_date, p.target_date):
            continue
        start = _as_date(getattr(p, "prediction_date", None))
        end = _as_date(p.target_date)
        if changed and not any(start is not None and start <= d <= end for d in changed):
            continue                       # 补的是别段的净值 ⇒ 这把锁问的那件事没变
        in_window, latest = window_evidence(db, fund_code, start, end)
        if target_cannot_evidence_window(in_window, latest, start, end, today=today) is None:
            if release_unverifiable_hold(p, as_of=today):
                released.append(p.id)
    return released


def release_holds_after_nav_commit(db: Session, fund_code: Optional[str],
                                   changed_dates: Optional[Iterable] = None,
                                   *, where: str = '') -> int:
    """同步补到净值之后调用：撤锁、说出口、不许把同步本身弄失败。

    单独成一处是为了让"每条写净值的路都要接它"这件事只有一份实现
    （判据 `test_the_nav_unlock_path_is_wired_into_both_sync_writers` 数的是接线）。
    """
    try:
        released = release_holds_after_nav_update(db, fund_code, changed_dates)
    except Exception as exc:                     # 撤锁失败不能毁掉一次净值同步
        logging.getLogger(__name__).warning(
            '[重问锁] %s 补完 %s 的净值后解除重问锁失败（行照旧留在原排期）：%s',
            where or '同步', fund_code, exc)
        return 0
    if released:
        logging.getLogger(__name__).info(
            '[重问锁] %s 补到 %s 的净值 ⇒ 解除 %d 条重问锁，回到「待验证到期」：%s',
            where or '同步', fund_code, len(released), released)
    return len(released)


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
    `unverifiable` 只由验证器真问出来的结构性结论写（哪些算，看 `is_structural_verdict`），
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
    判不出结论**的那批会被暂时压住（到重问日自动回队）。

    这一句**不许写"数据源给不出这段净值"**：行上只有那根日期，没有当初是哪个 reason
    把它压住的（`predictions` 没这列，为一句文案开生产迁移不值）。被压住的原因有两档，
    其中"起点与终点是同一条净值"的标的往往活得好好的、源端也给得出起点那条 ⇒ 那句话
    对这一半行是说反的（第 52 轮 A-2 / B-2）。逐条真原因在批次回执
    「上次验证未成功原因」里，那才是有 reason 的地方。
    """
    today = _as_date(as_of) or current_as_of()
    if getattr(prediction, "prediction_type", None) == "flat":
        return "中性预测（观望）不参与验证"
    if is_held_unverifiable(prediction, as_of=today):
        hold = hold_until(prediction)
        return (f"验证器已按区间问过、这一轮判不出结论 ⇒ 属结构性不可验，"
                f"{hold.isoformat()} 之前不再重问（到点自动回队再问一次；"
                f"到底是哪一档原因不落库，只在那一次验证的回执里）")
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
