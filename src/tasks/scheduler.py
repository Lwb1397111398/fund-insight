"""
定时任务调度器
用于定期执行清理任务
"""
import threading
import time
from datetime import datetime, timedelta, timezone
import logging

from src.core.safety import destructive_cleanup_enabled

logger = logging.getLogger(__name__)

# 北京时区（UTC+8），天天基金数据使用北京时间
BEIJING_TZ = timezone(timedelta(hours=8))

# 调度步骤「最后一次执行日」持久化到 `system_config`（北京日字符串 YYYY-MM-DD）。
# 构造器旁那句"使用持久化状态，避免重启后重置"的注释以前是**纸面话** —— 四个字段纯内存，
# 每次重启都会把整套窗口重放一遍（白打数据源、容易撞东财限流）。第 97 批让它成真。
_STATE_KEY_PREFIX = 'scheduler.last_'
_STATE_ATTRS = {
    'cleanup': '_last_cleanup_date',
    'verify': '_last_verify_date',
    'fund_update': '_last_fund_update_date',
    'viewpoint_summary': '_last_viewpoint_summary_date',
}


class TaskScheduler:
    """任务调度器"""

    def __init__(self):
        self._running = False
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()  # 保护状态变量的独立锁
        self.thread = None
        self.cleanup_interval_hours = 24  # 每天运行一次
        # 「最后一次执行日」先留空：真正的持久化由调度线程起手调
        # `_load_persisted_dates()` 从 `system_config` 读回（第 97 批，见 _STATE_ATTRS）。
        # 构造器不碰库 —— 既有用例直接 `TaskScheduler()` 断言这些字段为 None。
        self._last_cleanup_date = None
        self._last_verify_date = None
        self._last_fund_update_date = None
        self._last_viewpoint_summary_date = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self):
        """启动调度器（线程安全）"""
        with self._lock:
            if self._running:
                logger.warning("调度器已在运行中")
                return
            self._running = True
        self.thread = threading.Thread(target=self._run_scheduler, daemon=True)
        self.thread.start()
        logger.info(f"定时任务调度器已启动，清理间隔: {self.cleanup_interval_hours}小时")

    def stop(self):
        """停止调度器"""
        with self._lock:
            self._running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("定时任务调度器已停止")

    def _seconds_until_next_window(self):
        """计算到下一个调度窗口的秒数（使用北京时间）"""
        now = datetime.now(BEIJING_TZ)
        current_minute = now.hour * 60 + now.minute
        # 调度窗口: 2:00(120), 10:00(600), 15:30(930)
        windows = [120, 600, 930]
        for w in windows:
            if current_minute < w:
                return (w - current_minute) * 60 - now.second
        return (24 * 60 - current_minute + windows[0]) * 60 - now.second
    
    def _load_persisted_dates(self):
        """从 `system_config` 读回四个「最后执行日」（北京日）。

        读失败 ⇒ 当作全部没执行过（fail-open）：宁可今天多补一轮 update+verify，
        也不要因为读库抖动就让调度线程起手就死（连 `SessionLocal()` 本身都包进来）。
        """
        from src.models.database import SessionLocal, SystemConfig

        keys = [_STATE_KEY_PREFIX + k for k in _STATE_ATTRS]
        found = {}
        try:
            db = SessionLocal()
            try:
                for row in db.query(SystemConfig).filter(
                        SystemConfig.config_key.in_(keys)).all():
                    found[row.config_key] = row.config_value
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        except Exception as e:
            logger.warning("调度器读取持久化状态失败（按当天未执行处理）: %s", e)
            found = {}

        with self._state_lock:
            for key, attr in _STATE_ATTRS.items():
                raw = found.get(_STATE_KEY_PREFIX + key)
                parsed = None
                if raw:
                    try:
                        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
                    except ValueError:
                        parsed = None  # 值坏掉 ⇒ 当作没执行过，让它自然重跑一次
                setattr(self, attr, parsed)

    def _mark_step_done(self, step_key: str, day):
        """某步骤今天跑过了：内存 + `system_config` 双写（写库失败只打日志，不抛）。

        「跑过就记」而非「成功才记」：基金更新失败几条时不记会让窗口当天反复重放，
        等于拿同一批失败代码反复捶数据源；缺的净值由验证侧的按区间补拉自愈。
        """
        from src.models.database import SessionLocal, SystemConfig

        with self._state_lock:
            setattr(self, _STATE_ATTRS[step_key], day)
        db = SessionLocal()
        try:
            cfg_key = _STATE_KEY_PREFIX + step_key
            row = db.query(SystemConfig).filter(SystemConfig.config_key == cfg_key).first()
            if row is None:
                db.add(SystemConfig(config_key=cfg_key, config_value=day.isoformat(),
                                    description='调度步骤最后执行日（北京）'))
            else:
                row.config_value = day.isoformat()
            db.commit()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("调度器持久化步骤 %s 失败（重启后可能重跑一次）: %s", step_key, e)
        finally:
            try:
                db.close()
            except Exception:
                pass

    def _ensure_verify_cycle(self, now=None):
        """「更新 → 验证」这一班：只补今天还没做完的部分。

        老板第 97 批点名的规矩在此落实：**验证永远排在全量基金更新之后**。
        - 验证今天没跑 ⇒ 更新今天没跑就先更新，再验证；
        - 验证跑过、更新没跑 ⇒ 只补更新；
        - 两班都跑过 ⇒ 一个网络包都不发（旧版重启必全量重放）。
        """
        now = now or datetime.now(BEIJING_TZ)
        today = now.date()
        with self._state_lock:
            need_verify = self._last_verify_date != today
            need_update = self._last_fund_update_date != today
        if need_verify:
            if need_update:
                self._run_fund_update()
                self._mark_step_done('fund_update', today)
            result = self._run_prediction_verify()
            self._mark_step_done('verify', today)
            return result
        if need_update:
            self._run_fund_update()
            self._mark_step_done('fund_update', today)
        return {"skipped": True, "reason": "already_ran_today"}

    def _run_scheduler(self):
        """运行调度循环"""
        # 先读回「今天各步骤跑过没有」，再按缺口补做（而不是无脑全量重放）。
        self._load_persisted_dates()
        self._ensure_verify_cycle(datetime.now(BEIJING_TZ))

        while self.running:
            try:
                now = datetime.now(BEIJING_TZ)
                current_date = now.date()
                current_minute = now.hour * 60 + now.minute  # 当前分钟数（0-1439）

                # 每天上午 10:00-10:59 执行「基金更新 → 预测验证」（只需执行一次）
                if 600 <= current_minute < 660:
                    with self._state_lock:
                        should_run = self._last_verify_date != current_date
                    if should_run:
                        self._ensure_verify_cycle(now)

                # 每天上午 11:00-11:59 执行观点每日汇总（在观点抓取分析完成后）
                if 660 <= current_minute < 720:
                    with self._state_lock:
                        should_run = self._last_viewpoint_summary_date != current_date
                    if should_run:
                        self._run_viewpoint_summary()
                        self._mark_step_done('viewpoint_summary', current_date)

                # 每天凌晨 2:00-2:59 执行清理任务
                if 120 <= current_minute < 180:
                    with self._state_lock:
                        should_run = self._last_cleanup_date != current_date
                    if should_run:
                        self._run_cleanup()
                        self._mark_step_done('cleanup', current_date)

                # 每天下午 15:30-15:59 补一次基金更新（上午没跑过才跑；
                # 当天 ETF 净值多在晚间才发布，这一班主要兜「上午没开应用」的底）
                if 930 <= current_minute < 960:
                    with self._state_lock:
                        should_run = self._last_fund_update_date != current_date
                    if should_run:
                        self._run_fund_update()
                        self._mark_step_done('fund_update', current_date)

                # 计算到下一个调度窗口的秒数，上限300秒
                sleep_seconds = min(self._seconds_until_next_window(), 300)
                time.sleep(sleep_seconds)

            except KeyboardInterrupt:
                logger.info("调度器收到中断信号，正在停止...")
                break
            except Exception as e:
                logger.error(f"调度器异常: {e}", exc_info=True)
                time.sleep(300)  # 异常后等待5分钟再试
    
    def _run_viewpoint_summary(self):
        """执行观点每日汇总任务"""
        import os
        from src.services.viewpoint_workflow_service import ViewpointWorkflowService

        if os.getenv("VIEWPOINT_AUTO_SUMMARY_ENABLED", "false").lower() not in ("1", "true", "yes", "on"):
            logger.info("观点每日汇总已禁用（VIEWPOINT_AUTO_SUMMARY_ENABLED=false）")
            return {"success": True, "skipped": True, "reason": "viewpoint_summary_disabled"}

        logger.info("开始执行观点每日汇总任务...")
        try:
            result = ViewpointWorkflowService.run_daily_summary_task()
            if result.get("success"):
                logger.info(f"观点每日汇总完成: task_id={result.get('task_id')}")
            else:
                logger.error(f"观点每日汇总失败: {result}")
            return result
        except Exception as e:
            logger.error(f"执行观点每日汇总任务失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _run_cleanup(self):
        """执行清理任务。

        旧 retention-v2 硬删已下线，本地调度只做只读预览；真删统一走
        weekly-retention cron（三桶 + 确认口令）或前端「安全清理」。
        """
        if not destructive_cleanup_enabled():
            logger.info("定时清理任务已禁用（ENABLE_DATA_CLEANUP=false）")
            return {"success": True, "skipped": True, "reason": "cleanup_disabled"}

        from src.services.retention_cleanup_service import (
            HARD_DELETE_DISABLED,
            HARD_DELETE_DISABLED_REASON,
        )

        if HARD_DELETE_DISABLED:
            # 本地调度不做删除：真删只走 weekly-retention cron 或前端确认后的三桶清理
            logger.info("旧执行器硬删已下线，本地调度跳过清理: %s", HARD_DELETE_DISABLED_REASON)
            return {
                "success": True,
                "skipped": True,
                "reason": "hard_delete_disabled",
                "message": HARD_DELETE_DISABLED_REASON,
            }

        try:
            from src.tasks.cleanup_tasks import run_cleanup_task

            logger.info("开始执行定时清理任务...")
            result = run_cleanup_task()

            if result.get("success"):
                pred_deleted = (result.get("predictions") or {}).get("deleted", 0)
                vp_deleted = (result.get("viewpoints") or {}).get("deleted", 0)
                if result.get("skipped"):
                    logger.info("清理任务已跳过（未启用或硬删已下线）")
                else:
                    logger.info(f"清理任务完成: 删除预测 {pred_deleted} 个, "
                              f"删除观点 {vp_deleted} 个")
            else:
                logger.error(f"清理任务失败: {result}")
            return result

        except Exception as e:
            logger.error(f"执行清理任务失败: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    def _run_prediction_verify(self):
        """执行预测验证任务"""
        from src.services.prediction_verify_service import PredictionVerifyService
        from src.services.prediction_verify_task import prediction_verify_task
        from src.models.database import SessionLocal
        from sqlalchemy.orm import Session

        logger.info("开始执行预测验证任务...")
        db = SessionLocal()
        task_db = db if isinstance(db, Session) else None
        task_started = False
        task_id = None
        task_result = None
        try:
            try:
                start_result = prediction_verify_task.start(total=0, db=task_db)
                if not start_result["success"]:
                    return {
                        "success": True,
                        "skipped": True,
                        "message": start_result["message"],
                        "data": start_result["data"],
                    }
                task_id = start_result["data"]["task_id"]
                task_started = True
                service = PredictionVerifyService(db)

                def on_progress(processed, success_count, failed_count, *_args):
                    prediction_verify_task.update_progress(
                        processed,
                        success_count,
                        failed_count,
                        db=task_db,
                        task_id=task_id,
                    )

                result = service.verify_all_pending(
                    progress_callback=on_progress if task_id is not None else None
                )
                task_result = result

                if result.get("success"):
                    data = result.get("data", {})
                    logger.info(f"预测验证完成: 成功 {data.get('success_count', 0)} 个, 失败 {data.get('failed_count', 0)} 个")
                else:
                    logger.error(f"预测验证失败: {result}")
                return result
            finally:
                if task_started:
                    prediction_verify_task.finish(
                        task_result or {"success": False, "message": "定时验证异常终止"},
                        db=task_db,
                        task_id=task_id,
                    )
                db.close()
        except Exception as e:
            logger.error(f"执行预测验证任务失败: {e}", exc_info=True)
            # 确保异常时也能关闭数据库连接
            try:
                db.close()
            except Exception:
                pass
            return {"success": False, "error": str(e)}
    
    def _run_expired_verify(self):
        """兼容旧调度入口；复用带持久化任务锁的统一验证任务。"""
        return self._run_prediction_verify()
    
    def _run_fund_update(self):
        """执行基金数据更新（逐个提交：成功的即时保存，失败的不影响其他）"""
        from src.fund.fund_api import FundDataManager
        from src.models.database import SessionLocal, FundInfo

        logger.info("开始更新基金数据...")
        db = SessionLocal()
        try:
            try:
                dm = FundDataManager()
                funds = db.query(FundInfo).all()
                updated = 0
                failed = 0
                failed_codes = []

                for fund in funds:
                    try:
                        updated_fund = dm.update_fund_info(fund.fund_code, db=db)
                        if updated_fund is None:
                            raise RuntimeError("无法获取基金信息")
                        dm.update_fund_history(fund.fund_code, db=db)
                        db.commit()  # 每个基金单独提交
                        updated += 1
                    except Exception as e:
                        db.rollback()  # 单个失败只回滚当前
                        db.expire_all()  # 重置 session 状态，避免 detached 对象问题
                        failed += 1
                        failed_codes.append(fund.fund_code)
                        logger.warning(f"更新基金 {fund.fund_code} 失败: {e}")

                if failed == 0:
                    logger.info(f"基金数据更新完成: 成功 {updated} 个")
                else:
                    logger.warning(f"基金数据更新完成: 成功 {updated} 个, 失败 {failed} 个（{', '.join(failed_codes[:5])}{'...' if len(failed_codes) > 5 else ''}）")
                return {
                    "success": failed == 0,
                    "updated_count": updated,
                    "failed_count": failed,
                    "failed_codes": failed_codes,
                }
            finally:
                db.close()
        except Exception as e:
            logger.error(f"执行基金数据更新失败: {e}", exc_info=True)
            try:
                db.close()
            except Exception:
                pass
            return {"success": False, "error": str(e)}


# 全局调度器实例（带线程保护）
_scheduler = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> TaskScheduler:
    """获取调度器实例（线程安全）"""
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:  # 双重检查锁定
                _scheduler = TaskScheduler()
    return _scheduler


def start_scheduler():
    """启动定时任务调度器"""
    scheduler = get_scheduler()
    scheduler.start()
    return scheduler


def stop_scheduler():
    """停止定时任务调度器"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        with _scheduler_lock:
            _scheduler = None


if __name__ == '__main__':
    # 测试调度器
    scheduler = start_scheduler()
    
    try:
        # 保持主线程运行
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_scheduler()
        print("调度器已停止")
