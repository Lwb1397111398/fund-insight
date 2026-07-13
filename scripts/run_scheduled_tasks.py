"""
Render Cron 一次性定时任务入口
"""
import argparse
import logging
import os
import sys
from datetime import datetime

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.models.database import init_db
from src.tasks.scheduler import TaskScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _normalize_task_result(result) -> dict:
    if result is None:
        return {"success": True}
    if isinstance(result, dict):
        normalized = dict(result)
        normalized.setdefault("success", True)
        return normalized
    return {"success": bool(result), "result": result}


def run_daily_tasks() -> dict:
    started_at = datetime.now()
    init_db()
    scheduler = TaskScheduler()
    tasks = {}
    failed_tasks = []

    def run_step(name: str, func, *args, **kwargs):
        try:
            result = _normalize_task_result(func(*args, **kwargs))
        except Exception as e:
            logger.exception("Scheduled subtask failed: %s", name)
            result = {"success": False, "error": str(e)}
        tasks[name] = result
        if not result.get("success", True):
            failed_tasks.append(name)
        return result

    sector_flow_result = run_step("sector_flow", scheduler._run_sector_flow, trigger="render_cron")
    try:
        run_step("fund_update", scheduler._run_fund_update)
        run_step("prediction_verify", scheduler._run_prediction_verify)
        run_step("expired_verify", scheduler._run_expired_verify)
        return {
            "success": not failed_tasks,
            "sector_flow": sector_flow_result,
            "tasks": tasks,
            "failed_tasks": failed_tasks,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.exception("定时任务执行失败")
        return {
            "success": False,
            "sector_flow": sector_flow_result,
            "error": str(e),
            "started_at": started_at.isoformat(),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 Fund Insight 一次性定时任务")
    parser.add_argument("job", choices=["daily"], help="任务类型")
    args = parser.parse_args()

    if args.job == "daily":
        result = run_daily_tasks()
        logger.info("定时任务结果: %s", result)
        return 0 if result.get("success") else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
