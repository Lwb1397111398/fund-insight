"""轻量保留策略（默认 dry-run）— three-buckets-v2。

桶：
1. deleted_predictions — 软删且 deleted_at 满 N 天；**is_correct 非空永不进候选**
2. cleanup_item_logs — created_at 满 M 天
3. unverifiable_predictions — lifecycle=unverifiable 且目标日龄满 K 天；无结论
4. deleted_viewpoints — 软删观点，锚点 **deleted_at** 满 30 天（不看 valid_until）
5. summary_viewpoints — is_summary，锚点 **viewpoint_date** 满窗口（默认 90 天）
6. orphan_funds — 没有任何预测/映射/绑定/持仓/观点引用的基金，连同其全部净值
7. stale_fund_history — 仍被引用的基金，其早于「最早未结预测起点 - 宽限」且
   超出保底最近 N 条的净值行

净值是本库最大的表，6 与 7 才是真正腾空间的两桶；1-5 主要是清账。
全局单次上限 max_total_per_run；真删需 dry_run=False + confirm_token。
在线入口：`POST /api/config/cleanup/three-buckets`（开关 + 确认头 + 预览指纹）。
删除后由 `src/services/db_space.py` 回收磁盘空间（Postgres 需 VACUUM FULL）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session, load_only

from src.constants.sector_fund_map import SECTOR_ALIASES, SECTOR_FUND_MAP
from src.models.database import (
    CleanupItemLog,
    CleanupLog,
    FundHistory,
    FundHolding,
    FundInfo,
    FundSyncRetry,
    Prediction,
    SectorFundMapping,
    UserFundBinding,
    Viewpoint,
)
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
    # 净值：保底每只基金留最近 N 条（喂前端 30 天走势图 + 周/月涨幅计算）
    fund_history_keep_recent: int = 40
    # 未结预测起点再往前留几天，容错「起点日无净值需取前值」
    fund_history_grace_days: int = 15
    # 已验证预测的目标日在这个窗口内时，其净值窗口仍受保护（便于回溯核对）
    verified_lookback_days: int = 90
    # 基金本身多久没更新才算可回收（配合无引用判定）
    orphan_fund_stale_days: int = 30
    # 净值行单条极小、数量极大，与业务行分开计额度
    max_fund_history_per_run: int = 20_000
    batch_size: int = 200
    max_per_bucket: int = 500
    max_total_per_run: int = 500  # 全局单次上限（业务桶，跨桶共享）

    def to_dict(self) -> Dict[str, int]:
        return {
            "deleted_hard_delete_days": self.deleted_hard_delete_days,
            "cleanup_item_log_days": self.cleanup_item_log_days,
            "unverifiable_days": self.unverifiable_days,
            "deleted_viewpoint_days": self.deleted_viewpoint_days,
            "summary_viewpoint_days": self.summary_viewpoint_days,
            "fund_history_keep_recent": self.fund_history_keep_recent,
            "fund_history_grace_days": self.fund_history_grace_days,
            "verified_lookback_days": self.verified_lookback_days,
            "orphan_fund_stale_days": self.orphan_fund_stale_days,
            "max_fund_history_per_run": self.max_fund_history_per_run,
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
    BUCKET_ORPHAN_FUNDS = "orphan_funds"
    BUCKET_STALE_HISTORY = "stale_fund_history"
    BUCKETS = (
        BUCKET_DELETED,
        BUCKET_LOGS,
        BUCKET_UNVERIFIABLE,
        BUCKET_DELETED_VP,
        BUCKET_SUMMARY_VP,
        BUCKET_ORPHAN_FUNDS,
        BUCKET_STALE_HISTORY,
    )

    # 前端可读标签（与 web/index.html 的清理分类标签共用一套口径）
    BUCKET_LABELS = {
        BUCKET_DELETED: "回收站预测",
        BUCKET_LOGS: "清理明细日志",
        BUCKET_UNVERIFIABLE: "已错过验证窗口的预测",
        BUCKET_DELETED_VP: "回收站观点",
        BUCKET_SUMMARY_VP: "历史每日汇总",
        BUCKET_ORPHAN_FUNDS: "无引用基金（连带其全部净值）",
        BUCKET_STALE_HISTORY: "过期净值记录",
    }

    # 这些桶单条体量小/风险低，可以给更宽的单次额度
    HIGH_VOLUME_BUCKETS = (BUCKET_STALE_HISTORY,)

    # 需要 VACUUM 回收空间的表（按桶）
    BUCKET_TABLES = {
        BUCKET_DELETED: ("predictions",),
        BUCKET_LOGS: ("cleanup_item_logs",),
        BUCKET_UNVERIFIABLE: ("predictions",),
        BUCKET_DELETED_VP: ("viewpoints",),
        BUCKET_SUMMARY_VP: ("viewpoints",),
        BUCKET_ORPHAN_FUNDS: ("fund_info", "fund_history", "fund_sync_retry"),
        BUCKET_STALE_HISTORY: ("fund_history",),
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
        # 连带删除计数（如删基金时跟着走的净值行），只在一次 execute 内有效
        self._cascade_counts: Dict[str, int] = {}

    def build_plan(self) -> BucketPlan:
        plan = BucketPlan(as_of=self.today, policy=self.policy)
        deleted_ids, protected_verified = self._deleted_prediction_ids()
        plan.protected_counts["verified_ledger_excluded"] = protected_verified

        orphan_fund_ids, orphan_codes, protected_funds = self._orphan_fund_ids()
        plan.protected_counts["referenced_funds_kept"] = protected_funds
        stale_history_ids, history_protected = self._stale_fund_history_ids(orphan_codes)
        plan.protected_counts.update(history_protected)

        raw: Dict[str, List[int]] = {
            self.BUCKET_DELETED: deleted_ids,
            self.BUCKET_LOGS: self._cleanup_item_log_ids(),
            self.BUCKET_UNVERIFIABLE: self._unverifiable_prediction_ids(),
            self.BUCKET_DELETED_VP: self._deleted_viewpoint_ids(),
            self.BUCKET_SUMMARY_VP: self._summary_viewpoint_ids(),
            self.BUCKET_ORPHAN_FUNDS: orphan_fund_ids,
            self.BUCKET_STALE_HISTORY: stale_history_ids,
        }
        plan.candidate_ids, plan.truncated = self._apply_caps(raw)
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
            self.BUCKET_ORPHAN_FUNDS: (
                "无任何预测/板块映射/用户绑定/持仓/观点引用，且 "
                f"{self.policy.orphan_fund_stale_days} 天未更新；"
                "删除时连带该基金的全部净值与同步重试记录"
            ),
            self.BUCKET_STALE_HISTORY: (
                "仍被引用的基金：早于「最早未结预测起点 − "
                f"{self.policy.fund_history_grace_days} 天」且超出保底最近 "
                f"{self.policy.fund_history_keep_recent} 条的净值行；"
                f"已验证预测在 {self.policy.verified_lookback_days} 天内的窗口也保护"
            ),
        }
        plan.fingerprint = self._fingerprint(plan)
        return plan

    def _apply_caps(
        self, raw: Dict[str, List[int]]
    ) -> Tuple[Dict[str, List[int]], bool]:
        """业务桶共享 max_total_per_run；净值桶单独用 max_fund_history_per_run。

        净值行单条几十字节但数量上万，跟预测挤同一个 500 额度会导致永远清不完。
        """
        capped: Dict[str, List[int]] = {}
        truncated = False
        remaining = self.policy.max_total_per_run
        history_remaining = self.policy.max_fund_history_per_run
        for name in self.BUCKETS:
            ids = raw.get(name, [])
            if name in self.HIGH_VOLUME_BUCKETS:
                if len(ids) > history_remaining:
                    capped[name] = ids[:history_remaining]
                    truncated = True
                    history_remaining = 0
                else:
                    capped[name] = ids
                    history_remaining -= len(ids)
                continue
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
        return capped, truncated

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
        reclaim_space: bool = True,
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
            elif bucket == self.BUCKET_ORPHAN_FUNDS:
                deleted = self._hard_delete_orphan_funds(
                    ids, on_progress=lambda n, b=bucket: report_progress(b, n)
                )
            elif bucket == self.BUCKET_STALE_HISTORY:
                deleted = self._hard_delete_fund_history(
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
        cascade_counts = dict(self._cascade_counts)
        self._cascade_counts.clear()
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
                "cascade_counts": cascade_counts,
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
        report["cascade_counts"] = cascade_counts
        report["total_deleted"] = total_deleted
        report["total_rows_removed"] = total_deleted + sum(cascade_counts.values())
        report["cleanup_log_id"] = log.id
        report["message"] = "hard-delete completed"

        # 删完回收磁盘空间：Postgres 删行只标记死元组，不 VACUUM 文件不会变小
        if reclaim_space and total_deleted:
            tables: List[str] = []
            for bucket in sorted(selected):
                if deleted_counts.get(bucket):
                    tables.extend(self.BUCKET_TABLES.get(bucket, ()))
            report["space_reclaim"] = self.reclaim_space(tables)
        return report

    def reclaim_space(self, tables: Sequence[str]) -> dict:
        """对受影响的表回收空间；失败只记录不抛（删除已成功，不该回滚）。"""
        from src.services.db_space import reclaim_space as _reclaim

        try:
            return _reclaim(self.db, tables)
        except Exception as exc:  # pragma: no cover - 防御
            return {"success": False, "error": str(exc), "tables": {}}

    def estimate_cascade_rows(self, plan: BucketPlan) -> Dict[str, int]:
        """预览用：删无引用基金会连带删掉多少净值/重试行。

        这些行不在 candidate_ids 里（按 fund_info.id 计数），但会真的消失，
        对「腾空间」判断很关键，所以单独报出来。
        """
        fund_ids = plan.candidate_ids.get(self.BUCKET_ORPHAN_FUNDS, [])
        if not fund_ids:
            return {}
        codes = [
            row.fund_code
            for row in self.db.query(FundInfo.fund_code)
            .filter(FundInfo.id.in_(fund_ids))
            .all()
            if row.fund_code
        ]
        if not codes:
            return {}
        history = (
            self.db.query(func.count(FundHistory.id))
            .filter(FundHistory.fund_code.in_(codes))
            .scalar()
            or 0
        )
        retries = (
            self.db.query(func.count(FundSyncRetry.id))
            .filter(FundSyncRetry.fund_code.in_(codes))
            .scalar()
            or 0
        )
        out: Dict[str, int] = {}
        if history:
            out["fund_history"] = int(history)
        if retries:
            out["fund_sync_retry"] = int(retries)
        return out

    def table_sizes(self) -> Dict[str, int]:
        """各主表当前行数，供前端展示「哪张表最占地方」。"""
        targets = {
            "fund_history": FundHistory,
            "predictions": Prediction,
            "viewpoints": Viewpoint,
            "fund_info": FundInfo,
            "cleanup_item_logs": CleanupItemLog,
        }
        sizes: Dict[str, int] = {}
        for name, model in targets.items():
            try:
                sizes[name] = int(
                    self.db.query(func.count(model.id)).scalar() or 0
                )
            except Exception:
                continue
        return sizes

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

    # ----- 净值相关：本库最大的表，真正腾空间的两桶 -----

    def _referenced_fund_codes(self) -> Set[str]:
        """所有「还有人用」的基金代码。

        判定尽量宽：任何预测（含软删）、板块映射、用户绑定、持仓、观点、
        待重试同步都算引用。宁可少删也不能把仍在用的基金连净值一起删掉。
        """
        codes: Set[str] = set()

        funds = self.db.query(FundInfo.fund_code, FundInfo.fund_name, FundInfo.sector_type).all()
        mappings = self.db.query(
            SectorFundMapping.sector_name, SectorFundMapping.fund_code, SectorFundMapping.is_active
        ).all()
        bindings = self.db.query(UserFundBinding.sector, UserFundBinding.fund_code).all()

        # 映射/绑定/持仓本身即引用
        codes.update(row.fund_code for row in mappings if row.fund_code)
        codes.update(row.fund_code for row in bindings if row.fund_code)
        codes.update(
            row.fund_code
            for row in self.db.query(FundHolding.fund_code).distinct().all()
            if row.fund_code
        )
        codes.update(
            row.fund_code
            for row in self.db.query(Viewpoint.fund_code).distinct().all()
            if row.fund_code
        )
        codes.update(
            row.fund_code
            for row in self.db.query(FundSyncRetry.fund_code)
            .filter(FundSyncRetry.status.in_(("pending", "retrying", "running")))
            .distinct()
            .all()
            if row.fund_code
        )

        # 预测：直接代码 + 基金名 + 板块解析（含软删预测，避免恢复后无净值）
        for row in self.db.query(
            Prediction.fund_code, Prediction.fund_name, Prediction.sector, Prediction.sector_type
        ).all():
            codes.update(
                self._resolve_codes(
                    row.fund_code, row.fund_name, row.sector, row.sector_type,
                    funds=funds, mappings=mappings, bindings=bindings,
                )
            )
        return {code for code in codes if code}

    def _resolve_codes(
        self,
        fund_code: Optional[str],
        fund_name: Optional[str],
        sector: Optional[str],
        sector_type: Optional[str],
        *,
        funds,
        mappings,
        bindings,
    ) -> Set[str]:
        """把一条预测解析为它可能依赖的基金代码集合（与验证匹配逻辑同口径）。"""
        codes: Set[str] = set()
        if fund_code:
            codes.add(fund_code)
        if fund_name:
            codes.update(f.fund_code for f in funds if f.fund_name == fund_name)
        raw_sector = (sector or sector_type or "").strip()
        if not raw_sector:
            return codes
        standard = SECTOR_ALIASES.get(raw_sector, raw_sector)
        for mapping in mappings:
            if mapping.is_active is not False and self._sector_matches(
                standard, mapping.sector_name
            ):
                codes.add(mapping.fund_code)
        for binding in bindings:
            if self._sector_matches(standard, binding.sector):
                codes.add(binding.fund_code)
        for mapped_sector, fund in SECTOR_FUND_MAP.items():
            if self._sector_matches(standard, mapped_sector):
                code = fund.get("code")
                if code:
                    codes.add(code)
        for fund in funds:
            if fund.sector_type and self._sector_matches(standard, fund.sector_type):
                codes.add(fund.fund_code)
        return codes

    @staticmethod
    def _sector_matches(left: Optional[str], right: Optional[str]) -> bool:
        left = (left or "").strip()
        right = (right or "").strip()
        return bool(left and right and (left == right or left in right or right in left))

    def _orphan_fund_ids(self) -> Tuple[List[int], Set[str], int]:
        """返回 (可删 fund_info.id, 可删代码集合, 因被引用而保留的基金数)。

        「没有任何预测之后就可以整只删掉，净值跟着走」——这就是那一桶。
        """
        referenced = self._referenced_fund_codes()
        stale_cutoff = datetime.combine(
            self.today - timedelta(days=self.policy.orphan_fund_stale_days),
            datetime.min.time(),
        )
        ids: List[int] = []
        codes: Set[str] = set()
        protected = 0
        rows = (
            self.db.query(FundInfo)
            .options(
                load_only(
                    FundInfo.id,
                    FundInfo.fund_code,
                    FundInfo.fund_name,
                    FundInfo.is_core_fund,
                    FundInfo.can_delete,
                    FundInfo.updated_at,
                    FundInfo.nav_date,
                )
            )
            .order_by(FundInfo.id.asc())
            .all()
        )
        for fund in rows:
            protected_flag = bool(fund.is_core_fund) or fund.can_delete is False
            if protected_flag or fund.fund_code in referenced:
                protected += 1
                continue
            # 近期还在更新的基金先留着，避免刚加入还没配预测就被清掉
            if fund.updated_at is not None and fund.updated_at >= stale_cutoff:
                protected += 1
                continue
            ids.append(fund.id)
            codes.add(fund.fund_code)
            if len(ids) >= self.policy.max_per_bucket:
                break
        return ids, codes, protected

    def _protected_history_floors(self) -> Tuple[Dict[str, date], int]:
        """每只基金的「净值保护下界」：早于它的净值才可删。

        下界 = 最早仍需净值的预测起点 − grace。仍需净值 =
          - 未验证（is_correct is null）的任何预测，或
          - 已验证但目标日在 verified_lookback_days 内（便于回溯核对）
        """
        funds = self.db.query(FundInfo.fund_code, FundInfo.fund_name, FundInfo.sector_type).all()
        mappings = self.db.query(
            SectorFundMapping.sector_name, SectorFundMapping.fund_code, SectorFundMapping.is_active
        ).all()
        bindings = self.db.query(UserFundBinding.sector, UserFundBinding.fund_code).all()

        lookback_floor = self.today - timedelta(days=self.policy.verified_lookback_days)
        floors: Dict[str, date] = {}
        relevant = 0
        rows = self.db.query(
            Prediction.fund_code,
            Prediction.fund_name,
            Prediction.sector,
            Prediction.sector_type,
            Prediction.prediction_date,
            Prediction.target_date,
            Prediction.start_nav_date,
            Prediction.is_correct,
            Prediction.is_deleted,
        ).all()
        for row in rows:
            if row.is_correct is None:
                # 未验证：无论是否软删都保护（软删可恢复）
                needed = True
            else:
                needed = bool(row.target_date and row.target_date >= lookback_floor)
            if not needed:
                continue
            relevant += 1
            anchors = [d for d in (row.prediction_date, row.start_nav_date) if d]
            if not anchors:
                continue
            start = min(anchors) - timedelta(days=self.policy.fund_history_grace_days)
            for code in self._resolve_codes(
                row.fund_code, row.fund_name, row.sector, row.sector_type,
                funds=funds, mappings=mappings, bindings=bindings,
            ):
                if code not in floors or start < floors[code]:
                    floors[code] = start
        return floors, relevant

    def _stale_fund_history_ids(
        self, orphan_codes: Set[str]
    ) -> Tuple[List[int], Dict[str, int]]:
        """仍被引用的基金里，早于保护下界且超出保底条数的净值行。

        orphan_codes 的净值由 orphan_funds 桶连带删除，这里跳过以免重复计数。
        """
        floors, relevant_predictions = self._protected_history_floors()
        keep_recent = max(1, self.policy.fund_history_keep_recent)

        rows = (
            self.db.query(FundHistory.id, FundHistory.fund_code, FundHistory.nav_date)
            .order_by(FundHistory.fund_code.asc(), FundHistory.nav_date.desc())
            .all()
        )
        by_fund: Dict[str, List[tuple]] = {}
        for hist_id, fund_code, nav_date in rows:
            by_fund.setdefault(fund_code, []).append((hist_id, nav_date))

        candidates: List[int] = []
        protected_rows = 0
        for fund_code, entries in sorted(by_fund.items()):
            if fund_code in orphan_codes:
                continue
            # entries 已按 nav_date 降序：保底留前 keep_recent 条
            keep_floor = entries[min(keep_recent, len(entries)) - 1][1]
            floor = keep_floor
            predicted_floor = floors.get(fund_code)
            if predicted_floor is not None:
                # 起点日当天可能无净值，需保留其前一条作为起点前值
                anchor = next(
                    (nav_date for _, nav_date in entries if nav_date <= predicted_floor),
                    None,
                )
                effective = anchor if anchor is not None else predicted_floor
                floor = min(floor, effective)
            for hist_id, nav_date in entries:
                if nav_date < floor:
                    candidates.append(hist_id)
                else:
                    protected_rows += 1
        return candidates, {
            "fund_history_rows_kept": protected_rows,
            "history_relevant_predictions": relevant_predictions,
            "funds_with_history": len(by_fund),
        }

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
        if bucket == self.BUCKET_ORPHAN_FUNDS:
            rows = self.db.query(FundInfo).filter(FundInfo.id.in_(sample_ids)).all()
            history_counts = dict(
                self.db.query(FundHistory.fund_code, func.count(FundHistory.id))
                .filter(FundHistory.fund_code.in_([r.fund_code for r in rows]))
                .group_by(FundHistory.fund_code)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "fund_code": r.fund_code,
                    "fund_name": r.fund_name,
                    "nav_date": r.nav_date.isoformat() if r.nav_date else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    "history_rows": int(history_counts.get(r.fund_code, 0)),
                }
                for r in rows
            ]
        if bucket == self.BUCKET_STALE_HISTORY:
            rows = (
                self.db.query(FundHistory)
                .filter(FundHistory.id.in_(sample_ids))
                .order_by(FundHistory.fund_code.asc(), FundHistory.nav_date.asc())
                .all()
            )
            return [
                {
                    "id": r.id,
                    "fund_code": r.fund_code,
                    "fund_name": r.fund_name,
                    "nav_date": r.nav_date.isoformat() if r.nav_date else None,
                    "nav": r.nav,
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

    def _bulk_batches(self, ids: List[int], size: int = 2000):
        """净值行走大批次：单条极小，200 一批要跑上百轮。"""
        for i in range(0, len(ids), size):
            yield ids[i : i + size]

    def _hard_delete_orphan_funds(
        self,
        ids: List[int],
        *,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> int:
        """删无引用基金，连带其全部净值与同步重试记录。

        删除前再查一遍活跃预测，防止预览与执行之间刚好新增了预测。
        """
        if not ids:
            return 0
        deleted = 0
        for batch in self._batches(list(ids)):
            rows = self.db.query(FundInfo).filter(FundInfo.id.in_(batch)).all()
            codes = [r.fund_code for r in rows if r.fund_code]
            if codes:
                still_used = {
                    row.fund_code
                    for row in self.db.query(Prediction.fund_code)
                    .filter(
                        Prediction.fund_code.in_(codes),
                        Prediction.is_deleted.is_(False),
                    )
                    .distinct()
                    .all()
                    if row.fund_code
                }
                if still_used:
                    rows = [r for r in rows if r.fund_code not in still_used]
                    codes = [r.fund_code for r in rows if r.fund_code]
            if not rows:
                continue
            history_removed = (
                self.db.query(FundHistory)
                .filter(FundHistory.fund_code.in_(codes))
                .delete(synchronize_session=False)
            )
            retry_removed = (
                self.db.query(FundSyncRetry)
                .filter(FundSyncRetry.fund_code.in_(codes))
                .delete(synchronize_session=False)
            )
            self._cascade_counts["fund_history"] = (
                self._cascade_counts.get("fund_history", 0) + int(history_removed or 0)
            )
            self._cascade_counts["fund_sync_retry"] = (
                self._cascade_counts.get("fund_sync_retry", 0) + int(retry_removed or 0)
            )
            for row in rows:
                self.db.delete(row)
                deleted += 1
            self.db.flush()
            self.db.commit()
            if on_progress:
                on_progress(deleted)
        return deleted

    def _hard_delete_fund_history(
        self,
        ids: List[int],
        *,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> int:
        """批量删净值行：走 bulk delete，不逐行加载 ORM 对象。"""
        if not ids:
            return 0
        deleted = 0
        for batch in self._bulk_batches(list(ids)):
            removed = (
                self.db.query(FundHistory)
                .filter(FundHistory.id.in_(batch))
                .delete(synchronize_session=False)
            )
            deleted += int(removed or 0)
            self.db.commit()
            if on_progress:
                on_progress(deleted)
        return deleted

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
