from datetime import datetime
from threading import Lock, Thread
from typing import Callable, Dict


class FundUpdateTask:
    def __init__(self):
        self._lock = Lock()
        self._in_progress = False
        self._started_at = None
        self._finished_at = None
        self._last_result = None

    def start(self, runner: Callable[[], Dict], run_inline: bool = False, keep_running: bool = False) -> Dict:
        if not self._lock.acquire(blocking=False):
            return {"success": False, "message": "基金更新正在进行中，请稍后再试", "data": self.status()}

        self._in_progress = True
        self._started_at = datetime.now()
        self._finished_at = None
        self._last_result = None

        def execute():
            try:
                self._last_result = runner()
            except Exception as exc:
                self._last_result = {"success": False, "message": f"更新失败: {exc}"}
            finally:
                if not keep_running:
                    self._in_progress = False
                    self._finished_at = datetime.now()
                    self._lock.release()

        if run_inline:
            execute()
        else:
            Thread(target=execute, daemon=True).start()

        return {"success": True, "message": "基金更新任务已启动", "data": self.status()}

    def status(self) -> Dict:
        return {
            "in_progress": self._in_progress,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "finished_at": self._finished_at.isoformat() if self._finished_at else None,
            "last_result": self._last_result,
        }


fund_update_task = FundUpdateTask()
