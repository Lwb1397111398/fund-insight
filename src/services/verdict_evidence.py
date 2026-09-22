# -*- coding: utf-8 -*-
"""结论证据的可复现性判定（派生值，不落库、不加列）。

为什么做成派生而不是一个布尔列：这一类状态会随着净值序列的修正**自己变回去**
（补拉到位、历史被更正），写进列就要面对"谁来清、什么时候清、迁移要不要跑"
（本仓库生产加列＝一次迁移）。读的时候算一次，永远与净值表一致，也不可能出现
"列说证据有效而表里根本没有那一行"这种第二份真值。

它回答的问题是第 16/17 轮查出来的那件事：1163 条已判结论里有 250 条，
**结论存的端点净值在"当前标的"的净值表里已经复现不出来**。历轮自洽检查看不见它 ——
`verify_score` 与 `verify_history` 是同一次验证一起写的，互相吻合，
却可能整体挂着另一个标的、或一份被就地改写过的净值。

三种状态（None＝证据仍然成立）：
  - `verdict_under_other_fund` 写下结论时行上挂的是**另一个**代码，且那个代码当天的净值
    正好等于存的端点值 ⇒ 真·挂错标的（改标后没重算）。
  - `nav_row_missing`            端点那一天该标的没有净值行：周末目标日的老数据、镜像缺行、基金停更都在这一桶。
  - `nav_rewritten`              同一天有行，数值不同 ⇒ 净值被就地改写或覆盖过。
"""
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional

FLOAT_TOL = 1e-6


def _same_number(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= FLOAT_TOL * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return a == b


def has_verdict(prediction) -> bool:
    return getattr(prediction, 'is_correct', None) is not None \
        and getattr(prediction, 'end_nav', None) is not None \
        and getattr(prediction, 'end_nav_date', None) is not None


def _as_date(value):
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def evidence_status(prediction, nav_rows_by_key, verified_code=None) -> Optional[str]:
    """纯判据版本：`nav_rows_by_key` 是 `{(fund_code, nav_date): nav}` 批量预取的结果。

    拆成纯函数的原因与 `resync_verdict_scalars.find_desynced` 一样：
    判据要能脱离数据库被测（列表页一次 200 条，不能每条各打一次查询）。
    """
    if not has_verdict(prediction):
        return None
    code = prediction.fund_code
    end_date = _as_date(prediction.end_nav_date)
    current_nav = nav_rows_by_key.get((code, end_date))
    if verified_code and verified_code != code:
        # 两条同时成立才算"挂错标的"：结论当时是另一个代码，且那个代码当天的净值
        # 正好等于存的端点值。只看"改过标"会把"改标后又重验过"的行误判
        # （第 17 轮我第一版就这么错过：报出的 97 条里 95 条末次验证在改标之后）。
        if _same_number(nav_rows_by_key.get((verified_code, end_date)), prediction.end_nav):
            return 'verdict_under_other_fund'
    if current_nav is None:
        return 'nav_row_missing'
    if not _same_number(current_nav, prediction.end_nav):
        return 'nav_rewritten'
    return None


def _verified_codes(db, predictions: List) -> Dict[int, Optional[str]]:
    """每条预测**写下最后一条结论时**挂的 fund_code（最后一条 verified 变更日志的后像）。"""
    from src.models.database import PredictionChangeLog

    ids = [p.id for p in predictions]
    out: Dict[int, Optional[str]] = {}
    if not ids:
        return out
    # 按 id 升序遍历（id 即写入顺序）⇒ 后写覆盖前写，留下的就是"最后一次验证当时"的代码。
    # 只查需要的列：列表页一次可能带 200 条预测，日志行数在几千量级。
    rows = db.query(PredictionChangeLog.prediction_id,
                    PredictionChangeLog.after_state).filter(
        PredictionChangeLog.prediction_id.in_(ids),
        PredictionChangeLog.action == 'verified',
    ).order_by(PredictionChangeLog.prediction_id,
               PredictionChangeLog.id).all()
    for pid, after in rows:
        out[pid] = (after or {}).get('fund_code')
    return out


def evidence_statuses(db, predictions: Iterable) -> Dict[int, Optional[str]]:
    """批量算证据状态：两条查询，不给列表页加 N+1。"""
    from src.models.database import FundHistory

    rows = [p for p in predictions]
    judged = [p for p in rows if has_verdict(p)]
    if not judged:
        return {p.id: None for p in rows}

    dates = sorted({_as_date(p.end_nav_date) for p in judged if _as_date(p.end_nav_date)})
    codes = {p.fund_code for p in judged if p.fund_code}
    verified = _verified_codes(db, judged)
    codes |= {c for c in verified.values() if c}

    nav_rows = db.query(FundHistory.fund_code, FundHistory.nav_date, FundHistory.nav).filter(
        FundHistory.nav_date.in_(dates),
        # 必须按代码过滤：只按日期筛的话，一页 70 个日期就会把**全表**拉回来
        # （实测 8724/10600 行 = 82%），在 Supabase 上等于每翻一页回传一次净值全表。
        FundHistory.fund_code.in_(list(codes)),
    ).all() if codes and dates else []
    nav_by_key = {(c, _as_date(d)): nav for c, d, nav in nav_rows if c in codes}
    return {p.id: evidence_status(p, nav_by_key, verified.get(p.id)) for p in rows}


def stale_counts(db) -> Dict[str, int]:
    """全库已判结论里各失效成因的条数（每日跑批与审计脚本共用，别各写一份 SQL）。"""
    from src.models.database import Prediction

    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,                     # noqa: E712
        Prediction.is_correct.isnot(None),
        Prediction.end_nav.isnot(None),
        Prediction.end_nav_date.isnot(None)).all()
    counts: Dict[str, int] = {}
    for kind in evidence_statuses(db, rows).values():
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


