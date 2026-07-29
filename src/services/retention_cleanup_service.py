"""Dependency-aware retention planning and cleanup execution."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from sqlalchemy import DateTime, case, func
from sqlalchemy.orm import Session, load_only

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

# 硬删统一走 three-buckets 脚本；本服务 execute 永久只读下线（preview/build_plan 仍可用）
HARD_DELETE_DISABLED = True
HARD_DELETE_DISABLED_REASON = (
    "retention-v2 硬删路径已下线；请使用 scripts/run_three_bucket_retention.py "
    "（默认 dry-run，含 verified 台账护栏）。preview/build_plan 仍只读可用。"
)


class CleanupPlanChanged(Exception):
    """Raised when data changed after the user reviewed a cleanup preview."""

    def __init__(self, current_fingerprint: str):
        self.current_fingerprint = current_fingerprint
        super().__init__("cleanup preview is stale")


class HardDeleteDisabled(RuntimeError):
    """旧执行器硬删已关闭。"""


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
            "long_term_fund_windows": 0,
            "long_term_fund_history": 0,
            "protected_funds": 0,
            "active_viewpoints": 0,
            "running_tasks": 0,
        }

        # 预加载高频表为实例缓存，避免重复全表扫描（Supabase 每次往返 ~2s）
        # 仅存活于本次 build_plan 调用，不影响其他方法
        # 只选必要列，避免传输 TEXT/JSON 大列（predictions 全列 ~2MB，选 12 列后 ~200KB）
        self._cache_predictions = self.db.query(Prediction).options(
            load_only(
                Prediction.id, Prediction.status, Prediction.is_deleted,
                Prediction.restore_before, Prediction.verified_at,
                Prediction.target_date, Prediction.post_id, Prediction.fund_code,
                Prediction.fund_name, Prediction.sector, Prediction.sector_type,
                Prediction.prediction_date,
            )
        ).all()
        self._cache_funds = self.db.query(FundInfo).all()
        self._cache_groups = self.db.query(PredictionGroup).all()
        self._cache_mappings = self.db.query(SectorFundMapping).all()
        self._cache_bindings = self.db.query(UserFundBinding).all()

        try:
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
                for row in self._cache_funds
                if row.id in fund_candidates
            }
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
        finally:
            # 释放缓存，避免内存泄漏
            del self._cache_predictions
            del self._cache_funds
            del self._cache_groups
            del self._cache_mappings
            del self._cache_bindings

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
        if HARD_DELETE_DISABLED:
            raise HardDeleteDisabled(HARD_DELETE_DISABLED_REASON)

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

        # 清理前快照博主准确率，用于防崩溃校验
        accuracy_before = self._snapshot_blogger_accuracy(
            plan.candidate_ids.get("predictions", [])
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

        # 清理后准确率对比
        accuracy_after = self._snapshot_blogger_accuracy_by_ids(
            list(accuracy_before.keys())
        )
        blogger_accuracy_guard = self._build_accuracy_guard(
            accuracy_before, accuracy_after
        )

        finished_at = datetime.now()
        log = self.db.get(CleanupLog, log_id)
        log.end_time = finished_at
        log.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        log.success_count = sum(deleted_counts.values())
        log.failed_count = sum(
            len(plan.candidate_ids[category]) for category in failed_categories
        )
        log.status = "partial" if failed_categories else "completed"
        log.details = {
            "backup": backup,
            "deleted_counts": deleted_counts,
            "blogger_accuracy_guard": blogger_accuracy_guard,
        }
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
            "blogger_accuracy_guard": blogger_accuracy_guard,
        }

    def _prediction_candidates(self, protected_counts: Dict[str, int]):
        """返回 (candidate_ids, pending_predictions)。

        优化：从缓存读取（已用 load_only 只加载必要列），避免重复查询。
        """
        cutoff = self.today - timedelta(days=self.policy.retention_days)
        long_term_threshold = self.today + timedelta(days=90)
        predictions = self._cache_predictions
        groups = self._cache_groups
        pending: List[Prediction] = []
        candidates: Set[int] = set()
        for prediction in predictions:
            if prediction.status == "pending" and not prediction.is_deleted:
                pending.append(prediction)
                protected_counts["pending_predictions"] += 1
                if prediction.target_date and prediction.target_date > long_term_threshold:
                    protected_counts["long_term_predictions"] += 1
                continue
            if prediction.status not in {"success", "failed"}:
                continue
            if prediction.is_deleted and prediction.restore_before and prediction.restore_before >= self.today:
                continue
            anchor = prediction.verified_at.date() if prediction.verified_at else prediction.target_date
            if anchor and anchor < cutoff:
                candidates.add(prediction.id)

        for group in groups:
            member_ids = set(group.prediction_ids or [])
            if group.representative_id:
                member_ids.add(group.representative_id)
            group_candidates = member_ids & candidates
            if group_candidates and not member_ids <= candidates:
                candidates -= group_candidates
                protected_counts["mixed_group_predictions"] += len(group_candidates)
        return candidates, pending

    def _post_candidates(self, prediction_candidates: Set[int]) -> Set[int]:
        """空帖子候选：所有预测都在 candidates 中，或无任何预测且已分析过期。

        优化：从缓存读取 predictions，Post 只查必要字段。
        """
        cutoff = self.today - timedelta(days=self.policy.retention_days)
        predictions_by_post: Dict[int, Set[int]] = {}
        for prediction in self._cache_predictions:
            predictions_by_post.setdefault(prediction.post_id, set()).add(prediction.id)
        # 只查失败日志的 post_id，不加载整行
        failed_analysis_posts = {
            row.post_id
            for row in self.db.query(AnalysisLog.post_id).filter(
                AnalysisLog.parse_success.is_(False),
                AnalysisLog.post_id.isnot(None),
            ).all()
        }
        result = set()
        # Post 只查 id, post_date, analyzed 三列
        for post_id, post_date, analyzed in self.db.query(
            Post.id, Post.post_date, Post.analyzed,
        ).all():
            member_ids = predictions_by_post.get(post_id, set())
            if member_ids:
                if member_ids <= prediction_candidates:
                    result.add(post_id)
            elif post_date < cutoff and analyzed and post_id not in failed_analysis_posts:
                result.add(post_id)
        return result

    def _viewpoint_candidates(self, protected_counts: Dict[str, int]) -> Set[int]:
        cutoff = self.today - timedelta(days=self.policy.retention_days)
        result = set()
        # 只查判定需要的字段，减少传输
        rows = self.db.query(
            Viewpoint.id, Viewpoint.is_deleted, Viewpoint.restore_before,
            Viewpoint.viewpoint_date, Viewpoint.valid_until,
        ).all()
        for viewpoint_id, is_deleted, restore_before, viewpoint_date, valid_until in rows:
            if is_deleted and restore_before and restore_before >= self.today:
                protected_counts["active_viewpoints"] += 1
                continue
            anchor = max(viewpoint_date, valid_until or viewpoint_date)
            if anchor < cutoff:
                result.add(viewpoint_id)
            else:
                protected_counts["active_viewpoints"] += 1
        return result

    def _advice_candidates(self) -> Set[int]:
        cutoff = self.today - timedelta(days=self.policy.retention_days)
        return {
            row.id
            for row in self.db.query(InvestmentAdvice.id).filter(
                InvestmentAdvice.advice_date < cutoff
            ).all()
        }

    def _crawler_candidates(self, viewpoint_candidates: Set[int]) -> Set[int]:
        normal_cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.retention_days), datetime.min.time()
        )
        adopted_cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.adopted_crawler_days), datetime.min.time()
        )
        result = set()
        rows = self.db.query(
            CrawlerArticleRecord.id, CrawlerArticleRecord.viewpoint_id,
            CrawlerArticleRecord.is_adopted, CrawlerArticleRecord.fetched_at,
            CrawlerArticleRecord.created_at,
        ).all()
        for row_id, viewpoint_id, is_adopted, fetched_at, created_at in rows:
            if viewpoint_id and viewpoint_id not in viewpoint_candidates:
                continue
            anchor = fetched_at or created_at
            if is_adopted:
                if anchor and anchor < adopted_cutoff:
                    result.add(row_id)
            elif anchor and anchor < normal_cutoff:
                result.add(row_id)
        return result

    def _operation_candidates(self, protected_counts: Dict[str, int]) -> Dict[str, List[int]]:
        cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.retention_days), datetime.min.time()
        )
        audit_cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.cleanup_audit_days), datetime.min.time()
        )
        terminal = {"completed", "failed", "cancelled", "success", "partial"}
        result: Dict[str, List[int]] = {
            "analysis_logs": [],
            "batch_tasks": [],
            "sync_logs": [],
            "fund_sync_retries": [],
            "cleanup_tasks": [],
            "cleanup_logs": [],
        }
        # 每张表只查判定需要的字段
        for row in self.db.query(AnalysisLog.id, AnalysisLog.created_at).filter(
            AnalysisLog.created_at < cutoff
        ).all():
            result["analysis_logs"].append(row.id)
        for row in self.db.query(CleanupLog.id, CleanupLog.created_at).filter(
            CleanupLog.created_at < audit_cutoff
        ).all():
            result["cleanup_logs"].append(row.id)
        for row in self.db.query(
            BatchAnalysisTask.id, BatchAnalysisTask.status,
            BatchAnalysisTask.completed_at, BatchAnalysisTask.updated_at,
            BatchAnalysisTask.created_at,
        ).all():
            anchor = row.completed_at or row.updated_at or row.created_at
            if row.status in terminal and anchor and anchor < cutoff:
                result["batch_tasks"].append(row.id)
            elif row.status not in terminal:
                protected_counts["running_tasks"] += 1
        for row in self.db.query(SyncLog.id, SyncLog.status, SyncLog.sync_date).all():
            if row.status in terminal and row.sync_date and row.sync_date < cutoff:
                result["sync_logs"].append(row.id)
            elif row.status not in terminal:
                protected_counts["running_tasks"] += 1
        for row in self.db.query(
            FundSyncRetry.id, FundSyncRetry.status,
            FundSyncRetry.updated_at, FundSyncRetry.created_at,
        ).all():
            anchor = row.updated_at or row.created_at
            if row.status in terminal and anchor and anchor < cutoff:
                result["fund_sync_retries"].append(row.id)
            elif row.status not in terminal:
                protected_counts["running_tasks"] += 1
        for row in self.db.query(
            CleanupTask.id, CleanupTask.status,
            CleanupTask.completed_at, CleanupTask.created_at,
        ).all():
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
        funds = self._cache_funds
        mappings = self._cache_mappings
        bindings = self._cache_bindings
        result = set()
        unresolved = 0
        for prediction in predictions:
            codes = self._resolve_prediction_funds(prediction)
            if not codes:
                unresolved += 1
            result.update(codes)
        protected_counts["unresolved_prediction_funds"] += unresolved
        return result

    def _resolve_prediction_funds(self, prediction: Prediction) -> Set[str]:
        funds = self._cache_funds
        mappings = self._cache_mappings
        bindings = self._cache_bindings
        codes: Set[str] = set()
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
        for mapping in mappings:
            if mapping.is_active is not False and self._sector_matches(standard_sector, mapping.sector_name):
                codes.add(mapping.fund_code)
        for binding in bindings:
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
        mappings = self._cache_mappings
        bindings = self._cache_bindings
        protected_codes = set(protected_prediction_funds)
        for prediction in self._cache_predictions:
            if prediction.id not in prediction_candidates:
                protected_codes.update(self._resolve_prediction_funds(prediction))
        # 查未被清理的 viewpoint 关联的 fund_code
        protected_codes.update(
            row.fund_code
            for row in self.db.query(Viewpoint.fund_code, Viewpoint.id).all()
            if row.fund_code and row.id not in viewpoint_candidates
        )
        protected_codes.update(row.fund_code for row in mappings)
        protected_codes.update(row.fund_code for row in bindings)
        protected_codes.update(
            row.fund_code for row in self.db.query(FundHolding.fund_code).all()
        )
        protected_codes.update(
            row.fund_code for row in self.db.query(FundSyncRetry.fund_code).filter(
                FundSyncRetry.status.in_({"pending", "retrying", "running"})
            ).all()
        )

        result = set()
        for fund in self._cache_funds:
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
        long_term_protected_ids: Set[int] = set()
        long_term_codes: Set[str] = set()
        # 只查 3 列，按 fund_code 分组（避免加载 ORM 全行）
        history_rows = self.db.query(
            FundHistory.id, FundHistory.fund_code, FundHistory.nav_date,
        ).order_by(
            FundHistory.fund_code.asc(), FundHistory.nav_date.desc()
        ).all()
        history_by_fund: Dict[str, List[tuple]] = {}
        for hist_id, fund_code, nav_date in history_rows:
            history_by_fund.setdefault(fund_code, []).append((hist_id, nav_date))

        for prediction in pending_predictions:
            codes = self._resolve_prediction_funds(prediction)
            start = prediction.prediction_date
            end = max(self.today, prediction.target_date or self.today)
            span_days = (end - start).days if start else 0
            is_long_term = span_days >= 90
            for code in codes:
                rows = history_by_fund.get(code, [])
                window_ids = {
                    hist_id for hist_id, nav_date in rows if start <= nav_date <= end
                }
                protected_ids.update(window_ids)
                if is_long_term:
                    long_term_codes.add(code)
                    long_term_protected_ids.update(window_ids)
                anchor = next(
                    (hist_id for hist_id, nav_date in rows if nav_date <= start), None
                )
                if anchor is not None:
                    protected_ids.add(anchor)
                    if is_long_term:
                        long_term_protected_ids.add(anchor)

        recent_cutoff = self.today - timedelta(days=self.policy.retention_days)
        weekly_cutoff = self.today - timedelta(days=self.policy.weekly_history_until_days)
        candidates: Set[int] = set()
        for code, rows in history_by_fund.items():
            if code in orphan_codes:
                candidates.update(hist_id for hist_id, _ in rows)
                continue
            kept_weeks = set()
            for hist_id, nav_date in rows:
                if hist_id in protected_ids or nav_date >= recent_cutoff:
                    continue
                # 长期预测关联基金：预测窗外的 30-90 天仍可抽稀，窗内已全保护
                if nav_date >= weekly_cutoff:
                    iso = nav_date.isocalendar()
                    week_key = (iso.year, iso.week)
                    if week_key not in kept_weeks:
                        kept_weeks.add(week_key)
                        continue
                candidates.add(hist_id)
        protected_counts["fund_history"] = len(protected_ids)
        protected_counts["long_term_fund_windows"] = len(long_term_codes)
        protected_counts["long_term_fund_history"] = len(long_term_protected_ids)
        return candidates

    def _snapshot_blogger_accuracy(self, prediction_ids: List[int]) -> Dict[int, dict]:
        """清理前快照：将要删除的预测所涉博主的准确率。"""
        if not prediction_ids:
            return {}
        blogger_ids = {
            row.blogger_id
            for row in self.db.query(Prediction.blogger_id).filter(
                Prediction.id.in_(prediction_ids)
            ).all()
            if row.blogger_id
        }
        return self._snapshot_blogger_accuracy_by_ids(list(blogger_ids))

    def _snapshot_blogger_accuracy_by_ids(self, blogger_ids: List[int]) -> Dict[int, dict]:
        if not blogger_ids:
            return {}
        result: Dict[int, dict] = {}
        for blogger in self.db.query(Blogger).filter(Blogger.id.in_(blogger_ids)).all():
            result[blogger.id] = {
                "blogger_id": blogger.id,
                "name": blogger.name,
                "accuracy_rate": float(blogger.accuracy_rate or 0),
                "total_predictions": int(blogger.total_predictions or 0),
                "archived_verified_count": int(blogger.archived_verified_count or 0),
                "archived_verify_score": float(blogger.archived_verify_score or 0),
            }
        return result

    @staticmethod
    def _build_accuracy_guard(
        before: Dict[int, dict], after: Dict[int, dict]
    ) -> dict:
        rows = []
        max_abs_delta = 0.0
        for blogger_id, snap in before.items():
            after_snap = after.get(blogger_id) or {}
            before_acc = float(snap.get("accuracy_rate") or 0)
            after_acc = float(after_snap.get("accuracy_rate") or 0)
            delta = round(after_acc - before_acc, 4)
            max_abs_delta = max(max_abs_delta, abs(delta))
            rows.append({
                "blogger_id": blogger_id,
                "name": snap.get("name"),
                "before": before_acc,
                "after": after_acc,
                "delta": delta,
                "total_predictions_before": snap.get("total_predictions"),
                "total_predictions_after": after_snap.get("total_predictions"),
            })
        return {
            "bloggers_touched": len(rows),
            "accuracy_before_after": rows,
            "max_abs_delta": round(max_abs_delta, 4),
            "stable": max_abs_delta < 0.01,
        }

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
        blogger_ids = {row.blogger_id for row in rows if row.blogger_id}
        bloggers = {
            b.id: b
            for b in self.db.query(Blogger).filter(Blogger.id.in_(blogger_ids)).all()
        } if blogger_ids else {}
        for row in rows:
            blogger = bloggers.get(row.blogger_id)
            if blogger is not None and (row.verify_count or 0) > 0 and row.prediction_type != "flat":
                blogger.archived_verified_count = (blogger.archived_verified_count or 0) + 1
                blogger.archived_correct_count = (blogger.archived_correct_count or 0) + int(bool(row.is_correct))
                blogger.archived_verify_score = (blogger.archived_verify_score or 0) + float(row.verify_score or 0)
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
