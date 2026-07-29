"""
L1 shadow 双跑：线上仍服务 legacy，额外构建 l1_beta 只记录不生效。

日志：JSONL → data/l1_shadow.jsonl + logging
复评：新增存活结论 ≥150 或自启动日起满 6 周（先到先评）
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from src.core.config import BASE_DIR, config
from src.models.database import Prediction
from src.services import l1_weighting as l1

logger = logging.getLogger(__name__)

SHADOW_LOG_PATH = BASE_DIR / "data" / "l1_shadow.jsonl"


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


def reeval_status(db: Session) -> Dict[str, Any]:
    """
    复评触发：新增验证结论 ≥ L1_SHADOW_REEVAL_NEW 或满 L1_SHADOW_REEVAL_WEEKS 周。
    基线：L1_SHADOW_BASELINE_VERIFIED + L1_SHADOW_STARTED_AT

    口径分层（other 改造交互）：
    - data_era=pre_other|post_other（L1_SHADOW_DATA_ERA）
    - other 上线须：设 L3_OTHER_CUTOVER_AT、把 era 切 post_other、
      重置 BASELINE_VERIFIED 与 STARTED_AT；禁止 pre/post 混进同一 +150 计数。
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
    }
