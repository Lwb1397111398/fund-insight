"""轻量保留策略（默认 dry-run）— three-buckets-v2。

桶：
1. deleted_predictions — 软删且 deleted_at 满 N 天；**is_correct 非空永不进候选**
2. cleanup_item_logs — created_at 满 M 天
3. unverifiable_predictions — lifecycle=unverifiable 且目标日龄满 K 天；无结论
4. deleted_viewpoints — 软删观点，锚点 **deleted_at** 满 30 天（不看 valid_until）
5. summary_viewpoints — is_summary，锚点 **viewpoint_date** 满窗口（默认 90 天）

全局单次上限 max_total_per_run；真删需 dry_run=False + confirm_token。
在线入口：`POST /api/config/cleanup/three-buckets`（开关 + 确认头 + 预览指纹）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set

from sqlalchemy.orm import Session, load_only

from src.models.database import CleanupItemLog, CleanupLog, Prediction, Viewpoint
from src.services.prediction_lifecycle import (
    UNVERIFIABLE,
    classify,
    current_as_of,
)
from src.utils.blogger_stats import recalculate_blogger_stats

POLICY_NAME = "three-buckets-v2"
CONFIRM_TOKEN = "three-buckets-hard-delete"


class BucketPlanChanged(Exception):
    """预览指纹与当前数据不一致（用户看到的清单已过期）。"""

    def __init__(self, current_fingerprint: str):
        self.current_fingerprint = current_fingerprint
        super().__init__("three-bucket plan is stale")


@dataclass(frozen=True)
class ThreeBucketPolicy:
    deleted_hard_delete_days: int = 30
    cleanup_item_log_days: int = 90
    unverifiable_days: int = 90
    deleted_viewpoint_days: int = 30
    summary_viewpoint_days: int = 90  # 参数化；前端仍读历史汇总列表
    batch_size: int = 200
    max_per_bucket: int = 500
    max_total_per_run: int = 500  # 全局单次上限（跨桶）

    def to_dict(self) -> Dict[str, int]:
        return {
            "deleted_hard_delete_days": self.deleted_hard_delete_days,
            "cleanup_item_log_days": self.cleanup_item_log_days,
            "unverifiable_days": self.unverifiable_days,
            "deleted_viewpoint_days": self.deleted_viewpoint_days,
            "summary_viewpoint_days": self.summary_viewpoint_days,
            "batch_size": self.batch_size,
            "max_per_bucket": self.max_per_bucket,
            "max_total_per_run": self.max_total_per_run,
        }


@dataclass
class BucketPlan:
    as_of: date
    policy: ThreeBucketPolicy
    candidate_ids: Dict[str, List[int]] = field(default_factory=dict)
    protected_counts: Dict[str, int] = field(default_factory=dict)
    samples: Dict[str, List[dict]] = field(default_factory=dict)
    notes: Dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    fingerprint: str = ""

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.candidate_ids.values())

    def to_report_dict(self) -> dict:
        return {
            "policy_name": POLICY_NAME,
            "as_of": self.as_of.isoformat(),
            "policy": self.policy.to_dict(),
            "counts": {k: len(v) for k, v in self.candidate_ids.items()},
            "protected_counts": self.protected_counts,
            "total": self.total,
            "truncated_by_global_cap": self.truncated,
            "candidate_ids": self.candidate_ids,
            "samples": self.samples,
            "notes": self.notes,
            "fingerprint": self.fingerprint,
            "default_mode": "dry-run",
            "confirm_token_required_for_execute": CONFIRM_TOKEN,
            "invariants": [
                "is_correct IS NOT NULL predictions are never hard-deleted",
                f"max_total_per_run={self.policy.max_total_per_run}",
            ],
        }


class ThreeBucketRetentionService:
    BUCKET_DELETED = "deleted_predictions"
    BUCKET_LOGS = "cleanup_item_logs"
    BUCKET_UNVERIFIABLE = "unverifiable_predictions"
    BUCKET_DELETED_VP = "deleted_viewpoints"
    BUCKET_SUMMARY_VP = "summary_viewpoints"
    BUCKETS = (
        BUCKET_DELETED,
        BUCKET_LOGS,
        BUCKET_UNVERIFIABLE,
        BUCKET_DELETED_VP,
        BUCKET_SUMMARY_VP,
    )

    # 前端可读标签（与 web/index.html 的清理分类标签共用一套口径）
    BUCKET_LABELS = {
        BUCKET_DELETED: "回收站预测",
        BUCKET_LOGS: "清理明细日志",
        BUCKET_UNVERIFIABLE: "已错过验证窗口的预测",
        BUCKET_DELETED_VP: "回收站观点",
        BUCKET_SUMMARY_VP: "历史每日汇总",
    }

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
        deleted_ids, protected_verified = self._deleted_prediction_ids()
        plan.protected_counts["verified_ledger_excluded"] = protected_verified
        raw: Dict[str, List[int]] = {
            self.BUCKET_DELETED: deleted_ids,
            self.BUCKET_LOGS: self._cleanup_item_log_ids(),
            self.BUCKET_UNVERIFIABLE: self._unverifiable_prediction_ids(),
            self.BUCKET_DELETED_VP: self._deleted_viewpoint_ids(),
            self.BUCKET_SUMMARY_VP: self._summary_viewpoint_ids(),
        }
        # 全局上限：按桶顺序截断
        remaining = self.policy.max_total_per_run
        capped: Dict[str, List[int]] = {}
        truncated = False
        for name in self.BUCKETS:
            ids = raw.get(name, [])
            if remaining <= 0:
                capped[name] = []
                if ids:
                    truncated = True
                continue
            if len(ids) > remaining:
                capped[name] = ids[:remaining]
                truncated = True
                remaining = 0
            else:
                capped[name] = ids
                remaining -= len(ids)
        plan.candidate_ids = capped
        plan.truncated = truncated
        plan.samples = {
            name: self._samples(name, ids) for name, ids in plan.candidate_ids.items()
        }
        plan.notes = {
            self.BUCKET_DELETED: (
                f"is_deleted 且 deleted_at 满 {self.policy.deleted_hard_delete_days} 天；"
                f"排除 is_correct 非空（护栏排除 {protected_verified} 条）"
            ),
            self.BUCKET_LOGS: (
                f"cleanup_item_logs.created_at 满 {self.policy.cleanup_item_log_days} 天"
            ),
            self.BUCKET_UNVERIFIABLE: (
                f"lifecycle=unverifiable 且目标日龄满 {self.policy.unverifiable_days} 天"
            ),
            self.BUCKET_DELETED_VP: (
                f"观点软删：deleted_at 满 {self.policy.deleted_viewpoint_days} 天"
                "（锚点不用 valid_until）"
            ),
            self.BUCKET_SUMMARY_VP: (
                f"is_summary：viewpoint_date 满 {self.policy.summary_viewpoint_days} 天"
                "（前端列表可读历史汇总，窗口宜偏大）"
            ),
        }
        plan.fingerprint = self._fingerprint(plan)
        return plan

    def _fingerprint(self, plan: BucketPlan) -> str:
        """预览指纹：数据变了就失配，避免用户点确认时删到没看过的行。"""
        payload = {
            "policy_name": POLICY_NAME,
            "as_of": plan.as_of.isoformat(),
            "policy": plan.policy.to_dict(),
            "candidate_ids": plan.candidate_ids,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

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
        expected_fingerprint: Optional[str] = None,
        buckets: Optional[Set[str]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict:
        if expected_fingerprint is not None:
            # 复算当前 plan，只要候选集变了就拒绝（前端预览已过期）
            plan = self.build_plan()
            if plan.fingerprint != expected_fingerprint:
                raise BucketPlanChanged(plan.fingerprint)
        else:
            plan = plan or self.build_plan()

        selected = set(buckets) if buckets else set(self.BUCKETS)
        unknown = selected - set(self.BUCKETS)
        if unknown:
            raise ValueError(f"unknown retention buckets: {sorted(unknown)}")

        report = plan.to_report_dict()
        report["selected_buckets"] = sorted(selected)
        if dry_run:
            report["mode"] = "dry-run"
            report["deleted_counts"] = {k: 0 for k in self.BUCKETS}
            report["message"] = "dry-run：未删除任何行"
            return report

        if confirm_token != CONFIRM_TOKEN:
            raise PermissionError(
                f"真删需要 confirm_token={CONFIRM_TOKEN!r} 且 dry_run=False"
            )

        def ids_for(bucket: str) -> List[int]:
            if bucket not in selected:
                return []
            return list(plan.candidate_ids.get(bucket, []))

        # 执行前再拦一层 verified 台账
        self._assert_no_verified_ledger(
            ids_for(self.BUCKET_DELETED) + ids_for(self.BUCKET_UNVERIFIABLE)
        )

        selected_total = sum(len(ids_for(bucket)) for bucket in self.BUCKETS)
        processed = 0

        def report_progress(bucket: str, done_in_bucket: int) -> None:
            if progress_callback:
                progress_callback(processed + done_in_bucket, selected_total, bucket)

        started_at = datetime.now()
        deleted_counts: Dict[str, int] = {}
        for bucket in self.BUCKETS:
            ids = ids_for(bucket)
            if bucket == self.BUCKET_LOGS:
                deleted = self._hard_delete_cleanup_logs(
                    ids, on_progress=lambda n, b=bucket: report_progress(b, n)
                )
            elif bucket in (self.BUCKET_DELETED_VP, self.BUCKET_SUMMARY_VP):
                deleted = self._hard_delete_viewpoints(
                    ids, on_progress=lambda n, b=bucket: report_progress(b, n)
                )
            else:
                deleted = self._hard_delete_predictions(
                    ids, on_progress=lambda n, b=bucket: report_progress(b, n)
                )
            deleted_counts[bucket] = deleted
            processed += len(ids)
            report_progress(bucket, 0)
        total_deleted = sum(deleted_counts.values())
        finished_at = datetime.now()
        # 摘要归档：一条 CleanupLog，便于周 cron 审计（不写逐条 item，避免再堆 2600 行）
        log = CleanupLog(
            trigger_type="three_buckets",
            start_time=started_at,
            end_time=finished_at,
            duration_ms=int((finished_at - started_at).total_seconds() * 1000),
            status="completed",
            total_items=plan.total,
            success_count=total_deleted,
            failed_count=0,
            rules_snapshot={
                "policy_name": POLICY_NAME,
                "policy": self.policy.to_dict(),
                "as_of": self.today.isoformat(),
                "protected_counts": plan.protected_counts,
                "selected_buckets": sorted(selected),
            },
            details={
                "deleted_counts": deleted_counts,
                "candidate_counts": {k: len(v) for k, v in plan.candidate_ids.items()},
                "truncated_by_global_cap": plan.truncated,
                "invariants": plan.to_report_dict().get("invariants"),
            },
            errors=[],
        )
        self.db.add(log)
        self.db.commit()
        report["mode"] = "execute"
        report["deleted_counts"] = deleted_counts
        report["total_deleted"] = total_deleted
        report["cleanup_log_id"] = log.id
        report["message"] = "hard-delete completed"
        return report

    def _assert_no_verified_ledger(self, ids: Sequence[int]) -> None:
        if not ids:
            return
        bad = (
            self.db.query(Prediction.id)
            .filter(Prediction.id.in_(list(ids)), Prediction.is_correct.isnot(None))
            .all()
        )
        if bad:
            raise RuntimeError(
                f"verified ledger guard blocked delete of ids={[r.id for r in bad[:20]]}"
            )

    # ----- candidates -----

    def _deleted_prediction_ids(self) -> tuple[List[int], int]:
        """返回 (可删 id, 因 verified 护栏排除数)。"""
        cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.deleted_hard_delete_days),
            datetime.min.time(),
        )
        base = self.db.query(Prediction).filter(
            Prediction.is_deleted.is_(True),
            Prediction.deleted_at.isnot(None),
            Prediction.deleted_at < cutoff,
        )
        protected = base.filter(Prediction.is_correct.isnot(None)).count()
        rows = (
            base.filter(Prediction.is_correct.is_(None))
            .order_by(Prediction.deleted_at.asc(), Prediction.id.asc())
            .limit(self.policy.max_per_bucket)
            .all()
        )
        return [r.id for r in rows], int(protected)

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
        min_age = self.policy.unverifiable_days
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
                Prediction.is_correct.is_(None),  # 台账护栏
                Prediction.target_date.isnot(None),
                Prediction.target_date <= target_cutoff,
            )
            .order_by(Prediction.target_date.asc(), Prediction.id.asc())
            .limit(self.policy.max_per_bucket * 3)
            .all()
        )
        ids: List[int] = []
        for p in rows:
            if p.is_correct is not None:
                continue
            if classify(p, as_of=self.today) != UNVERIFIABLE:
                continue
            ids.append(p.id)
            if len(ids) >= self.policy.max_per_bucket:
                break
        return ids

    def _deleted_viewpoint_ids(self) -> List[int]:
        cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.deleted_viewpoint_days),
            datetime.min.time(),
        )
        rows = (
            self.db.query(Viewpoint.id)
            .filter(
                Viewpoint.is_deleted.is_(True),
                Viewpoint.deleted_at.isnot(None),
                Viewpoint.deleted_at < cutoff,
            )
            .order_by(Viewpoint.deleted_at.asc(), Viewpoint.id.asc())
            .limit(self.policy.max_per_bucket)
            .all()
        )
        return [r.id for r in rows]

    def _summary_viewpoint_ids(self) -> List[int]:
        cutoff = self.today - timedelta(days=self.policy.summary_viewpoint_days)
        rows = (
            self.db.query(Viewpoint.id)
            .filter(
                Viewpoint.is_summary.is_(True),
                Viewpoint.is_deleted.is_(False),
                Viewpoint.viewpoint_date < cutoff,
            )
            .order_by(Viewpoint.viewpoint_date.asc(), Viewpoint.id.asc())
            .limit(self.policy.max_per_bucket)
            .all()
        )
        return [r.id for r in rows]

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
        if bucket in (self.BUCKET_DELETED_VP, self.BUCKET_SUMMARY_VP):
            rows = self.db.query(Viewpoint).filter(Viewpoint.id.in_(sample_ids)).all()
            return [
                {
                    "id": r.id,
                    "is_summary": bool(r.is_summary),
                    "is_deleted": bool(r.is_deleted),
                    "viewpoint_date": r.viewpoint_date.isoformat()
                    if r.viewpoint_date
                    else None,
                    "valid_until": r.valid_until.isoformat() if r.valid_until else None,
                    "deleted_at": r.deleted_at.isoformat() if r.deleted_at else None,
                    "source": r.source,
                }
                for r in rows
            ]
        rows = self.db.query(Prediction).filter(Prediction.id.in_(sample_ids)).all()
        return [
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
            for r in rows
        ]

    # ----- execute helpers -----

    def _batches(self, ids: List[int]):
        size = self.policy.batch_size
        for i in range(0, len(ids), size):
            yield ids[i : i + size]

    def _hard_delete_predictions(
        self,
        ids: List[int],
        *,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> int:
        if not ids:
            return 0
        self._assert_no_verified_ledger(ids)
        deleted = 0
        for batch in self._batches(list(ids)):
            rows = self.db.query(Prediction).filter(Prediction.id.in_(batch)).all()
            # 再滤一层
            rows = [r for r in rows if r.is_correct is None]
            blogger_ids = {r.blogger_id for r in rows if r.blogger_id}
            for row in rows:
                if (
                    (row.verify_count or 0) > 0
                    and row.prediction_type != "flat"
                    and row.blogger_id
                ):
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
            # 每批提交：释放写锁，让外部进度查询能读到中间状态
            self.db.commit()
            if on_progress:
                on_progress(deleted)
        return deleted

    def _hard_delete_cleanup_logs(
        self,
        ids: List[int],
        *,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> int:
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
            self.db.commit()
            if on_progress:
                on_progress(deleted)
        return deleted

    def _hard_delete_viewpoints(
        self,
        ids: List[int],
        *,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> int:
        if not ids:
            return 0
        from src.models.database import CrawlerArticleRecord

        deleted = 0
        for batch in self._batches(list(ids)):
            self.db.query(CrawlerArticleRecord).filter(
                CrawlerArticleRecord.viewpoint_id.in_(batch)
            ).update(
                {CrawlerArticleRecord.viewpoint_id: None},
                synchronize_session=False,
            )
            rows = self.db.query(Viewpoint).filter(Viewpoint.id.in_(batch)).all()
            for row in rows:
                self.db.delete(row)
                deleted += 1
            self.db.flush()
            self.db.commit()
            if on_progress:
                on_progress(deleted)
        return deleted
