"""
市场整体状态（投资建议链路的软闸）。

纯本地计算：读 fund_history 里宽基指数 ETF 的净值序列（510300 优先，
缺数据退 510050 / 510100），算近 5 日、近 20 日涨跌与自近期高点回撤，
按常量阈值分档 strong / range / weak / very_weak，附一句人话仓位话术。
净值不足 MIN_ROWS 个点 ⇒ stance='unknown'：闸自动失效，不拦建议。

阈值是启发式初版（老板 2026-10-12 拍板"软提醒，AI 可 override 但要说明理由"），
调阈值只改本文件常量，校验层从 stance 读结果、不复制阈值。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.models.database import FundHistory

# 宽基代理：主 510300（沪深300ETF华泰柏瑞），缺数据依次退 510050 / 510100
# （代码与 src/constants/sector_fund_map.py 宽基表一致）
MARKET_INDEX_FUNDS: Tuple[str, ...] = ("510300", "510050", "510100")

# 计算窗口与下限：近 N 个交易日；净值点不足则闸失效
WINDOW_DAYS = 20          # 近 20 个交易日涨跌
RECENT_DAYS = 5           # 近 5 个交易日涨跌
MIN_ROWS = 6              # 少于 6 个净值点无法算 5 日涨跌 ⇒ unknown

# 分档阈值（%）。very_weak 需"20日跌透且仍在跌"两个条件同时成立
VERY_WEAK_R20 = -6.0      # 近20日跌幅 ≤ -6%
WEAK_R20 = -3.0           # 近20日跌幅 ≤ -3% ⇒ 弱势
WEAK_DRAWDOWN = -8.0      # 或自窗口高点回撤 ≥ 8% ⇒ 弱势
STRONG_R20 = 3.0          # 近20日涨幅 ≥ +3% 且近5日仍在涨 ⇒ 强势

STANCE_LABELS: Dict[str, str] = {
    "strong": "强势",
    "range": "震荡",
    "weak": "弱势",
    "very_weak": "极弱",
    "unknown": "无数据",
}

POSITION_LABELS: Dict[str, str] = {
    "strong": "市场强势，可正常买入、加仓",
    "range": "市场震荡，轻仓参与、快进快出",
    "weak": "市场弱势，建议减仓为主（持仓不超过三成），不要新开仓买入",
    "very_weak": "市场极弱，建议空仓观望，原有板块全部卖出",
    "unknown": "大盘数据缺失，系统无法给出仓位建议",
}

# 闸生效的两档：出现买入/加仓类动作必须给 override_reason（fail-closed）
GATE_ENFORCED_STANCES = frozenset({"weak", "very_weak"})


def classify_stance(r5: Optional[float], r20: Optional[float], drawdown: Optional[float]) -> str:
    """按阈值分档；任一关键值缺失按 range 处理（数据形状异常时不硬判）。"""
    if r5 is None or r20 is None or drawdown is None:
        return "range"
    if r20 <= VERY_WEAK_R20 and r5 < 0.0:
        return "very_weak"
    if r20 <= WEAK_R20 or drawdown <= WEAK_DRAWDOWN:
        return "weak"
    if r20 >= STRONG_R20 and r5 > 0.0:
        return "strong"
    return "range"


def _pct(last: float, base: float) -> Optional[float]:
    if not base or base <= 0:
        return None
    return round((last / base - 1.0) * 100.0, 2)


def _metrics(nav_list: List[float]) -> Dict[str, Any]:
    """升序净值序列（≥MIN_ROWS 个点）→ r5 / r20 / 回撤 / 窗口天数。"""
    n = len(nav_list)
    last = nav_list[-1]
    span = min(WINDOW_DAYS, n - 1)
    r5 = _pct(last, nav_list[-(RECENT_DAYS + 1)]) if n >= RECENT_DAYS + 1 else _pct(last, nav_list[0])
    r20 = _pct(last, nav_list[-(span + 1)])
    window_high = max(nav_list[-(span + 1):])
    drawdown = _pct(last, window_high)
    return {"r5": r5, "r20": r20, "drawdown": drawdown, "window_days": span}


def compute_market_state(db: Session, as_of: Optional[date] = None) -> Dict[str, Any]:
    """
    算出大盘状态字典。永不抛异常到调用方：查不到/算不出就回 unknown。

    返回键：stance / stance_label / position_label / enforce /
    index_code / index_name / nav_as_of / r5 / r20 / drawdown / window_days / basis。
    basis 是给页面展示的"数据依据"一句人话。
    """
    rows = None
    chosen_code = None
    for code in MARKET_INDEX_FUNDS:
        got = (
            db.query(FundHistory.nav_date, FundHistory.nav, FundHistory.fund_name)
            .filter(FundHistory.fund_code == code)
            .order_by(FundHistory.nav_date.desc())
            .limit(WINDOW_DAYS + 1)
            .all()
        )
        if len(got) >= MIN_ROWS:
            rows = got
            chosen_code = code
            break

    if not rows or not chosen_code:
        return _unknown()

    # 查询按 nav_date 倒序，反转为升序
    navs = [float(r.nav) for r in reversed(rows)]
    if any(v <= 0 for v in navs):
        return _unknown()

    metrics = _metrics(navs)
    stance = classify_stance(metrics["r5"], metrics["r20"], metrics["drawdown"])
    nav_as_of = rows[0].nav_date  # 倒序第一条 = 最新净值日
    index_name = (rows[0].fund_name or "").strip() or chosen_code

    basis = (
        f"{index_name}（{chosen_code}）近5日 {metrics['r5']:+.2f}%、"
        f"近{metrics['window_days']}日 {metrics['r20']:+.2f}%、"
        f"自窗口高点回撤 {abs(metrics['drawdown']):.2f}%，净值截至 {nav_as_of.isoformat()}"
    )

    return {
        "stance": stance,
        "stance_label": STANCE_LABELS[stance],
        "position_label": POSITION_LABELS[stance],
        "enforce": stance in GATE_ENFORCED_STANCES,
        "index_code": chosen_code,
        "index_name": index_name,
        "nav_as_of": nav_as_of.isoformat(),
        "r5": metrics["r5"],
        "r20": metrics["r20"],
        "drawdown": metrics["drawdown"],
        "window_days": metrics["window_days"],
        "basis": basis,
    }


def _unknown() -> Dict[str, Any]:
    return {
        "stance": "unknown",
        "stance_label": STANCE_LABELS["unknown"],
        "position_label": POSITION_LABELS["unknown"],
        "enforce": False,
        "index_code": None,
        "index_name": None,
        "nav_as_of": None,
        "r5": None,
        "r20": None,
        "drawdown": None,
        "window_days": None,
        "basis": "宽基指数净值（510300/510050/510100）在库里不足 6 个交易日，闸自动失效",
    }


SECTOR_STATES_MAX = 20  # 板块状态最多算多少个（证据里出现的板块去重后截断，防无界查询）


def compute_sector_states(db: Session, sectors) -> Dict[str, Dict[str, Any]]:
    """板块**自身**强弱分档（2026-10-13 老板点名：大盘弱 ≠ 板块弱，板块在涨就该敢给买入）。

    每个板块 → 代表基金（与路由附挂标的同一把尺子 `get_fund_for_sector`，不另造映射）
    → 读 `fund_history` 近期净值 → **复用与大盘同一套阈值**（`_metrics` + `classify_stance`）。

    查不到映射 / 净值点不足 ⇒ stance='unknown'：不豁免（回到"弱势敢买就得写理由"）。
    豁免只认 stance='strong'——涨出来的是事实；range 不算"马上雄起"的免检证据。

    永不抛：单板块算不出就记 unknown，不拖垮整条证据链。
    """
    from src.constants.sector_fund_map import get_fund_for_sector

    out: Dict[str, Dict[str, Any]] = {}
    seen: List[str] = []
    for raw in sectors or []:
        s = (raw or "").strip()
        if s and s not in seen:
            seen.append(s)
    for sector in seen[:SECTOR_STATES_MAX]:
        try:
            out[sector] = _one_sector_state(db, sector, get_fund_for_sector)
        except Exception as exc:  # noqa: BLE001 — 证据链不因一个板块崩
            out[sector] = {"stance": "unknown", "fund_code": "", "fund_name": "",
                           "r5": None, "r20": None, "nav_as_of": None,
                           "basis": f"板块状态计算失败：{exc}"}
    return out


def _one_sector_state(db: Session, sector: str, get_fund_for_sector) -> Dict[str, Any]:
    hit = (get_fund_for_sector(sector) or {})
    code = str(hit.get("code") or "").strip()
    name_hint = str(hit.get("name") or "").strip()
    if not code:
        return {"stance": "unknown", "fund_code": "", "fund_name": name_hint,
                "r5": None, "r20": None, "nav_as_of": None,
                "basis": f"板块「{sector}」映射答不出代表基金，无法判它自己的强弱"}
    rows = (
        db.query(FundHistory.nav_date, FundHistory.nav, FundHistory.fund_name)
        .filter(FundHistory.fund_code == code)
        .order_by(FundHistory.nav_date.desc())
        .limit(WINDOW_DAYS + 1)
        .all()
    )
    if len(rows) < MIN_ROWS:
        return {"stance": "unknown", "fund_code": code, "fund_name": name_hint,
                "r5": None, "r20": None, "nav_as_of": None,
                "basis": f"{code} 净值不足 {MIN_ROWS} 个交易日，判不出板块强弱"}
    navs = [float(r.nav) for r in reversed(rows)]
    if any(v <= 0 for v in navs):
        return {"stance": "unknown", "fund_code": code, "fund_name": name_hint,
                "r5": None, "r20": None, "nav_as_of": None,
                "basis": f"{code} 净值数据异常（含非正数），判不出板块强弱"}
    metrics = _metrics(navs)
    stance = classify_stance(metrics["r5"], metrics["r20"], metrics["drawdown"])
    name = (rows[0].fund_name or name_hint or code).strip() or code
    return {
        "stance": stance,
        "stance_label": STANCE_LABELS[stance],
        "fund_code": code,
        "fund_name": name,
        "r5": metrics["r5"],
        "r20": metrics["r20"],
        "drawdown": metrics["drawdown"],
        "nav_as_of": rows[0].nav_date.isoformat(),
        "basis": (f"{name}（{code}）近5日 {metrics['r5']:+.2f}%、"
                  f"近{metrics['window_days']}日 {metrics['r20']:+.2f}%，"
                  f"净值截至 {rows[0].nav_date.isoformat()}"),
    }
