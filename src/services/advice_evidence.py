"""
投资建议证据集（P0）

选数硬约束：
- 当前方向信号只调用 prediction_lifecycle.filter_actionable_current
- 不得复制 is_expired / target_date 过滤条件
- 一人多预测服务层聚合；观点作者限流
- as_of 统一来自 current_as_of()
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.core.config import config
from src.models.database import Blogger, FundInfo, Prediction, Viewpoint
from src.services import l1_weighting as l1
from src.services.prediction_lifecycle import (
    ACTIVE,
    classify,
    current_as_of,
    filter_actionable_current,
)


@dataclass
class EvidencePack:
    """规范化、可审计的建议输入证据。"""

    as_of_date: date
    bloggers: List[Dict[str, Any]] = field(default_factory=list)
    predictions: List[Dict[str, Any]] = field(default_factory=list)
    viewpoints: List[Dict[str, Any]] = field(default_factory=list)
    funds: List[Dict[str, Any]] = field(default_factory=list)
    exclusions: List[Dict[str, Any]] = field(default_factory=list)
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    evidence_hash: Optional[str] = None

    def to_llm_input(self) -> Dict[str, Any]:
        """三阶段兼容输入（保持 bloggers/predictions/viewpoints 键）。"""
        return {
            "bloggers": self.bloggers,
            "predictions": self.predictions,
            "viewpoints": self.viewpoints,
            "funds": self.funds,
            "as_of_date": self.as_of_date.isoformat(),
            "conflicts": self.conflicts,
            "meta": self.meta,
        }

    def to_audit_dict(self) -> Dict[str, Any]:
        raw = asdict(self)
        raw["as_of_date"] = self.as_of_date.isoformat()
        return raw


class AdviceEvidenceBuilder:
    """构建 EvidencePack；供 AdviceService / 三阶段使用。"""

    def __init__(
        self,
        db: Session,
        *,
        min_accuracy: float = 50.0,
        min_blogger_samples: int = 3,
        top_bloggers: int = 15,
        near_days: int = 7,
        mid_days: int = 30,
        mid_limit: int = 20,
        max_predictions: int = 30,
        recent_viewpoints_days: int = 7,
        top_viewpoints: int = 50,
        max_viewpoints_per_author: int = 3,
        max_predictions_per_blogger: int = 3,
    ):
        self.db = db
        self.min_accuracy = min_accuracy
        self.min_blogger_samples = min_blogger_samples
        self.top_bloggers = top_bloggers
        self.near_days = near_days
        self.mid_days = mid_days
        self.mid_limit = mid_limit
        self.max_predictions = max_predictions
        self.recent_viewpoints_days = recent_viewpoints_days
        self.top_viewpoints = top_viewpoints
        self.max_viewpoints_per_author = max_viewpoints_per_author
        self.max_predictions_per_blogger = max_predictions_per_blogger

    def build(
        self,
        as_of: Optional[date] = None,
        *,
        force_l1: Optional[bool] = None,
        run_shadow: Optional[bool] = None,
    ) -> EvidencePack:
        """
        构建证据包。
        force_l1: 覆盖 flag（shadow 双跑用）。
        run_shadow: 默认跟 config.ADVICE_L1_SHADOW；force_l1 指定时不套娃 shadow。
        """
        as_of = as_of or current_as_of()
        use_l1 = (
            bool(force_l1)
            if force_l1 is not None
            else bool(getattr(config, "ADVICE_L1_HIT_WEIGHTING", False))
        )
        pack = self._build_pack(as_of=as_of, use_l1=use_l1)

        do_shadow = run_shadow if run_shadow is not None else None
        if do_shadow is None:
            from src.services.l1_shadow import shadow_enabled

            do_shadow = shadow_enabled() and force_l1 is None and not use_l1

        if do_shadow:
            try:
                from src.services.l1_shadow import summarize_shadow, write_shadow_log

                shadow_pack = self._build_pack(as_of=as_of, use_l1=True)
                summary = summarize_shadow(
                    as_of=as_of,
                    legacy_pack_meta=pack.meta,
                    legacy_predictions=pack.predictions,
                    l1_predictions=shadow_pack.predictions,
                    l1_bloggers=shadow_pack.bloggers,
                    l1_meta=shadow_pack.meta,
                )
                write_shadow_log(summary)
                pack.meta["l1_shadow"] = summary
                pack.meta["l1_shadow_enabled"] = True
            except Exception as e:  # noqa: BLE001 — shadow 失败不得影响主路径
                pack.meta["l1_shadow_enabled"] = True
                pack.meta["l1_shadow_error"] = str(e)

        return pack

    def _build_pack(self, *, as_of: date, use_l1: bool) -> EvidencePack:
        exclusions: List[Dict[str, Any]] = []

        if use_l1:
            bloggers, blogger_map, blogger_excl = self._build_blogger_reliability_l1()
        else:
            bloggers, blogger_map, blogger_excl = self._build_blogger_reliability_legacy()
        exclusions.extend(blogger_excl)

        predictions, pred_excl, pred_truncated = self._build_predictions(
            as_of=as_of, blogger_map=blogger_map, use_l1=use_l1
        )
        exclusions.extend(pred_excl)

        viewpoints, vp_excl, vp_truncated = self._build_viewpoints(as_of=as_of)
        exclusions.extend(vp_excl)

        funds = self._build_funds()
        conflicts = self._detect_conflicts(predictions, viewpoints)

        pack = EvidencePack(
            as_of_date=as_of,
            bloggers=bloggers,
            predictions=predictions,
            viewpoints=viewpoints,
            funds=funds,
            exclusions=exclusions,
            conflicts=conflicts,
            meta={
                "prediction_truncated": pred_truncated,
                "viewpoint_truncated": vp_truncated,
                "blogger_count": len(bloggers),
                "prediction_count": len(predictions),
                "viewpoint_count": len(viewpoints),
                "exclusion_count": len(exclusions),
                "conflict_count": len(conflicts),
                "weight_strategy_version": (
                    l1.STRATEGY_VERSION if use_l1 else l1.LEGACY_STRATEGY_VERSION
                ),
                "l1_hit_weighting": use_l1,
                "insufficient_evidence": len(predictions) == 0 and len(viewpoints) == 0,
            },
        )
        pack.evidence_hash = self._hash_pack(pack)
        return pack

    # ------------------------------------------------------------------ bloggers
    def _build_blogger_reliability(
        self,
    ) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
        if bool(getattr(config, "ADVICE_L1_HIT_WEIGHTING", False)):
            return self._build_blogger_reliability_l1()
        return self._build_blogger_reliability_legacy()

    def _build_blogger_reliability_legacy(
        self,
    ) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
        """现网行为：accuracy_rate=加权评分 + 简单 n/10 收缩。"""
        exclusions: List[Dict[str, Any]] = []
        q = (
            self.db.query(Blogger)
            .filter(
                Blogger.accuracy_rate >= self.min_accuracy,
                Blogger.total_predictions >= self.min_blogger_samples,
            )
            .order_by(Blogger.accuracy_rate.desc())
        )
        rows = q.limit(self.top_bloggers).all()

        if not rows:
            rows = (
                self.db.query(Blogger)
                .filter(Blogger.total_predictions >= 1)
                .order_by(Blogger.accuracy_rate.desc())
                .limit(self.top_bloggers)
                .all()
            )
            exclusions.append(
                {
                    "reason": "blogger_fallback_low_sample",
                    "detail": "无满足 accuracy/样本门槛的博主，回退 total>=1",
                }
            )

        if not rows:
            rows = (
                self.db.query(Blogger)
                .order_by(Blogger.accuracy_rate.desc())
                .limit(self.top_bloggers)
                .all()
            )
            exclusions.append(
                {
                    "reason": "blogger_fallback_any",
                    "detail": "无样本博主，回退任意 topN",
                }
            )

        bloggers: List[Dict[str, Any]] = []
        blogger_map: Dict[int, Dict[str, Any]] = {}
        for b in rows:
            sample = int(b.total_predictions or 0)
            acc = float(b.accuracy_rate or 0.0)
            reliability = l1.legacy_reliability_score(acc, sample)
            item = {
                "blogger_id": b.id,
                "name": b.name,
                "accuracy_rate": acc,
                "grade": b.grade or "C",
                "total_predictions": sample,
                "correct_predictions": int(b.correct_predictions or 0),
                "reliability_score": round(reliability, 2),
                "sector_accuracy": None,
                "horizon_accuracy": None,
            }
            bloggers.append(item)
            blogger_map[b.id] = item
        return bloggers, blogger_map, exclusions

    def _build_blogger_reliability_l1(
        self,
    ) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
        """L1：存活 is_correct + Beta 收缩；min_n 分层排序。"""
        exclusions: List[Dict[str, Any]] = []
        p0 = float(getattr(config, "L1_P0", l1.DEFAULT_P0))
        alpha = float(getattr(config, "L1_ALPHA", l1.DEFAULT_ALPHA))
        min_n = int(getattr(config, "L1_MIN_N", l1.DEFAULT_MIN_N))

        # 候选：优先 is_active；不足再全表
        rows = (
            self.db.query(Blogger)
            .filter(Blogger.is_active == True)  # noqa: E712
            .all()
        )
        if not rows:
            rows = self.db.query(Blogger).all()
            exclusions.append(
                {
                    "reason": "blogger_fallback_any",
                    "detail": "无 is_active 博主，回退全表后按 L1 排序截断",
                }
            )

        hit_map = l1.query_hit_stats(self.db, [b.id for b in rows])
        items: List[Dict[str, Any]] = []
        for b in rows:
            items.append(
                l1.build_l1_blogger_item(
                    blogger_id=b.id,
                    name=b.name,
                    grade=b.grade,
                    accuracy_rate=float(b.accuracy_rate or 0.0),
                    total_predictions=int(b.total_predictions or 0),
                    correct_predictions=int(b.correct_predictions or 0),
                    hit=hit_map.get(b.id),
                    p0=p0,
                    alpha=alpha,
                    min_n=min_n,
                )
            )
        items.sort(key=l1.sort_key_l1)
        selected = items[: self.top_bloggers]
        if len(items) > self.top_bloggers:
            exclusions.append(
                {
                    "reason": "blogger_l1_top_truncated",
                    "detail": f"L1 按 tier/p_hat 截断 top {self.top_bloggers}",
                }
            )

        blogger_map = {int(it["blogger_id"]): it for it in selected}
        return selected, blogger_map, exclusions

    # --------------------------------------------------------------- predictions
    def _build_predictions(
        self,
        *,
        as_of: date,
        blogger_map: Dict[int, Dict[str, Any]],
        use_l1: bool = False,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
        exclusions: List[Dict[str, Any]] = []
        raw = filter_actionable_current(
            self.db,
            as_of=as_of,
            near_days=self.near_days,
            mid_days=self.mid_days,
            mid_limit=self.mid_limit,
        )

        # 补全不在 reliability 池中的博主（活跃预测博主）
        missing_ids = {p.blogger_id for p in raw if p.blogger_id not in blogger_map}
        if missing_ids:
            extra = self.db.query(Blogger).filter(Blogger.id.in_(list(missing_ids))).all()
            if use_l1:
                hit_map = l1.query_hit_stats(self.db, list(missing_ids))
                p0 = float(getattr(config, "L1_P0", l1.DEFAULT_P0))
                alpha = float(getattr(config, "L1_ALPHA", l1.DEFAULT_ALPHA))
                min_n = int(getattr(config, "L1_MIN_N", l1.DEFAULT_MIN_N))
                for b in extra:
                    blogger_map[b.id] = l1.build_l1_blogger_item(
                        blogger_id=b.id,
                        name=b.name,
                        grade=b.grade,
                        accuracy_rate=float(b.accuracy_rate or 0.0),
                        total_predictions=int(b.total_predictions or 0),
                        correct_predictions=int(b.correct_predictions or 0),
                        hit=hit_map.get(b.id),
                        p0=p0,
                        alpha=alpha,
                        min_n=min_n,
                    )
            else:
                for b in extra:
                    sample = int(b.total_predictions or 0)
                    acc = float(b.accuracy_rate or 0.0)
                    reliability = l1.legacy_reliability_score(acc, sample)
                    blogger_map[b.id] = {
                        "blogger_id": b.id,
                        "name": b.name,
                        "accuracy_rate": acc,
                        "grade": b.grade or "C",
                        "total_predictions": sample,
                        "correct_predictions": int(b.correct_predictions or 0),
                        "reliability_score": round(reliability, 2),
                        "sector_accuracy": None,
                        "horizon_accuracy": None,
                    }

        # 按博主聚合/限流，防一人多票
        by_blogger: Dict[int, List[Prediction]] = defaultdict(list)
        for p in raw:
            by_blogger[p.blogger_id or 0].append(p)

        selected: List[Prediction] = []
        for blogger_id, items in by_blogger.items():
            items_sorted = sorted(
                items,
                key=lambda x: (
                    0 if (x.target_date and (x.target_date - as_of).days <= self.near_days) else 1,
                    x.target_date or date.max,
                    -(x.confidence or 0),
                ),
            )
            keep = items_sorted[: self.max_predictions_per_blogger]
            drop = items_sorted[self.max_predictions_per_blogger :]
            selected.extend(keep)
            for d in drop:
                exclusions.append(
                    {
                        "reason": "prediction_per_blogger_cap",
                        "prediction_id": d.id,
                        "blogger_id": blogger_id,
                        "detail": f"同一博主超过 {self.max_predictions_per_blogger} 条，已截断",
                    }
                )

        # 全局截断
        selected_sorted = sorted(
            selected,
            key=lambda x: (
                0 if (x.target_date and (x.target_date - as_of).days <= self.near_days) else 1,
                x.target_date or date.max,
            ),
        )
        truncated = False
        if len(selected_sorted) > self.max_predictions:
            for d in selected_sorted[self.max_predictions :]:
                exclusions.append(
                    {
                        "reason": "prediction_global_cap",
                        "prediction_id": d.id,
                        "detail": f"全局上限 {self.max_predictions}",
                    }
                )
            selected_sorted = selected_sorted[: self.max_predictions]
            truncated = True

        weight_floor = None
        if use_l1:
            p_hats = [
                float(blogger_map[bid]["p_hat"])
                for bid in {p.blogger_id for p in selected_sorted if p.blogger_id in blogger_map}
                if blogger_map[bid].get("p_hat") is not None
            ]
            weight_floor = l1.compute_weight_floor(
                p_hats,
                floor_ratio=float(getattr(config, "L1_FLOOR_RATIO", l1.DEFAULT_FLOOR_RATIO)),
            )

        out: List[Dict[str, Any]] = []
        for p in selected_sorted:
            binfo = blogger_map.get(p.blogger_id) or {
                "name": "未知",
                "grade": "C",
                "accuracy_rate": 0.0,
                "reliability_score": 50.0,
            }
            days = (p.target_date - as_of).days if p.target_date else 0
            conf = float(p.confidence or 50)
            rel = float(binfo.get("reliability_score") or 50)
            if use_l1:
                weight, floored = l1.prediction_weight_from_reliability(
                    rel, conf, weight_floor=weight_floor
                )
            else:
                # 合成权重：可靠性 × 自身信心（现网）
                weight = round((rel / 100.0) * (conf / 100.0), 4)
                floored = False
            row = {
                "prediction_id": p.id,
                "blogger_id": p.blogger_id,
                "blogger_name": binfo.get("name") or "未知",
                "blogger_grade": binfo.get("grade") or "C",
                "blogger_accuracy": binfo.get("accuracy_rate") or 0,
                "blogger_reliability": rel,
                "sector": p.sector,
                "prediction_type": p.prediction_type,
                "prediction_content": p.prediction_content or "",
                "confidence": p.confidence,
                "status": p.status,
                "lifecycle": classify(p, as_of=as_of),
                "prediction_date": p.prediction_date.isoformat()
                if p.prediction_date
                else None,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "days_to_target": days,
                "term": "near" if days <= self.near_days else "mid",
                "weight": weight,
            }
            if use_l1:
                row["weight_floored"] = floored
                row["blogger_p_hat"] = binfo.get("p_hat")
                row["evidence_tier"] = binfo.get("evidence_tier")
            out.append(row)
        return out, exclusions, truncated

    # ---------------------------------------------------------------- viewpoints
    def _build_viewpoints(
        self, *, as_of: date
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
        exclusions: List[Dict[str, Any]] = []
        start = as_of - timedelta(days=self.recent_viewpoints_days)

        summary_dates = self.db.query(Viewpoint.viewpoint_date).filter(
            Viewpoint.is_summary == True,
            Viewpoint.is_deleted == False,
        )
        rows = (
            self.db.query(Viewpoint)
            .filter(
                Viewpoint.viewpoint_date >= start,
                Viewpoint.is_deleted == False,
                (Viewpoint.valid_until == None) | (Viewpoint.valid_until >= as_of),
                (Viewpoint.is_summary == True)
                | (
                    (Viewpoint.reasoning.isnot(None))
                    & (Viewpoint.summary.isnot(None))
                    & (~Viewpoint.viewpoint_date.in_(summary_dates))
                ),
            )
            .order_by(Viewpoint.viewpoint_date.desc())
            .all()
        )

        # 内容指纹去重（summary/content 前 80 字）
        seen_fp = set()
        deduped: List[Viewpoint] = []
        for v in rows:
            text = (v.summary or v.content or "").strip()[:80]
            fp = hashlib.md5(text.encode("utf-8")).hexdigest() if text else f"id:{v.id}"
            if fp in seen_fp:
                exclusions.append(
                    {
                        "reason": "viewpoint_duplicate_content",
                        "viewpoint_id": v.id,
                        "detail": "相似/重复内容",
                    }
                )
                continue
            seen_fp.add(fp)
            deduped.append(v)

        # 作者限流
        by_author: Dict[str, List[Viewpoint]] = defaultdict(list)
        for v in deduped:
            key = (v.author or v.source or "unknown").strip().lower()
            by_author[key].append(v)

        limited: List[Viewpoint] = []
        for author, items in by_author.items():
            items_sorted = sorted(
                items,
                key=lambda x: (
                    -(x.credibility_score or 50),
                    -(x.confidence or 50),
                    x.viewpoint_date or date.min,
                ),
                reverse=False,
            )
            # credibility 高优先：按 -score 已在 key 中
            items_sorted = sorted(
                items,
                key=lambda x: (
                    -(float(x.credibility_score or 50)),
                    -(float(x.confidence or 50)),
                    -(x.viewpoint_date.toordinal() if x.viewpoint_date else 0),
                ),
            )
            keep = items_sorted[: self.max_viewpoints_per_author]
            limited.extend(keep)
            for d in items_sorted[self.max_viewpoints_per_author :]:
                exclusions.append(
                    {
                        "reason": "viewpoint_author_cap",
                        "viewpoint_id": d.id,
                        "author": author,
                        "detail": f"同一作者超过 {self.max_viewpoints_per_author} 条",
                    }
                )

        limited_sorted = sorted(
            limited,
            key=lambda x: (
                -(float(x.credibility_score or 50) * float(x.weight or 1.0)),
                -(x.viewpoint_date.toordinal() if x.viewpoint_date else 0),
            ),
        )
        truncated = False
        if len(limited_sorted) > self.top_viewpoints:
            for d in limited_sorted[self.top_viewpoints :]:
                exclusions.append(
                    {
                        "reason": "viewpoint_global_cap",
                        "viewpoint_id": d.id,
                        "detail": f"全局上限 {self.top_viewpoints}",
                    }
                )
            limited_sorted = limited_sorted[: self.top_viewpoints]
            truncated = True

        out: List[Dict[str, Any]] = []
        for v in limited_sorted:
            cred = float(v.credibility_score or 50)
            w = float(v.weight or 1.0)
            effective = int(cred * w)
            out.append(
                {
                    "viewpoint_id": v.id,
                    "source": v.source,
                    "author": v.author,
                    "market_direction": v.market_direction,
                    "confidence": v.confidence,
                    "credibility_score": v.credibility_score or 50,
                    "weight": v.weight or 1.0,
                    "effective_score": effective,
                    "sectors_bullish": v.sectors_bullish or [],
                    "sectors_bearish": v.sectors_bearish or [],
                    "summary": v.summary
                    if v.summary
                    else ((v.content[:500] if v.content else "")),
                    "reasoning": v.reasoning or "",
                    "is_summary": bool(v.is_summary),
                    "viewpoint_date": v.viewpoint_date.isoformat()
                    if v.viewpoint_date
                    else None,
                }
            )
        return out, exclusions, truncated

    # -------------------------------------------------------------------- funds
    def _build_funds(self) -> List[Dict[str, Any]]:
        funds = (
            self.db.query(FundInfo)
            .filter(FundInfo.latest_nav.isnot(None))
            .order_by(FundInfo.day_growth.desc())
            .limit(10)
            .all()
        )
        return [
            {
                "fund_code": f.fund_code,
                "fund_name": f.fund_name,
                "sector_type": f.sector_type,
                "day_growth": f.day_growth,
                "week_growth": f.week_growth,
                "month_growth": f.month_growth,
            }
            for f in funds
        ]

    # ---------------------------------------------------------------- conflicts
    def _detect_conflicts(
        self, predictions: List[Dict[str, Any]], viewpoints: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """板块级：高权预测方向 vs 观点方向简单冲突标记。"""
        pred_dir: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"up": 0.0, "down": 0.0}
        )
        for p in predictions:
            sector = (p.get("sector") or "").strip()
            if not sector:
                continue
            t = (p.get("prediction_type") or "").lower()
            w = float(p.get("weight") or 0)
            if t == "up":
                pred_dir[sector]["up"] += w
            elif t == "down":
                pred_dir[sector]["down"] += w

        conflicts: List[Dict[str, Any]] = []
        for v in viewpoints:
            for sector in v.get("sectors_bullish") or []:
                if pred_dir.get(sector, {}).get("down", 0) > pred_dir.get(sector, {}).get(
                    "up", 0
                ):
                    conflicts.append(
                        {
                            "type": "viewpoint_vs_prediction",
                            "sector": sector,
                            "viewpoint_id": v.get("viewpoint_id"),
                            "detail": "观点看涨但加权预测偏空",
                        }
                    )
            for sector in v.get("sectors_bearish") or []:
                if pred_dir.get(sector, {}).get("up", 0) > pred_dir.get(sector, {}).get(
                    "down", 0
                ):
                    conflicts.append(
                        {
                            "type": "viewpoint_vs_prediction",
                            "sector": sector,
                            "viewpoint_id": v.get("viewpoint_id"),
                            "detail": "观点看跌但加权预测偏多",
                        }
                    )
        return conflicts

    # -------------------------------------------------------------------- hash
    def _hash_pack(self, pack: EvidencePack) -> str:
        """对规范化入选证据做稳定语义哈希（P2 可再增强版本字段）。"""
        payload = {
            "as_of": pack.as_of_date.isoformat(),
            "predictions": [
                {
                    "id": p.get("prediction_id"),
                    "type": p.get("prediction_type"),
                    "sector": p.get("sector"),
                    "target": p.get("target_date"),
                    "confidence": p.get("confidence"),
                    "term": p.get("term"),
                    "weight": p.get("weight"),
                    "blogger_id": p.get("blogger_id"),
                }
                for p in sorted(pack.predictions, key=lambda x: x.get("prediction_id") or 0)
            ],
            "bloggers": [
                {
                    "id": b.get("blogger_id"),
                    "accuracy": b.get("accuracy_rate"),
                    "reliability": b.get("reliability_score"),
                }
                for b in sorted(pack.bloggers, key=lambda x: x.get("blogger_id") or 0)
            ],
            "viewpoints": [
                {
                    "id": v.get("viewpoint_id"),
                    "dir": v.get("market_direction"),
                    "cred": v.get("credibility_score"),
                    "eff": v.get("effective_score"),
                }
                for v in sorted(pack.viewpoints, key=lambda x: x.get("viewpoint_id") or 0)
            ],
            "strategy": pack.meta.get("weight_strategy_version"),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
