"""
定时任务调度器
用于定期执行清理任务
"""
import threading
import time
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class TaskScheduler:
    """任务调度器"""
    
    def __init__(self):
        self.running = False
        self.thread = None
        self.cleanup_interval_hours = 24  # 每天运行一次
    
    def start(self):
        """启动调度器"""
        if self.running:
            logger.warning("调度器已在运行中")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run_scheduler, daemon=True)
        self.thread.start()
        logger.info(f"定时任务调度器已启动，清理间隔: {self.cleanup_interval_hours}小时")
    
    def stop(self):
        """停止调度器"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("定时任务调度器已停止")
    
    def _run_scheduler(self):
        """运行调度循环"""
        # 启动时先更新基金数据，再执行验证
        self._run_fund_update()
        self._run_prediction_verify()
        self._run_expired_verify()
        
        last_cleanup_date = None
        last_verify_date = None
        last_fund_update_date = None
        
        while self.running:
            try:
                now = datetime.now()
                current_date = now.date()
                
                # 每天早上10点执行预测验证
                if now.hour == 10 and now.minute == 0:
                    if last_verify_date != current_date:
                        self._run_fund_update()
                        self._run_prediction_verify()
                        self._run_expired_verify()
                        last_verify_date = current_date
                
                # 每天执行一次清理任务
                if last_cleanup_date != current_date:
                    self._run_cleanup()
                    last_cleanup_date = current_date
                
                # 每天下午3点更新基金数据（收盘后）
                if now.hour == 15 and now.minute >= 30:
                    if last_fund_update_date != current_date:
                        self._run_fund_update()
                        last_fund_update_date = current_date
                
                # 每分钟检查一次
                time.sleep(60)
                    
            except Exception as e:
                logger.error(f"调度器异常: {e}")
                time.sleep(3600)  # 异常后等待1小时再试
    
    def _run_cleanup(self):
        """执行清理任务"""
        try:
            from src.tasks.cleanup_tasks import run_cleanup_task
            
            logger.info("开始执行定时清理任务...")
            result = run_cleanup_task()
            
            if result.get("success"):
                logger.info(f"清理任务完成: 删除预测 {result['predictions'].get('deleted_predictions', 0)} 个, "
                          f"删除观点 {result['viewpoints'].get('deleted_viewpoints', 0)} 个")
            else:
                logger.error(f"清理任务失败: {result}")
                
        except Exception as e:
            logger.error(f"执行清理任务失败: {e}")
    
    def _run_prediction_verify(self):
        """执行预测验证任务"""
        try:
            from src.services.prediction_verify_service import PredictionVerifyService
            from src.models.database import SessionLocal
            
            logger.info("开始执行预测验证任务...")
            
            db = SessionLocal()
            try:
                service = PredictionVerifyService(db)
                result = service.verify_all_pending()
                
                if result.get("success"):
                    data = result.get("data", {})
                    logger.info(f"预测验证完成: 成功 {data.get('success_count', 0)} 个, 失败 {data.get('failed_count', 0)} 个")
                else:
                    logger.error(f"预测验证失败: {result}")
                    
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"执行预测验证任务失败: {e}")
    
    def _run_expired_verify(self):
        """执行已过期待验证预测的补救验证"""
        try:
            from src.services.prediction_verify_service import PredictionVerifyService
            from src.models.database import SessionLocal
            
            logger.info("开始执行补救验证任务...")
            
            db = SessionLocal()
            try:
                service = PredictionVerifyService(db)
                result = service.verify_expired_pending()
                
                if result.get("success"):
                    data = result.get("data", {})
                    logger.info(f"补救验证完成: 成功 {data.get('success_count', 0)} 个, 失败 {data.get('failed_count', 0)} 个")
                else:
                    logger.error(f"补救验证失败: {result}")
                    
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"执行补救验证任务失败: {e}")
    
    def _run_fund_update(self):
        """执行基金数据更新"""
        try:
            from src.fund.fund_api import FundDataManager
            from src.models.database import SessionLocal, FundInfo
            
            logger.info("开始更新基金数据...")
            
            db = SessionLocal()
            try:
                dm = FundDataManager()
                funds = db.query(FundInfo).all()
                updated = 0
                failed = 0
                
                for fund in funds:
                    try:
                        dm.update_fund_info(fund.fund_code, db=db)
                        dm.update_fund_history(fund.fund_code, days=30, db=db)
                        updated += 1
                    except Exception as e:
                        failed += 1
                        logger.warning(f"更新基金 {fund.fund_code} 失败: {e}")
                
                logger.info(f"基金数据更新完成: 成功 {updated} 个, 失败 {failed} 个")
                
            finally:
                db.close()
                
        except Exception as e:
            logger.error(f"执行基金数据更新失败: {e}")


# 全局调度器实例
_scheduler = None


def get_scheduler() -> TaskScheduler:
    """获取调度器实例"""
    global _scheduler
    if _scheduler is None:
        _scheduler = TaskScheduler()
    return _scheduler


def start_scheduler():
    """启动定时任务调度器"""
    scheduler = get_scheduler()
    scheduler.start()
    return scheduler


def stop_scheduler():
    """停止定时任务调度器"""
    scheduler = get_scheduler()
    scheduler.stop()


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
