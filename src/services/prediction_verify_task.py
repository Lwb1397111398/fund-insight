from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.models.database import BatchAnalysisTask


_PROCESS_LOCK = Lock()
_ACTIVE_STATUSES = ("pending", "running")
_POSTGRES_LOCK_ID = 73462025


# 判据 reason 的人话名字：前端"上次验证未成功原因"按这个合并展示，
# 没有登记的 reason 原样透出（不猜、不编）。
REASON_LABELS = {
    'same_nav_endpoint': '目标日没有独立净值（起点与终点是同一天），无法判定方向',
    'no_source_history': '已问过数据源，该区间它给不出足够净值 ⇒ 结构性不可验',
    'insufficient_points': '窗口内净值条数不足',
    'no_history': '该基金本地无历史净值',
    'waiting_target_nav': '目标日净值尚未发布，等待中',
    'end_nav_too_old': '终点净值太旧，超过允许陈旧期限',
    'endpoint_lag_unproven': '端点早于目标日且中间还有工作日没有净值，'
                             '无法区分"市场没有"与"本地缺行" ⇒ 需按区间回补历史',
}

class PredictionVerifyTask:
    """Track batch verification in the database, with an in-memory fallback for tests."""

    def __init__(self, stale_after: timedelta = timedelta(minutes=30)):
        self._lock = Lock()
        self._in_progress = False
        self._started_at: Optional[datetime] = None
        self._finished_at: Optional[datetime] = None
        self._last_result: Optional[Dict] = None
        self._total = 0
        self._stale_after = stale_after

    def start(self, total: int, db: Optional[Session] = None) -> Dict:
        if db is None:
            return self._start_in_memory(total)

        if not _PROCESS_LOCK.acquire(blocking=False):
            return self._already_running(self.status(db=db))

        try:
            if not self._acquire_database_start_lock(db):
                db.rollback()
                return self._already_running(self.status(db=db))

            now = datetime.now()
            active = self._latest(db, statuses=_ACTIVE_STATUSES)
            if active and not self._is_stale(active, now):
                return self._already_running(self._serialize(active))

            if active:
                active.status = "failed"
                active.error_message = self._stale_note(active)
                active.completed_at = now
                active.updated_at = now

            task = BatchAnalysisTask(
                task_type="predictions",
                status="running",
                total_count=total,
                processed_count=0,
                success_count=0,
                failed_count=0,
                processed_ids=[],
                failed_ids=[],
                started_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(task)
            db.commit()
            db.refresh(task)
            return {
                "success": True,
                "message": "预测验证任务已启动",
                "data": self._serialize(task),
            }
        except Exception:
            db.rollback()
            raise
        finally:
            _PROCESS_LOCK.release()

    def update_progress(
        self,
        processed: int,
        success_count: int,
        failed_count: int,
        total: Optional[int] = None,
        db: Optional[Session] = None,
        task_id: Optional[int] = None,
    ) -> None:
        """验证过程中更新已处理条数，让前端进度条随轮询增长。

        每条提交一次并立即 expire，保证独立连接能看到最新计数，
        也避免长时间运行累计大量未提交脏数据。
        """
        if db is None or task_id is None:
            return

        try:
            task = db.get(BatchAnalysisTask, task_id)
            if task is None:
                return
            task.processed_count = max(0, int(processed))
            task.success_count = max(0, int(success_count))
            task.failed_count = max(0, int(failed_count))
            if total is not None:
                task.total_count = max(0, int(total))
            task.updated_at = datetime.now()
            db.commit()
            db.expire(task)
        except Exception:
            # 进度上报失败不得中断验证主流程
            db.rollback()

    def finish(
        self,
        result: Dict,
        db: Optional[Session] = None,
        task_id: Optional[int] = None,
    ) -> None:
        if db is None:
            self._finish_in_memory(result)
            return

        try:
            task = db.get(BatchAnalysisTask, task_id) if task_id else self._latest(
                db, statuses=_ACTIVE_STATUSES
            )
            if task is None:
                return

            data = result.get("data") or {}
            success_count = data.get("success_count")
            failed_count = data.get("failed_count")
            processed_count = data.get("processed_count")
            task.status = "completed" if result.get("success") else "failed"
            task.total_count = int(data.get("total") or task.total_count or 0)
            # 只有回执**真的带了数**时才改写计数。崩在半路的那一支
            # （`_verify_all_background` 的 except 只填 message、没有 data）以前会把
            # 已经推进到的 209/239 一路 `int(... or 0)` 归零 ⇒ 台账上"跑了 209 条然后断"
            # 与"一条都没跑"长成同一个样子，页面上那句"还剩 N 条"也跟着变成假的。
            if processed_count is not None or success_count is not None or failed_count is not None:
                s = max(0, int(success_count or 0))
                f = max(0, int(failed_count or 0))
                task.processed_count = max(0, int(processed_count)) \
                    if processed_count is not None else s + f
                task.success_count = s
                task.failed_count = f
            task.result_summary = result
            task.error_message = None if result.get("success") else result.get("message")
            task.completed_at = datetime.now()
            task.updated_at = task.completed_at
            db.commit()
        except Exception:
            db.rollback()
            raise

    def status(self, db: Optional[Session] = None) -> Dict:
        if db is None:
            return self._memory_status()
        task = self._latest(db)
        if task is None:
            return self._empty_status()
        # 自愈：用户中途关网页导致后台任务失联时，DB 里会残留 status='running'。
        # 只在 start() 判超时不够——前端轮询 status 会永远拿到 in_progress=True，
        # 按钮卡在"验证中"且无法再次点击。这里顺手把超时任务标记失败。
        if task.status in _ACTIVE_STATUSES and self._is_stale(task, datetime.now()):
            self._mark_stale_failed(db, task)
        return self._serialize(task)

    def _stale_note(self, task: BatchAnalysisTask) -> str:
        """判死时说的话只有一处实现（`start()` 与 `status()` 两处都要用）。

        必须带上"推进到几条"：光说"超时已终止"，老板分不清是**一条都没跑**还是
        跑了 209/239 然后断在半路 —— 后者的意思是"还剩 30 条没验证"。
        """
        return ("检测到批次中断（%d 分钟没有新的进展，已推进到 %s/%s 条）"
                "⇒ 还有 %s 条没验证，可重新发起"
                % (int(self._stale_after.total_seconds() // 60),
                   task.processed_count or 0, task.total_count or 0,
                   self._shortfall(task)))

    @staticmethod
    def _shortfall(task: BatchAnalysisTask) -> int:
        """队列里说好要跑、这一轮实际没被跑到的条数。"""
        return max(0, (task.total_count or 0) - (task.processed_count or 0))

    def _mark_stale_failed(self, db: Session, task: BatchAnalysisTask) -> None:
        now = datetime.now()
        try:
            task.status = "failed"
            task.error_message = self._stale_note(task)
            task.completed_at = now
            task.updated_at = now
            db.commit()
        except Exception:
            db.rollback()
            raise

    def _start_in_memory(self, total: int) -> Dict:
        if not self._lock.acquire(blocking=False):
            return self._already_running(self._memory_status())
        self._in_progress = True
        self._started_at = datetime.now()
        self._finished_at = None
        self._last_result = None
        self._total = total
        return {
            "success": True,
            "message": "预测验证任务已启动",
            "data": self._memory_status(),
        }

    def _finish_in_memory(self, result: Dict) -> None:
        self._last_result = result
        self._in_progress = False
        self._finished_at = datetime.now()
        if self._lock.locked():
            self._lock.release()

    def _memory_status(self) -> Dict:
        return {
            "task_id": None,
            "in_progress": self._in_progress,
            "total": self._total,
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "not_processed": 0,
            "progress": 0,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "finished_at": self._finished_at.isoformat() if self._finished_at else None,
            "last_result": self._last_result,
        }

    def _latest(self, db: Session, statuses=None) -> Optional[BatchAnalysisTask]:
        query = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.task_type == "predictions"
        )
        if statuses:
            query = query.filter(BatchAnalysisTask.status.in_(statuses))
        return query.order_by(BatchAnalysisTask.id.desc()).first()

    def _is_stale(self, task: BatchAnalysisTask, now: datetime) -> bool:
        """问的是"最后一次心跳离现在多久"，不是"这任务开了多久"。

        生产实测（`batch_analysis_tasks` 219，2026-09-26 18:44:01→19:18:35）：一批 239 条
        跑了 34.5 分钟，而旧写法拿 `started_at` 当参照 —— 那根针**永远不推进** ⇒ 第 30 分钟
        `status()` 把还在推进的批次标成 failed、`in_progress` 翻成 false、按钮当场解锁，
        于是 19:16 真的又起了第二批（220），两批并发写同一批预测的结论。
        帖子（`post_analysis_service.heal_stale_job`）与观点（`heal_stale_task`）两支
        早就是心跳语义，只有这一支是异类。`update_progress` 每条都刷 `updated_at`，
        健康批次不可能 30 分钟无心跳；真卡在某一条上 30 分钟仍会被判死 —— 那才是要判死的形状。
        """
        reference = task.updated_at or task.started_at or task.created_at
        return reference is not None and now - reference > self._stale_after

    @staticmethod
    def _acquire_database_start_lock(db: Session) -> bool:
        if db.get_bind().dialect.name != "postgresql":
            return True
        return bool(
            db.execute(
                text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                {"lock_id": _POSTGRES_LOCK_ID},
            ).scalar()
        )

    @staticmethod
    def _already_running(status: Dict) -> Dict:
        return {
            "success": False,
            "message": "预测验证正在进行中，请稍后再试",
            "data": status,
        }

    @staticmethod
    def _empty_status() -> Dict:
        return {
            "task_id": None,
            "in_progress": False,
            "total": 0,
            "processed_count": 0,
            "success_count": 0,
            "failed_count": 0,
            "not_processed": 0,
            "progress": 0,
            "started_at": None,
            "finished_at": None,
            "last_result": None,
        }

    @staticmethod
    def _summarize_failures(result: Dict) -> str:
        """汇总批量验证未成功条目的原因（相同原因合并计数），供前端直接展示。"""
        data = result.get("data") or {}
        entries = list(data.get("results") or []) + [
            dict(item, success=False) for item in (data.get("skipped") or [])
        ]
        counts: Dict[str, int] = {}
        for entry in entries:
            if entry.get("success"):
                continue
            # 先按 reason 合并：message 每条都嵌日期，直接当分组键会把
            # "N 条同一类原因"打成 N 行不可读的长文案（第 12 轮 MINOR-5）
            key = entry.get("reason") or entry.get("message") or "未知原因"
            reason = REASON_LABELS.get(key, key)[:80]
            counts[reason] = counts.get(reason, 0) + 1
        return "；".join(f"{reason}（{count} 条）" for reason, count in counts.items())

    @staticmethod
    def _serialize(task: BatchAnalysisTask) -> Dict:
        total = task.total_count or 0
        processed = task.processed_count or 0
        # 跑完之后这个数就是"这一批说好要跑、实际没被跑到"的条数；进行中它就是剩余条数。
        # 崩在半路的批次以前从这里看不出来：状态翻成 completed/failed、failed_count 还是
        # 上一颗心跳的 0 ⇒ 页面既不说"没跑够"也不说"为什么"。
        not_processed = max(0, total - processed)
        last_result = task.result_summary
        failure_summary = None
        if task.status not in _ACTIVE_STATUSES:
            parts = []
            if last_result:
                summary = PredictionVerifyTask._summarize_failures(last_result)
                if summary:
                    parts.append(summary)
            if not_processed:
                parts.append(
                    "这一批队列里 %d 条、只跑到 %d 条 ⇒ 还有 %d 条没验证"
                    % (total, processed, not_processed)
                    + ("（%s）" % task.error_message if task.error_message else ""))
            if parts:
                failure_summary = "；".join(parts)
        return {
            "task_id": task.id,
            "in_progress": task.status in _ACTIVE_STATUSES,
            "total": total,
            "processed_count": processed,
            "success_count": task.success_count or 0,
            "failed_count": task.failed_count or 0,
            "not_processed": not_processed,
            "progress": round(processed * 100 / total, 1) if total else 0,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "finished_at": task.completed_at.isoformat() if task.completed_at else None,
            "last_result": last_result,
            "failure_summary": failure_summary,
        }


prediction_verify_task = PredictionVerifyTask()
