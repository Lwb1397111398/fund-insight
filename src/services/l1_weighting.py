"""
L1：存活命中率 Beta 收缩加权（纯函数 + 查询辅助）。

口径与 docs/L1_WEIGHTING_DESIGN.md / ACCURACY_METRIC_SPEC 一致。
feature flag 关闭时证据层不得调用本模块改变行为。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from src.models.database import Prediction

# 设计默认
DEFAULT_P0 = 0.609
DEFAULT_ALPHA = 15.0
DEFAULT_MIN_N = 10
DEFAULT_FLOOR_RATIO = 0.4
STRATEGY_VERSION = "l1.hit_beta.v1"
LEGACY_STRATEGY_VERSION = "p0.global_accuracy.v1"


def beta_hit_rate(
    correct: int,
    n: int,
    *,
    p0: float = DEFAULT_P0,
    alpha: float = DEFAULT_ALPHA,
) -> float:
    """Beta–Binomial 后验均值。(c + α p0) / (n + α)；n<=0 → p0。"""
    if n is None or int(n) <= 0:
        return float(p0)
    c = max(0, int(correct or 0))
    nn = int(n)
    a = float(alpha)
    return (c + a * float(p0)) / (nn + a)


def evidence_tier(n: int, *, min_n: int = DEFAULT_MIN_N) -> str:
    """n=0 neutral；0<n<min_n prior；否则 empirical。"""
    nn = int(n or 0)
    if nn <= 0:
        return "neutral"
    if nn < int(min_n):
        return "prior"
    return "empirical"


def legacy_reliability_score(accuracy_rate: float, total_predictions: int) -> float:
    """
    精确复刻现网 AdviceEvidenceBuilder 公式（legacy 臂 / flag 关路径）：
    reliability = 50 + (acc - 50) * min(1, sample/10)
    sample=0 时 shrink=0 → 50（补全 missing 分支用 0 shrink 同结果）。
    """
    sample = int(total_predictions or 0)
    acc = float(accuracy_rate or 0.0)
    shrink = min(1.0, sample / 10.0) if sample else 0.0
    return 50.0 + (acc - 50.0) * shrink


def prediction_weight_from_reliability(
    reliability_0_100: float,
    confidence: Optional[float],
    *,
    weight_floor: Optional[float] = None,
) -> Tuple[float, bool]:
    """
    w = (rel/100)*(conf/100)；可选 floor。
    返回 (weight, floored)。
    """
    conf = float(confidence if confidence is not None else 50)
    rel = float(reliability_0_100 if reliability_0_100 is not None else 50)
    w_raw = (rel / 100.0) * (conf / 100.0)
    if weight_floor is None:
        return round(w_raw, 4), False
    floor = float(weight_floor)
    w = max(floor, w_raw)
    floored = w > w_raw + 1e-12
    return round(w, 4), floored


def compute_weight_floor(
    p_hats: Sequence[float],
    *,
    floor_ratio: float = DEFAULT_FLOOR_RATIO,
) -> float:
    """relative：floor = floor_ratio * mean(p̂)；无样本时 0。"""
    vals = [float(x) for x in p_hats if x is not None]
    if not vals:
        return 0.0
    return float(floor_ratio) * (sum(vals) / len(vals))


@dataclass(frozen=True)
class HitStats:
    blogger_id: int
    hit_correct: int
    hit_verified: int

    @property
    def hit_rate_raw(self) -> Optional[float]:
        if self.hit_verified <= 0:
            return None
        return self.hit_correct / self.hit_verified


def query_hit_stats(
    db: Session,
    blogger_ids: Optional[Iterable[int]] = None,
    *,
    as_of_exclusive: Optional[date] = None,
) -> Dict[int, HitStats]:
    """
    存活命中：is_deleted=false 且 is_correct 非空。
    as_of_exclusive：仅计入「结论落地日 < as_of」的样本（防回测泄漏）。
    落地日 = COALESCE(verified_at::date, target_date, prediction_date)。
    """
    q = db.query(
        Prediction.blogger_id,
        func.count(case((Prediction.is_correct.isnot(None), 1))).label("verified"),
        func.count(case((Prediction.is_correct.is_(True), 1))).label("correct"),
    ).filter(
        Prediction.is_deleted == False,  # noqa: E712
        Prediction.is_correct.isnot(None),
    )
    if blogger_ids is not None:
        ids = list(blogger_ids)
        if not ids:
            return {}
        q = q.filter(Prediction.blogger_id.in_(ids))
    if as_of_exclusive is not None:
        # verified_at 可能是 datetime
        verified_day = func.coalesce(
            func.date(Prediction.verified_at),
            Prediction.target_date,
            Prediction.prediction_date,
        )
        q = q.filter(verified_day < as_of_exclusive)
    rows = q.group_by(Prediction.blogger_id).all()
    out: Dict[int, HitStats] = {}
    for bid, verified, correct in rows:
        if bid is None:
            continue
        out[int(bid)] = HitStats(
            blogger_id=int(bid),
            hit_correct=int(correct or 0),
            hit_verified=int(verified or 0),
        )
    return out


def build_l1_blogger_item(
    *,
    blogger_id: int,
    name: str,
    grade: Optional[str],
    accuracy_rate: float,
    total_predictions: int,
    correct_predictions: int,
    hit: Optional[HitStats],
    p0: float = DEFAULT_P0,
    alpha: float = DEFAULT_ALPHA,
    min_n: int = DEFAULT_MIN_N,
) -> Dict:
    """构造证据层博主字典（L1）。"""
    n = int(hit.hit_verified) if hit else 0
    c = int(hit.hit_correct) if hit else 0
    raw = (c / n) if n > 0 else None
    p_hat = beta_hit_rate(c, n, p0=p0, alpha=alpha)
    tier = evidence_tier(n, min_n=min_n)
    return {
        "blogger_id": blogger_id,
        "name": name,
        "accuracy_rate": float(accuracy_rate or 0.0),  # 物化加权分，次列审计
        "grade": grade or "C",
        "total_predictions": int(total_predictions or 0),
        "correct_predictions": int(correct_predictions or 0),
        "hit_correct": c,
        "hit_verified": n,
        "hit_rate_raw": round(raw * 100, 2) if raw is not None else None,
        "hit_rate_shrunk": round(p_hat * 100, 2),
        "p_hat": p_hat,
        "evidence_tier": tier,
        "reliability_score": round(p_hat * 100, 2),
        "sector_accuracy": None,
        "horizon_accuracy": None,
    }


def sort_key_l1(item: Dict) -> Tuple:
    """empirical 优先，再按 p_hat、n。"""
    tier = item.get("evidence_tier") or "neutral"
    tier_rank = 0 if tier == "empirical" else (1 if tier == "prior" else 2)
    p_hat = float(item.get("p_hat") or 0.0)
    n = int(item.get("hit_verified") or 0)
    return (tier_rank, -p_hat, -n)


def conclusion_as_of(pred) -> Optional[date]:
    """单条预测结论落地日，供回测防泄漏。"""
    va = getattr(pred, "verified_at", None)
    if isinstance(va, datetime):
        return va.date()
    if isinstance(va, date):
        return va
    td = getattr(pred, "target_date", None)
    if isinstance(td, date):
        return td
    pd = getattr(pred, "prediction_date", None)
    if isinstance(pd, date):
        return pd
    return None
