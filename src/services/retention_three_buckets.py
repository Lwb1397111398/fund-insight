"""三桶轻量保留策略（默认 dry-run）。

桶：
1. deleted_predictions — is_deleted 且 deleted_at 早于 N 天 → 硬删
2. cleanup_item_logs — created_at 早于 M 天 → 硬删
3. unverifiable_predictions — lifecycle=unverifiable 且目标日距今 ≥ K 天 → 硬删

不替换 RetentionCleanupService；独立、可审、默认可逆（只出清单）。
真删必须 dry_run=False 且 confirm_token 匹配。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session, load_only

from src.models.database import CleanupItemLog, Prediction
from src.services.prediction_lifecycle import (
    UNVERIFIABLE,
    classify,
    current_as_of,
)
from src.utils.blogger_stats import recalculate_blogger_stats

POLICY_NAME = "three-buckets-v1"
CONFIRM_TOKEN = "three-buckets-hard-delete"


@dataclass(frozen=True)
class ThreeBucketPolicy:
    deleted_hard_delete_days: int = 30
    cleanup_item_log_days: int = 90
    unverifiable_days: int = 90
    batch_size: int = 200
    # 单次执行每桶最多处理条数（防止一次扫光）
    max_per_bucket: int = 500

    def to_dict(self) -> Dict[str, int]:
        return {
            "deleted_hard_delete_days": self.deleted_hard_delete_days,
            "cleanup_item_log_days": self.cleanup_item_log_days,
            "unverifiable_days": self.unverifiable_days,
            "batch_size": self.batch_size,
            "max_per_bucket": self.max_per_bucket,
        }


@dataclass
class BucketPlan:
    as_of: date
    policy: ThreeBucketPolicy
    candidate_ids: Dict[str, List[int]] = field(default_factory=dict)
    samples: Dict[str, List[dict]] = field(default_factory=dict)
    notes: Dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.candidate_ids.values())

    def to_report_dict(self) -> dict:
        return {
            "policy_name": POLICY_NAME,
            "as_of": self.as_of.isoformat(),
            "policy": self.policy.to_dict(),
            "counts": {k: len(v) for k, v in self.candidate_ids.items()},
            "total": self.total,
            "candidate_ids": self.candidate_ids,
            "samples": self.samples,
            "notes": self.notes,
            "default_mode": "dry-run",
            "confirm_token_required_for_execute": CONFIRM_TOKEN,
        }


class ThreeBucketRetentionService:
    BUCKET_DELETED = "deleted_predictions"
    BUCKET_LOGS = "cleanup_item_logs"
    BUCKET_UNVERIFIABLE = "unverifiable_predictions"
    BUCKETS = (BUCKET_DELETED, BUCKET_LOGS, BUCKET_UNVERIFIABLE)

    def __init__(
        self,
        db: Session,
        *,
        today: Optional[date] = None,
        policy: Optional[ThreeBucketPolicy] = None,
    ):
        self.db = db
        self.today = today or current_as_of()
        self.policy = policy or ThreeBucketPolicy()

    def build_plan(self) -> BucketPlan:
        plan = BucketPlan(as_of=self.today, policy=self.policy)
        plan.candidate_ids[self.BUCKET_DELETED] = self._deleted_prediction_ids()
        plan.candidate_ids[self.BUCKET_LOGS] = self._cleanup_item_log_ids()
        plan.candidate_ids[self.BUCKET_UNVERIFIABLE] = self._unverifiable_prediction_ids()
        plan.samples = {
            name: self._samples(name, ids)
            for name, ids in plan.candidate_ids.items()
        }
        plan.notes = {
            self.BUCKET_DELETED: (
                f"is_deleted=true 且 deleted_at < {self.today - timedelta(days=self.policy.deleted_hard_delete_days)}"
            ),
            self.BUCKET_LOGS: (
                f"cleanup_item_logs.created_at < {self.today - timedelta(days=self.policy.cleanup_item_log_days)}"
            ),
            self.BUCKET_UNVERIFIABLE: (
                f"lifecycle=unverifiable 且 (as_of-target_date) >= {self.policy.unverifiable_days}；"
                "当前未满窗则计数为 0（策略先行）"
            ),
        }
        return plan

    def write_dry_run_report(self, path: Path, plan: Optional[BucketPlan] = None) -> Path:
        plan = plan or self.build_plan()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = plan.to_report_dict()
        payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
        payload["mode"] = "dry-run"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return path

    def execute(
        self,
        *,
        dry_run: bool = True,
        confirm_token: Optional[str] = None,
        plan: Optional[BucketPlan] = None,
    ) -> dict:
        """默认 dry_run=True：只返回将删清单，不写库。

        真删条件：dry_run=False 且 confirm_token==CONFIRM_TOKEN。
        """
        plan = plan or self.build_plan()
        report = plan.to_report_dict()
        if dry_run:
            report["mode"] = "dry-run"
            report["deleted_counts"] = {k: 0 for k in self.BUCKETS}
            report["message"] = "dry-run：未删除任何行"
            return report

        if confirm_token != CONFIRM_TOKEN:
            raise PermissionError(
                f"真删需要 confirm_token={CONFIRM_TOKEN!r} 且 dry_run=False"
            )

        deleted_counts = {
            self.BUCKET_DELETED: self._hard_delete_predictions(
                plan.candidate_ids.get(self.BUCKET_DELETED, [])
            ),
            self.BUCKET_LOGS: self._hard_delete_cleanup_logs(
                plan.candidate_ids.get(self.BUCKET_LOGS, [])
            ),
            self.BUCKET_UNVERIFIABLE: self._hard_delete_predictions(
                plan.candidate_ids.get(self.BUCKET_UNVERIFIABLE, [])
            ),
        }
        self.db.commit()
        report["mode"] = "execute"
        report["deleted_counts"] = deleted_counts
        report["total_deleted"] = sum(deleted_counts.values())
        report["message"] = "hard-delete completed"
        return report

    # ----- candidates -----

    def _deleted_prediction_ids(self) -> List[int]:
        cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.deleted_hard_delete_days),
            datetime.min.time(),
        )
        rows = (
            self.db.query(Prediction.id)
            .filter(
                Prediction.is_deleted.is_(True),
                Prediction.deleted_at.isnot(None),
                Prediction.deleted_at < cutoff,
            )
            .order_by(Prediction.deleted_at.asc(), Prediction.id.asc())
            .limit(self.policy.max_per_bucket)
            .all()
        )
        return [r.id for r in rows]

    def _cleanup_item_log_ids(self) -> List[int]:
        cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.cleanup_item_log_days),
            datetime.min.time(),
        )
        rows = (
            self.db.query(CleanupItemLog.id)
            .filter(CleanupItemLog.created_at < cutoff)
            .order_by(CleanupItemLog.created_at.asc(), CleanupItemLog.id.asc())
            .limit(self.policy.max_per_bucket)
            .all()
        )
        return [r.id for r in rows]

    def _unverifiable_prediction_ids(self) -> List[int]:
        """只收 lifecycle=unverifiable 且目标日距 as_of 已满窗。"""
        min_age = self.policy.unverifiable_days
        # 粗筛：未删、无结论、target 足够早，再 classify 精筛
        target_cutoff = self.today - timedelta(days=min_age)
        rows = (
            self.db.query(Prediction)
            .options(
                load_only(
                    Prediction.id,
                    Prediction.is_deleted,
                    Prediction.is_correct,
                    Prediction.target_date,
                    Prediction.status,
                    Prediction.fund_code,
                    Prediction.prediction_type,
                )
            )
            .filter(
                Prediction.is_deleted.is_(False),
                Prediction.is_correct.is_(None),
                Prediction.target_date.isnot(None),
                Prediction.target_date <= target_cutoff,
            )
            .order_by(Prediction.target_date.asc(), Prediction.id.asc())
            .limit(self.policy.max_per_bucket * 3)  # classify 前多取一点
            .all()
        )
        ids: List[int] = []
        for p in rows:
            if classify(p, as_of=self.today) != UNVERIFIABLE:
                continue
            ids.append(p.id)
            if len(ids) >= self.policy.max_per_bucket:
                break
        return ids

    def _samples(self, bucket: str, ids: Sequence[int], limit: int = 15) -> List[dict]:
        if not ids:
            return []
        sample_ids = list(ids)[:limit]
        if bucket == self.BUCKET_LOGS:
            rows = (
                self.db.query(CleanupItemLog)
                .filter(CleanupItemLog.id.in_(sample_ids))
                .all()
            )
            return [
                {
                    "id": r.id,
                    "data_type": r.data_type,
                    "data_id": r.data_id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "action": r.action,
                }
                for r in rows
            ]
        rows = self.db.query(Prediction).filter(Prediction.id.in_(sample_ids)).all()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r.id,
                    "status": r.status,
                    "is_deleted": r.is_deleted,
                    "is_correct": r.is_correct,
                    "target_date": r.target_date.isoformat() if r.target_date else None,
                    "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
                    "lifecycle": classify(r, as_of=self.today),
                    "fund_code": r.fund_code,
                }
            )
        return out

    # ----- execute helpers -----

    def _batches(self, ids: List[int]):
        size = self.policy.batch_size
        for i in range(0, len(ids), size):
            yield ids[i : i + size]

    def _hard_delete_predictions(self, ids: List[int]) -> int:
        if not ids:
            return 0
        deleted = 0
        for batch in self._batches(list(ids)):
            rows = self.db.query(Prediction).filter(Prediction.id.in_(batch)).all()
            blogger_ids = {r.blogger_id for r in rows if r.blogger_id}
            for row in rows:
                # 软删行若曾验证：归档分数，避免准确率分母空洞
                if (row.verify_count or 0) > 0 and row.prediction_type != "flat" and row.blogger_id:
                    from src.models.database import Blogger

                    blogger = self.db.get(Blogger, row.blogger_id)
                    if blogger is not None:
                        blogger.archived_verified_count = (
                            blogger.archived_verified_count or 0
                        ) + 1
                        blogger.archived_correct_count = (
                            blogger.archived_correct_count or 0
                        ) + int(bool(row.is_correct))
                        blogger.archived_verify_score = (
                            blogger.archived_verify_score or 0
                        ) + float(row.verify_score or 0)
                self.db.delete(row)
                deleted += 1
            self.db.flush()
            for bid in blogger_ids:
                recalculate_blogger_stats(self.db, bid, commit=False)
        return deleted

    def _hard_delete_cleanup_logs(self, ids: List[int]) -> int:
        if not ids:
            return 0
        deleted = 0
        for batch in self._batches(list(ids)):
            rows = (
                self.db.query(CleanupItemLog)
                .filter(CleanupItemLog.id.in_(batch))
                .all()
            )
            for row in rows:
                self.db.delete(row)
                deleted += 1
            self.db.flush()
        return deleted
