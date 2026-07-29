"""
L1 shadow 双跑：线上仍服务 legacy，额外构建 l1_beta 只记录不生效。

日志：JSONL → data/l1_shadow.jsonl + logging
复评：新增存活结论 ≥150 或自启动日起满 6 周（先到先评）
复评清单含：tier 分离度（empirical vs prior 命中/收益）
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.config import BASE_DIR, config
from src.models.database import Prediction
from src.services import l1_weighting as l1

logger = logging.getLogger(__name__)

SHADOW_LOG_PATH = BASE_DIR / "data" / "l1_shadow.jsonl"
# 与 L3/loud-calls 备忘录一致的 empirical 门槛
TIER_EMPIRICAL_MIN_N = 10


def shadow_enabled() -> bool:
    return bool(getattr(config, "ADVICE_L1_SHADOW", True)) and not bool(
        getattr(config, "ADVICE_L1_HIT_WEIGHTING", False)
    )


def summarize_shadow(
    *,
    as_of: date,
    legacy_pack_meta: Dict[str, Any],
    legacy_predictions: List[Dict[str, Any]],
    l1_predictions: List[Dict[str, Any]],
    l1_bloggers: List[Dict[str, Any]],
    l1_meta: Dict[str, Any],
) -> Dict[str, Any]:
    """压缩对比：权重差、top 博主、策略戳。不含全文证据。"""
    leg_w = {
        p.get("prediction_id"): float(p.get("weight") or 0)
        for p in legacy_predictions
        if p.get("prediction_id") is not None
    }
    l1_w = {
        p.get("prediction_id"): float(p.get("weight") or 0)
        for p in l1_predictions
        if p.get("prediction_id") is not None
    }
    ids = sorted(set(leg_w) | set(l1_w))
    diffs = []
    for pid in ids:
        a, b = leg_w.get(pid, 0.0), l1_w.get(pid, 0.0)
        if abs(a - b) > 1e-6:
            diffs.append(
                {
                    "prediction_id": pid,
                    "legacy_w": round(a, 4),
                    "l1_w": round(b, 4),
                    "delta": round(b - a, 4),
                }
            )
    diffs.sort(key=lambda x: abs(x["delta"]), reverse=True)

    top_l1 = sorted(
        l1_bloggers,
        key=lambda b: float(b.get("p_hat") or b.get("reliability_score") or 0),
        reverse=True,
    )[:5]

    return {
        "as_of": as_of.isoformat(),
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
        "serving_strategy": legacy_pack_meta.get("weight_strategy_version")
        or l1.LEGACY_STRATEGY_VERSION,
        "shadow_strategy": l1.STRATEGY_VERSION,
        "l1_meta_version": l1_meta.get("weight_strategy_version"),
        "legacy_prediction_count": len(legacy_predictions),
        "l1_prediction_count": len(l1_predictions),
        "weight_diff_count": len(diffs),
        "top_weight_diffs": diffs[:15],
        "l1_top_bloggers": [
            {
                "blogger_id": b.get("blogger_id"),
                "name": b.get("name"),
                "p_hat": b.get("p_hat"),
                "hit_verified": b.get("hit_verified"),
                "evidence_tier": b.get("evidence_tier"),
                "reliability_score": b.get("reliability_score"),
            }
            for b in top_l1
        ],
    }


def write_shadow_log(summary: Dict[str, Any]) -> None:
    """追加 JSONL；失败只打日志不抛。"""
    try:
        SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SHADOW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("L1 shadow JSONL 写入失败: %s", e)
    logger.info(
        "L1 shadow recorded strategy=%s diffs=%s preds_l1=%s",
        summary.get("shadow_strategy"),
        summary.get("weight_diff_count"),
        summary.get("l1_prediction_count"),
    )


def count_alive_verified(db: Session) -> int:
    return (
        db.query(func.count(Prediction.id))
        .filter(
            Prediction.is_deleted == False,  # noqa: E712
            Prediction.is_correct.isnot(None),
        )
        .scalar()
        or 0
    )


def _directional_return(pred: Prediction) -> Optional[float]:
    try:
        s = float(pred.start_nav) if pred.start_nav is not None else None
        e = float(pred.current_nav) if pred.current_nav is not None else None
    except (TypeError, ValueError):
        return None
    if s is None or e is None or s <= 0:
        return None
    raw = e / s - 1.0
    p = (pred.prediction_type or "").lower()
    if p in ("up", "bullish"):
        return raw
    if p in ("down", "bearish"):
        return -raw
    return None


def tier_separation_report(
    db: Session,
    *,
    min_n: int = TIER_EMPIRICAL_MIN_N,
) -> Dict[str, Any]:
    """
    L1 复评清单项：empirical vs prior 分层命中率/方向收益（只读聚合）。

    tier 定义与 loud-calls / L3 附录 C 一致：
    - 按 blogger 存活已结论数：empirical(n>=min_n) / prior(1..min_n-1)
    - 每条已结论归入其博主 tier

    判读提示（不自动开闸）：
    - empirical 命中与收益均不明显优于 prior → 分层/战绩加权根基存疑
    - 详见 docs/SIGNAL_LOUD_CALLS_MEMO.md §5
    """
    rows = (
        db.query(Prediction)
        .filter(
            Prediction.is_deleted == False,  # noqa: E712
            Prediction.is_correct.isnot(None),
        )
        .all()
    )
    by_b: Dict[int, List[Prediction]] = defaultdict(list)
    for p in rows:
        if p.blogger_id is None:
            continue
        by_b[int(p.blogger_id)].append(p)

    blogger_tier: Dict[int, str] = {}
    for bid, plist in by_b.items():
        n = len(plist)
        if n >= min_n:
            blogger_tier[bid] = "empirical"
        elif n >= 1:
            blogger_tier[bid] = "prior"
        else:
            blogger_tier[bid] = "neutral"

    buckets: Dict[str, List[Prediction]] = defaultdict(list)
    for p in rows:
        if p.blogger_id is None:
            continue
        t = blogger_tier.get(int(p.blogger_id), "neutral")
        if t in ("empirical", "prior"):
            buckets[t].append(p)

    def _stats(items: List[Prediction]) -> Dict[str, Any]:
        n = len(items)
        if n == 0:
            return {"n": 0, "hit_rate": None, "avg_return": None, "return_n": 0}
        correct = sum(1 for p in items if bool(p.is_correct))
        rets = [_directional_return(p) for p in items]
        ok = [r for r in rets if r is not None]
        return {
            "n": n,
            "hit_rate": round(correct / n, 4),
            "avg_return": round(sum(ok) / len(ok), 6) if ok else None,
            "return_n": len(ok),
        }

    emp = _stats(buckets.get("empirical", []))
    pri = _stats(buckets.get("prior", []))

    hit_gap = None
    ret_gap = None
    if emp["hit_rate"] is not None and pri["hit_rate"] is not None:
        hit_gap = round(emp["hit_rate"] - pri["hit_rate"], 4)
    if emp["avg_return"] is not None and pri["avg_return"] is not None:
        ret_gap = round(emp["avg_return"] - pri["avg_return"], 6)

    separates = None
    if hit_gap is not None:
        separates = bool(hit_gap > 0.02 or (ret_gap is not None and ret_gap > 0))
    warning = None
    if hit_gap is not None and hit_gap <= 0 and (ret_gap is None or ret_gap <= 0):
        warning = (
            "empirical 未优于 prior（命中 gap<=0 且收益未胜）→ "
            "战绩分层/L1 加权根基存疑，并入复评讨论"
        )

    return {
        "min_n": min_n,
        "empirical": emp,
        "prior": pri,
        "hit_rate_gap_emp_minus_pri": hit_gap,
        "avg_return_gap_emp_minus_pri": ret_gap,
        "empirical_separates_from_prior": separates,
        "warning": warning,
        "checklist_item": "tier_separation",
        "note": "L1 复评必看项；与 loud-calls 备忘录共用 tier 定义",
    }


def reeval_status(db: Session) -> Dict[str, Any]:
    """
    复评触发 + era 分层 + tier 分离度清单。
    """
    baseline = int(getattr(config, "L1_SHADOW_BASELINE_VERIFIED", 220))
    started_s = str(getattr(config, "L1_SHADOW_STARTED_AT", "") or "")
    try:
        started = date.fromisoformat(started_s[:10]) if started_s else date.today()
    except ValueError:
        started = date.today()
    need_n = int(getattr(config, "L1_SHADOW_REEVAL_NEW", 150))
    need_weeks = int(getattr(config, "L1_SHADOW_REEVAL_WEEKS", 6))
    era = str(getattr(config, "L1_SHADOW_DATA_ERA", "pre_other") or "pre_other").strip()
    cutover = str(getattr(config, "L3_OTHER_CUTOVER_AT", "") or "").strip() or None

    current = count_alive_verified(db)
    new_n = max(0, current - baseline)
    elapsed_days = (date.today() - started).days
    by_count = new_n >= need_n
    by_time = elapsed_days >= need_weeks * 7
    due = by_count or by_time

    era_note = (
        "当前 pre_other：复评计数含改造前口径；other 上线须清零 baseline 并切 post_other"
        if era == "pre_other"
        else "当前 post_other：仅计改造后口径；勿与 pre_other 历史混加"
    )
    if cutover and era == "pre_other":
        era_note += f"；已配置 L3_OTHER_CUTOVER_AT={cutover} 但仍为 pre_other，请重置 baseline/era"
    if not cutover and era == "post_other":
        era_note += "；post_other 但未写 cutover 日，请补 L3_OTHER_CUTOVER_AT"

    try:
        tier_sep = tier_separation_report(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("tier_separation_report failed: %s", e)
        tier_sep = {"error": str(e), "checklist_item": "tier_separation"}

    return {
        "due": due,
        "reason": (
            "new_verified>=" + str(need_n)
            if by_count
            else ("weeks>=" + str(need_weeks) if by_time else "not_due")
        ),
        "baseline_verified": baseline,
        "current_verified": current,
        "new_verified": new_n,
        "need_new": need_n,
        "started_at": started.isoformat(),
        "elapsed_days": elapsed_days,
        "need_weeks": need_weeks,
        "by_count": by_count,
        "by_time": by_time,
        "data_era": era,
        "other_cutover_at": cutover,
        "era_note": era_note,
        "tier_separation": tier_sep,
        "reeval_checklist": [
            "l1_beta vs equal/legacy 命中+收益（walk-forward）",
            "tier_separation empirical vs prior",
            "data_era 未混计 pre/post other",
            "loud-calls 备忘录是否仍只读（观察期禁动权重）",
        ],
    }