EVIDENCE_LABELS = {
    'verdict_under_other_fund': '结论是按改标前的标的判的，至今没重算',
    'nav_row_missing': '端点那天该标的没有净值行（周末老数据 / 本地缺行 / 基金停更），需按区间回补后重验',
    'nav_rewritten': '端点那天的净值后来被修正过，结论用的数已复现不出来',
}


def evidence_label(status: Optional[str]) -> Optional[str]:
    return EVIDENCE_LABELS.get(status) if status else None


def judged_rows(db) -> List:
    """未删除、已按下结论的预测（唯一取法，脚本与接口共用）。"""
    from src.models.database import Prediction
    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,                      # noqa: E712
        Prediction.is_correct != None,
    ).all()
    return [r for r in rows if has_verdict(r)]


def span_report(db) -> Dict:
    """把"多少结论证据已失效"折算成一份可展示的体检报告（**唯一出处**）。

    为什么放在服务层而不是脚本里：脚本 `audit_verdict_evidence.py` 与页面都要报这几个数，
    两把尺子迟早打架（第 23 轮：我给老板报了十几轮**镜像库**的数，生产其实是另一组）。
    所以这里连 `database` 与 `as_of` 一起给出去 —— 数字必须带库名和截止日。
    """
    from src.services.prediction_lifecycle import current_as_of

    rows = judged_rows(db)
    judged = len(rows)
    correct = sum(1 for r in rows if r.is_correct)
    statuses = evidence_statuses(db, rows)
    stale_rows = [r for r in rows if statuses.get(r.id)]
    stale_correct = sum(1 for r in stale_rows if r.is_correct)
    by_kind: Dict[str, int] = {}
    for r in stale_rows:
        kind = statuses.get(r.id)
        by_kind[kind] = by_kind.get(kind, 0) + 1
    pct = lambda n: round(100.0 * n / judged, 2) if judged else 0.0
    return {
        'judged': judged,
        'correct': correct,
        'accuracy_pct': pct(correct),
        'stale_evidence': len(stale_rows),
        'stale_pct': pct(len(stale_rows)),
        # 两端假设：失效的这批"全判错" / "全判对"。真值在区间内，今天定不到小数点。
        'span_low_pct': pct(correct - stale_correct),
        'span_high_pct': pct(correct - stale_correct + len(stale_rows)),
        'by_kind': by_kind,
        # 统一走 `current_as_of()`（北京时间自然日）：第 24 轮评审指出 Render 没设 TZ，
        # `date.today()` 在 UTC 下每天会有 8 小时显示"截至昨天"，而这个字段存在的理由
        # 恰恰是"数字必须带截止日"。
        'as_of': current_as_of().isoformat(),
        'database': database_label(db),
    }


def database_label(db) -> str:
    """给老板看的库名：本地镜像 / 线上生产，别说"数据库"这种没信息量的词。"""
    try:
        url = str(db.get_bind().url)
    except Exception:
        return '未知库'
    if url.startswith('sqlite'):
        return '本地镜像库'
    if url.startswith(('postgres', 'postgresql')):
        return '线上生产库'
    if url.startswith('mysql'):
        return 'MySQL 库'
    return url.split('://')[0] + ' 库'
