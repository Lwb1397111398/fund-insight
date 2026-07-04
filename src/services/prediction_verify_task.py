from datetime import datetime
from threading import Lock
from typing import Dict, Optional


class PredictionVerifyTask:
    """记录批量预测验证后台任务状态。"""

    def __init__(self):
        self._lock = Lock()
        self._in_progress = False
        self._started_at: Optional[datetime] = None
        self._finished_at: Optional[datetime] = None
        self._last_result: Optional[Dict] = None
        self._total = 0

    def start(self, total: int) -> Dict:
        if not self._lock.acquire(blocking=False):
            return {
                "success": False,
                "message": "预测验证正在进行中，请稍后再试",
                "data": self.status(),
            }

        self._in_progress = True
        self._started_at = datetime.now()
        self._finished_at = None
        self._last_result = None
        self._total = total

        return {
            "success": True,
            "message": "预测验证任务已启动",
            "data": self.status(),
        }

    def finish(self, result: Dict):
        self._last_result = result
        self._in_progress = False
        self._finished_at = datetime.now()
        self._lock.release()

    def status(self) -> Dict:
        return {
            "in_progress": self._in_progress,
            "total": self._total,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "finished_at": self._finished_at.isoformat() if self._finished_at else None,
            "last_result": self._last_result,
        }


prediction_verify_task = PredictionVerifyTask()
