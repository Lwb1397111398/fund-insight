from datetime import datetime, timedelta
from threading import Lock
from typing import Dict, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.models.database import BatchAnalysisTask


_PROCESS_LOCK = Lock()
_ACTIVE_STATUSES = ("pending", "running")
_POSTGRES_LOCK_ID = 73462025


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
                active.error_message = "任务运行超时，已自动终止"
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
            success_count = int(data.get("success_count", 0) or 0)
            failed_count = int(data.get("failed_count", 0) or 0)
            processed_count = int(
                data.get("processed_count", success_count + failed_count) or 0
            )
            task.status = "completed" if result.get("success") else "failed"
            task.total_count = int(data.get("total", task.total_count) or task.total_count or 0)
            task.processed_count = processed_count
            task.success_count = success_count
            task.failed_count = failed_count
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

    def _mark_stale_failed(self, db: Session, task: BatchAnalysisTask) -> None:
        now = datetime.now()
        try:
            task.status = "failed"
            task.error_message = "任务运行超时，已自动终止"
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
        reference = task.started_at or task.updated_at or task.created_at
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
            "progress": 0,
            "started_at": None,
            "finished_at": None,
            "last_result": None,
        }

    @staticmethod
    def _serialize(task: BatchAnalysisTask) -> Dict:
        total = task.total_count or 0
        processed = task.processed_count or 0
        return {
            "task_id": task.id,
            "in_progress": task.status in _ACTIVE_STATUSES,
            "total": total,
            "processed_count": processed,
            "success_count": task.success_count or 0,
            "failed_count": task.failed_count or 0,
            "progress": round(processed * 100 / total, 1) if total else 0,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "finished_at": task.completed_at.isoformat() if task.completed_at else None,
            "last_result": task.result_summary,
        }


prediction_verify_task = PredictionVerifyTask()
