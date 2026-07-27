"""Dependency-aware retention planning and cleanup execution."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from sqlalchemy.orm import Session

from src.constants.sector_fund_map import SECTOR_ALIASES, SECTOR_FUND_MAP
from src.models.database import (
    AdviceReasoning,
    AnalysisLog,
    BatchAnalysisTask,
    Blogger,
    CleanupItemLog,
    CleanupLog,
    CleanupTask,
    CrawlerArticleRecord,
    FundHistory,
    FundHolding,
    FundInfo,
    FundSyncRetry,
    InvestmentAdvice,
    Post,
    Prediction,
    PredictionChangeLog,
    PredictionGroup,
    SectorFundMapping,
    SyncLog,
    UserFundBinding,
    VerificationTask,
    Viewpoint,
)
from src.utils.blogger_stats import recalculate_blogger_stats


POLICY_VERSION = "retention-v2"


class CleanupPlanChanged(Exception):
    """Raised when data changed after the user reviewed a cleanup preview."""

    def __init__(self, current_fingerprint: str):
        self.current_fingerprint = current_fingerprint
        super().__init__("cleanup preview is stale")


@dataclass(frozen=True)
class CleanupPolicy:
    retention_days: int = 30
    weekly_history_until_days: int = 90
    adopted_crawler_days: int = 180
    cleanup_audit_days: int = 365
    batch_size: int = 200

    def to_dict(self) -> Dict[str, int]:
        return {
            "retention_days": self.retention_days,
            "weekly_history_until_days": self.weekly_history_until_days,
            "adopted_crawler_days": self.adopted_crawler_days,
            "cleanup_audit_days": self.cleanup_audit_days,
            "batch_size": self.batch_size,
        }


@dataclass
class CleanupPlan:
    generated_at: datetime
    policy: CleanupPolicy
    candidate_ids: Dict[str, List[int]]
    protected_counts: Dict[str, int]
    samples: Dict[str, List[dict]] = field(default_factory=dict)
    health_warnings: List[dict] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def total_candidates(self) -> int:
        return sum(len(ids) for ids in self.candidate_ids.values())


class RetentionCleanupService:
    """Use one policy for preview, manual cleanup, and scheduled cleanup."""

    CATEGORIES = (
        "predictions",
        "posts",
        "viewpoints",
        "fund_history",
        "funds",
        "advice",
        "crawler_records",
        "analysis_logs",
        "batch_tasks",
        "sync_logs",
        "fund_sync_retries",
        "cleanup_tasks",
        "cleanup_logs",
    )

    EXECUTION_ORDER = (
        "predictions",
        "viewpoints",
        "posts",
        "advice",
        "crawler_records",
        "analysis_logs",
        "batch_tasks",
        "sync_logs",
        "fund_sync_retries",
        "fund_history",
        "funds",
        "cleanup_tasks",
        "cleanup_logs",
    )

    MODEL_BY_CATEGORY = {
        "viewpoints": Viewpoint,
        "advice": InvestmentAdvice,
        "crawler_records": CrawlerArticleRecord,
        "analysis_logs": AnalysisLog,
        "batch_tasks": BatchAnalysisTask,
        "sync_logs": SyncLog,
        "fund_sync_retries": FundSyncRetry,
        "fund_history": FundHistory,
        "funds": FundInfo,
        "cleanup_tasks": CleanupTask,
        "cleanup_logs": CleanupLog,
    }

    def __init__(
        self,
        db: Session,
        *,
        today: Optional[date] = None,
        policy: Optional[CleanupPolicy] = None,
    ):
        self.db = db
        self.today = today or date.today()
        self.policy = policy or CleanupPolicy()

    def build_plan(self) -> CleanupPlan:
        candidate_ids = {category: [] for category in self.CATEGORIES}
        protected_counts = {
            "pending_predictions": 0,
            "long_term_predictions": 0,
            "mixed_group_predictions": 0,
            "unresolved_prediction_funds": 0,
            "fund_history": 0,
            "protected_funds": 0,
            "active_viewpoints": 0,
            "running_tasks": 0,
        }

        prediction_candidates, pending = self._prediction_candidates(protected_counts)
        viewpoint_candidates = self._viewpoint_candidates(protected_counts)
        candidate_ids["predictions"] = sorted(prediction_candidates)
        candidate_ids["posts"] = sorted(self._post_candidates(prediction_candidates))
        candidate_ids["viewpoints"] = sorted(viewpoint_candidates)
        candidate_ids["advice"] = sorted(self._advice_candidates())
        candidate_ids["crawler_records"] = sorted(
            self._crawler_candidates(viewpoint_candidates)
        )
        candidate_ids.update(self._operation_candidates(protected_counts))

        protected_prediction_funds = self._prediction_fund_codes(
            pending, protected_counts
        )
        fund_candidates = self._fund_candidates(
            prediction_candidates,
            viewpoint_candidates,
            protected_prediction_funds,
            protected_counts,
        )
        candidate_ids["funds"] = sorted(fund_candidates)
        orphan_codes = {
            row.fund_code
            for row in self.db.query(FundInfo).filter(FundInfo.id.in_(fund_candidates)).all()
        } if fund_candidates else set()
        candidate_ids["fund_history"] = sorted(
            self._fund_history_candidates(
                pending,
                protected_prediction_funds,
                protected_counts,
                orphan_codes,
            )
        )

        warnings = self._health_warnings(pending, protected_counts)
        samples = self._samples(candidate_ids)
        generated_at = datetime.combine(self.today, datetime.min.time())
        fingerprint_payload = {
            "version": POLICY_VERSION,
            "today": self.today.isoformat(),
            "policy": self.policy.to_dict(),
            "candidate_ids": candidate_ids,
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return CleanupPlan(
            generated_at=generated_at,
            policy=self.policy,
            candidate_ids=candidate_ids,
            protected_counts=protected_counts,
            samples=samples,
            health_warnings=warnings,
            fingerprint=fingerprint,
        )

    def execute(
        self,
        *,
        expected_fingerprint: str,
        backup_before_cleanup: bool = True,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        trigger_type: str = "manual",
        categories: Optional[Set[str]] = None,
    ) -> dict:
        plan = self.build_plan()
        if plan.fingerprint != expected_fingerprint:
            raise CleanupPlanChanged(plan.fingerprint)

        selected_categories = set(categories or self.EXECUTION_ORDER)
        unknown_categories = selected_categories - set(self.EXECUTION_ORDER)
        if unknown_categories:
            raise ValueError(f"unknown cleanup categories: {sorted(unknown_categories)}")
        selected_total = sum(
            len(plan.candidate_ids[category]) for category in selected_categories
        )

        backup = self._create_backup() if backup_before_cleanup else None
        started_at = datetime.now()
        log = CleanupLog(
            trigger_type=trigger_type,
            start_time=started_at,
            status="running",
            total_items=selected_total,
            rules_snapshot={
                "version": POLICY_VERSION,
                "fingerprint": plan.fingerprint,
                **plan.policy.to_dict(),
            },
            details={"backup": backup},
            errors=[],
        )
        self.db.add(log)
        self.db.commit()
        log_id = log.id

        deleted_counts: Dict[str, int] = {}
        failed_categories: Dict[str, str] = {}
        processed = 0
        for category in self.EXECUTION_ORDER:
            if category not in selected_categories:
                continue
            ids = plan.candidate_ids.get(category, [])
            if not ids:
                continue
            try:
                deleted = 0
                for batch in self._batches(ids):
                    deleted += self._delete_batch(category, batch, log_id, plan)
                    self.db.commit()
                    processed += len(batch)
                    if progress_callback:
                        progress_callback(processed, selected_total, category)
                deleted_counts[category] = deleted
            except Exception as exc:
                self.db.rollback()
                failed_categories[category] = str(exc)

        finished_at = datetime.now()
        log = self.db.get(CleanupLog, log_id)
        log.end_time = finished_at
        log.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        log.success_count = sum(deleted_counts.values())
        log.failed_count = sum(
            len(plan.candidate_ids[category]) for category in failed_categories
        )
        log.status = "partial" if failed_categories else "completed"
        log.details = {"backup": backup, "deleted_counts": deleted_counts}
        log.errors = [
            {"category": category, "error": error}
            for category, error in failed_categories.items()
        ]
        self.db.commit()
        return {
            "success": not failed_categories,
            "status": log.status,
            "deleted_counts": deleted_counts,
            "failed_categories": failed_categories,
            "total_deleted": sum(deleted_counts.values()),
            "log_id": log_id,
            "backup": backup,
        }

    def _prediction_candidates(self, protected_counts: Dict[str, int]):
        cutoff = self.today - timedelta(days=self.policy.retention_days)
        predictions = self.db.query(Prediction).all()
        pending: List[Prediction] = []
        candidates: Set[int] = set()
        for prediction in predictions:
            if prediction.status == "pending" and not prediction.is_deleted:
                pending.append(prediction)
                protected_counts["pending_predictions"] += 1
                if prediction.target_date and prediction.target_date > self.today + timedelta(days=90):
                    protected_counts["long_term_predictions"] += 1
                continue
            if prediction.status not in {"success", "failed"}:
                continue
            if prediction.is_deleted and prediction.restore_before and prediction.restore_before >= self.today:
                continue
            anchor = prediction.verified_at.date() if prediction.verified_at else prediction.target_date
            if anchor and anchor < cutoff:
                candidates.add(prediction.id)

        for group in self.db.query(PredictionGroup).all():
            member_ids = set(group.prediction_ids or [])
            if group.representative_id:
                member_ids.add(group.representative_id)
            group_candidates = member_ids & candidates
            if group_candidates and not member_ids <= candidates:
                candidates -= group_candidates
                protected_counts["mixed_group_predictions"] += len(group_candidates)
        return candidates, pending

    def _post_candidates(self, prediction_candidates: Set[int]) -> Set[int]:
        cutoff = self.today - timedelta(days=self.policy.retention_days)
        predictions_by_post: Dict[int, Set[int]] = {}
        for prediction in self.db.query(Prediction).all():
            predictions_by_post.setdefault(prediction.post_id, set()).add(prediction.id)
        failed_analysis_posts = {
            row.post_id
            for row in self.db.query(AnalysisLog).filter(AnalysisLog.parse_success.is_(False)).all()
            if row.post_id is not None
        }
        result = set()
        for post in self.db.query(Post).all():
            member_ids = predictions_by_post.get(post.id, set())
            if member_ids:
                if member_ids <= prediction_candidates:
                    result.add(post.id)
            elif post.post_date < cutoff and post.analyzed and post.id not in failed_analysis_posts:
                result.add(post.id)
        return result

    def _viewpoint_candidates(self, protected_counts: Dict[str, int]) -> Set[int]:
        cutoff = self.today - timedelta(days=self.policy.retention_days)
        result = set()
        for viewpoint in self.db.query(Viewpoint).all():
            if viewpoint.is_deleted and viewpoint.restore_before and viewpoint.restore_before >= self.today:
                protected_counts["active_viewpoints"] += 1
                continue
            anchor = max(viewpoint.viewpoint_date, viewpoint.valid_until or viewpoint.viewpoint_date)
            if anchor < cutoff:
                result.add(viewpoint.id)
            else:
                protected_counts["active_viewpoints"] += 1
        return result

    def _advice_candidates(self) -> Set[int]:
        cutoff = self.today - timedelta(days=self.policy.retention_days)
        return {
            row.id
            for row in self.db.query(InvestmentAdvice).all()
            if row.advice_date < cutoff
        }

    def _crawler_candidates(self, viewpoint_candidates: Set[int]) -> Set[int]:
        normal_cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.retention_days), datetime.min.time()
        )
        adopted_cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.adopted_crawler_days), datetime.min.time()
        )
        result = set()
        for row in self.db.query(CrawlerArticleRecord).all():
            anchor = row.fetched_at or row.created_at
            if row.viewpoint_id and row.viewpoint_id not in viewpoint_candidates:
                continue
            if row.is_adopted:
                if anchor and anchor < adopted_cutoff:
                    result.add(row.id)
            elif anchor and anchor < normal_cutoff:
                result.add(row.id)
        return result

    def _operation_candidates(self, protected_counts: Dict[str, int]) -> Dict[str, List[int]]:
        cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.retention_days), datetime.min.time()
        )
        audit_cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.cleanup_audit_days), datetime.min.time()
        )
        terminal = {"completed", "failed", "cancelled", "success", "partial"}
        result = {
            "analysis_logs": [
                row.id for row in self.db.query(AnalysisLog).all()
                if row.created_at and row.created_at < cutoff
            ],
            "batch_tasks": [],
            "sync_logs": [],
            "fund_sync_retries": [],
            "cleanup_tasks": [],
            "cleanup_logs": [
                row.id for row in self.db.query(CleanupLog).all()
                if row.created_at and row.created_at < audit_cutoff
            ],
        }
        for row in self.db.query(BatchAnalysisTask).all():
            anchor = row.completed_at or row.updated_at or row.created_at
            if row.status in terminal and anchor and anchor < cutoff:
                result["batch_tasks"].append(row.id)
            elif row.status not in terminal:
                protected_counts["running_tasks"] += 1
        for row in self.db.query(SyncLog).all():
            if row.status in terminal and row.sync_date and row.sync_date < cutoff:
                result["sync_logs"].append(row.id)
            elif row.status not in terminal:
                protected_counts["running_tasks"] += 1
        for row in self.db.query(FundSyncRetry).all():
            anchor = row.updated_at or row.created_at
            if row.status in terminal and anchor and anchor < cutoff:
                result["fund_sync_retries"].append(row.id)
            elif row.status not in terminal:
                protected_counts["running_tasks"] += 1
        for row in self.db.query(CleanupTask).all():
            anchor = row.completed_at or row.created_at
            if row.status in terminal and anchor and anchor < cutoff:
                result["cleanup_tasks"].append(row.id)
            elif row.status not in terminal:
                protected_counts["running_tasks"] += 1
        return {key: sorted(value) for key, value in result.items()}

    def _prediction_fund_codes(
        self,
        predictions: Iterable[Prediction],
        protected_counts: Dict[str, int],
    ) -> Set[str]:
        result = set()
        for prediction in predictions:
            codes = self._resolve_prediction_funds(prediction)
            if not codes:
                protected_counts["unresolved_prediction_funds"] += 1
            result.update(codes)
        return result

    def _resolve_prediction_funds(self, prediction: Prediction) -> Set[str]:
        codes: Set[str] = set()
        funds = self.db.query(FundInfo).all()
        if prediction.fund_code:
            codes.add(prediction.fund_code)
        if prediction.fund_name:
            codes.update(
                fund.fund_code for fund in funds if fund.fund_name == prediction.fund_name
            )
        sector = (prediction.sector or prediction.sector_type or "").strip()
        if not sector:
            return codes

        standard_sector = SECTOR_ALIASES.get(sector, sector)
        for mapping in self.db.query(SectorFundMapping).all():
            if mapping.is_active is not False and self._sector_matches(standard_sector, mapping.sector_name):
                codes.add(mapping.fund_code)
        for binding in self.db.query(UserFundBinding).all():
            if self._sector_matches(standard_sector, binding.sector):
                codes.add(binding.fund_code)
        for mapped_sector, fund in SECTOR_FUND_MAP.items():
            if self._sector_matches(standard_sector, mapped_sector):
                code = fund.get("code")
                if code:
                    codes.add(code)
        for fund in funds:
            if fund.sector_type and self._sector_matches(standard_sector, fund.sector_type):
                codes.add(fund.fund_code)
        return codes

    @staticmethod
    def _sector_matches(left: str, right: str) -> bool:
        left = (left or "").strip()
        right = (right or "").strip()
        return bool(left and right and (left == right or left in right or right in left))

    def _fund_candidates(
        self,
        prediction_candidates: Set[int],
        viewpoint_candidates: Set[int],
        protected_prediction_funds: Set[str],
        protected_counts: Dict[str, int],
    ) -> Set[int]:
        cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.retention_days), datetime.min.time()
        )
        protected_codes = set(protected_prediction_funds)
        for prediction in self.db.query(Prediction).all():
            if prediction.id not in prediction_candidates:
                protected_codes.update(self._resolve_prediction_funds(prediction))
        protected_codes.update(
            row.fund_code for row in self.db.query(Viewpoint).all()
            if row.id not in viewpoint_candidates and row.fund_code
        )
        protected_codes.update(row.fund_code for row in self.db.query(SectorFundMapping).all())
        protected_codes.update(row.fund_code for row in self.db.query(UserFundBinding).all())
        protected_codes.update(row.fund_code for row in self.db.query(FundHolding).all())
        protected_codes.update(
            row.fund_code for row in self.db.query(FundSyncRetry).all()
            if row.status in {"pending", "retrying", "running"}
        )

        result = set()
        for fund in self.db.query(FundInfo).all():
            stale = fund.updated_at is None or fund.updated_at < cutoff
            protected = (
                fund.is_core_fund
                or fund.can_delete is False
                or fund.fund_code in protected_codes
            )
            if stale and not protected:
                result.add(fund.id)
            elif protected:
                protected_counts["protected_funds"] += 1
        return result

    def _fund_history_candidates(
        self,
        pending_predictions: List[Prediction],
        prediction_fund_codes: Set[str],
        protected_counts: Dict[str, int],
        orphan_codes: Set[str],
    ) -> Set[int]:
        protected_ids: Set[int] = set()
        history_by_fund: Dict[str, List[FundHistory]] = {}
        for row in self.db.query(FundHistory).order_by(
            FundHistory.fund_code.asc(), FundHistory.nav_date.desc()
        ).all():
            history_by_fund.setdefault(row.fund_code, []).append(row)

        for prediction in pending_predictions:
            codes = self._resolve_prediction_funds(prediction)
            for code in codes:
                rows = history_by_fund.get(code, [])
                start = prediction.prediction_date
                end = max(self.today, prediction.target_date or self.today)
                protected_ids.update(row.id for row in rows if start <= row.nav_date <= end)
                anchor = next((row for row in rows if row.nav_date <= start), None)
                if anchor:
                    protected_ids.add(anchor.id)

        recent_cutoff = self.today - timedelta(days=self.policy.retention_days)
        weekly_cutoff = self.today - timedelta(days=self.policy.weekly_history_until_days)
        candidates: Set[int] = set()
        for code, rows in history_by_fund.items():
            if code in orphan_codes:
                candidates.update(row.id for row in rows)
                continue
            kept_weeks = set()
            for row in rows:
                if row.id in protected_ids or row.nav_date >= recent_cutoff:
                    continue
                if row.nav_date >= weekly_cutoff:
                    iso = row.nav_date.isocalendar()
                    week_key = (iso.year, iso.week)
                    if week_key not in kept_weeks:
                        kept_weeks.add(week_key)
                        continue
                candidates.add(row.id)
        protected_counts["fund_history"] = len(protected_ids)
        return candidates

    def _health_warnings(
        self,
        pending: List[Prediction],
        protected_counts: Dict[str, int],
    ) -> List[dict]:
        warnings = []
        overdue = sum(1 for row in pending if row.target_date and row.target_date < self.today)
        if overdue:
            warnings.append({"code": "overdue_predictions", "count": overdue})
        unresolved = protected_counts["unresolved_prediction_funds"]
        if unresolved:
            warnings.append({"code": "unresolved_prediction_funds", "count": unresolved})
        stale_cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.retention_days), datetime.min.time()
        )
        stale = self.db.query(FundInfo).filter(FundInfo.updated_at < stale_cutoff).count()
        if stale:
            warnings.append({"code": "stale_funds", "count": stale})
        return warnings

    def _samples(self, candidate_ids: Dict[str, List[int]]) -> Dict[str, List[dict]]:
        samples = {}
        model_map = {**self.MODEL_BY_CATEGORY, "predictions": Prediction, "posts": Post}
        for category, ids in candidate_ids.items():
            if not ids or category not in model_map:
                samples[category] = []
                continue
            rows = self.db.query(model_map[category]).filter(
                model_map[category].id.in_(ids[:20])
            ).all()
            samples[category] = [
                {
                    "id": row.id,
                    "title": getattr(row, "title", None)
                    or getattr(row, "fund_name", None)
                    or getattr(row, "content", None)
                    or getattr(row, "advice_content", None),
                }
                for row in rows
            ]
        return samples

    def _create_backup(self) -> Optional[dict]:
        bind = self.db.get_bind()
        if bind.dialect.name != "sqlite":
            return None
        database = bind.url.database
        if not database or database == ":memory:":
            return None
        from scripts.backup_database import create_sqlite_backup

        result = create_sqlite_backup(Path(database), Path("backup"))
        return {key: str(value) for key, value in result.items()}

    def _batches(self, ids: List[int]):
        for start in range(0, len(ids), self.policy.batch_size):
            yield ids[start:start + self.policy.batch_size]

    def _delete_batch(
        self,
        category: str,
        ids: List[int],
        log_id: int,
        plan: CleanupPlan,
    ) -> int:
        if category == "predictions":
            return self._delete_predictions(ids, log_id, set(plan.candidate_ids["predictions"]))
        if category == "posts":
            return self._delete_posts(ids, log_id)
        if category == "viewpoints":
            return self._delete_viewpoints(ids, log_id)
        rows = self.db.query(self.MODEL_BY_CATEGORY[category]).filter(
            self.MODEL_BY_CATEGORY[category].id.in_(ids)
        ).all()
        if category == "advice":
            self.db.query(AdviceReasoning).filter(AdviceReasoning.advice_id.in_(ids)).delete(
                synchronize_session=False
            )
        elif category == "batch_tasks":
            self.db.query(AnalysisLog).filter(AnalysisLog.task_id.in_(ids)).delete(
                synchronize_session=False
            )
        elif category == "funds":
            codes = [row.fund_code for row in rows]
            self.db.query(FundSyncRetry).filter(
                FundSyncRetry.fund_code.in_(codes),
                FundSyncRetry.status.in_({"success", "failed", "cancelled", "completed"}),
            ).delete(synchronize_session=False)
            self.db.query(FundHistory).filter(FundHistory.fund_code.in_(codes)).delete(
                synchronize_session=False
            )
        elif category == "cleanup_logs":
            self.db.query(CleanupItemLog).filter(CleanupItemLog.log_id.in_(ids)).delete(
                synchronize_session=False
            )
        for row in rows:
            self._audit_item(log_id, category.rstrip("s"), row)
            self.db.delete(row)
        return len(rows)

    def _delete_predictions(self, ids: List[int], log_id: int, all_candidates: Set[int]) -> int:
        groups = self.db.query(PredictionGroup).all()
        for group in groups:
            members = set(group.prediction_ids or [])
            if group.representative_id:
                members.add(group.representative_id)
            if members and members <= all_candidates:
                self.db.delete(group)
        self.db.flush()
        self.db.query(VerificationTask).filter(VerificationTask.prediction_id.in_(ids)).delete(
            synchronize_session=False
        )
        self.db.query(PredictionChangeLog).filter(PredictionChangeLog.prediction_id.in_(ids)).delete(
            synchronize_session=False
        )
        rows = self.db.query(Prediction).filter(Prediction.id.in_(ids)).all()
        blogger_ids = set()
        for row in rows:
            blogger = self.db.get(Blogger, row.blogger_id)
            if (row.verify_count or 0) > 0 and row.prediction_type != "flat":
                blogger.archived_verified_count = (blogger.archived_verified_count or 0) + 1
                blogger.archived_correct_count = (blogger.archived_correct_count or 0) + int(bool(row.is_correct))
                blogger.archived_verify_score = (blogger.archived_verify_score or 0) + float(row.verify_score or 0)
            blogger_ids.add(row.blogger_id)
            self._audit_item(log_id, "prediction", row)
            self.db.delete(row)
        self.db.flush()
        for blogger_id in blogger_ids:
            recalculate_blogger_stats(self.db, blogger_id, commit=False)
        return len(rows)

    def _delete_posts(self, ids: List[int], log_id: int) -> int:
        self.db.query(AnalysisLog).filter(AnalysisLog.post_id.in_(ids)).delete(
            synchronize_session=False
        )
        self.db.query(Viewpoint).filter(Viewpoint.post_id.in_(ids)).update(
            {Viewpoint.post_id: None}, synchronize_session=False
        )
        rows = self.db.query(Post).filter(Post.id.in_(ids)).all()
        for row in rows:
            self._audit_item(log_id, "post", row)
            self.db.delete(row)
        return len(rows)

    def _delete_viewpoints(self, ids: List[int], log_id: int) -> int:
        self.db.query(CrawlerArticleRecord).filter(
            CrawlerArticleRecord.viewpoint_id.in_(ids)
        ).update({CrawlerArticleRecord.viewpoint_id: None}, synchronize_session=False)
        rows = self.db.query(Viewpoint).filter(Viewpoint.id.in_(ids)).all()
        for row in rows:
            self._audit_item(log_id, "viewpoint", row)
            self.db.delete(row)
        return len(rows)

    def _audit_item(self, log_id: int, data_type: str, row) -> None:
        original_date = (
            getattr(row, "prediction_date", None)
            or getattr(row, "post_date", None)
            or getattr(row, "viewpoint_date", None)
            or getattr(row, "nav_date", None)
            or getattr(row, "advice_date", None)
        )
        self.db.add(CleanupItemLog(
            log_id=log_id,
            data_type=data_type,
            data_id=row.id,
            data_title=(getattr(row, "title", None) or getattr(row, "fund_name", None)),
            action="delete",
            reason=f"{POLICY_VERSION}: retention expired",
            original_date=original_date,
            deleted_at=datetime.now(),
            can_restore=False,
        ))
