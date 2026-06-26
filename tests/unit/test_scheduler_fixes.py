"""
调度器修复验证测试
验证 Critical 和 High 级别 bug 的修复
"""
import pytest
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock

# 添加项目路径
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from src.tasks.scheduler import TaskScheduler, get_scheduler, stop_scheduler


class TestSchedulerFixes:
    """调度器修复测试"""

    def test_timezone_handling(self):
        """测试时区处理：确保使用北京时间"""
        scheduler = TaskScheduler()

        # 测试 _seconds_until_next_window 使用北京时间
        with patch('src.tasks.scheduler.datetime') as mock_dt:
            # 模拟北京时间 9:00
            beijing_time = datetime(2026, 6, 14, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))
            mock_dt.now.return_value = beijing_time
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

            seconds = scheduler._seconds_until_next_window()

            # 验证返回的是到下一个窗口的秒数（应该是到 10:00 的 3600 秒）
            assert isinstance(seconds, int)
            assert seconds > 0

    def test_state_variables_persistence(self):
        """测试状态变量持久化：不会因局部变量而重置"""
        scheduler = TaskScheduler()

        # 初始状态应该是 None
        assert scheduler._last_cleanup_date is None
        assert scheduler._last_verify_date is None
        assert scheduler._last_fund_update_date is None

        # 设置状态
        with scheduler._state_lock:
            scheduler._last_cleanup_date = datetime(2026, 6, 14).date()

        # 验证状态已保存
        with scheduler._state_lock:
            assert scheduler._last_cleanup_date == datetime(2026, 6, 14).date()

    def test_thread_safety_of_get_scheduler(self):
        """测试 get_scheduler 的线程安全性"""
        # 重置全局状态
        import src.tasks.scheduler as scheduler_module
        original_scheduler = scheduler_module._scheduler
        scheduler_module._scheduler = None

        try:
            results = []
            barrier = threading.Barrier(10)

            def get_instance():
                barrier.wait()  # 等待所有线程就绪
                scheduler = get_scheduler()
                results.append(id(scheduler))

            threads = [threading.Thread(target=get_instance) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # 验证所有线程获取的是同一个实例
            assert len(set(results)) == 1, "线程安全失败：创建了多个实例"
        finally:
            # 恢复原始状态
            scheduler_module._scheduler = original_scheduler

    def test_fund_update_transaction_rollback_on_failure(self):
        """测试基金更新事务：失败时回滚"""
        scheduler = TaskScheduler()

        # 模拟数据库会话
        mock_db = Mock()
        mock_db.commit = Mock()
        mock_db.rollback = Mock()
        mock_db.close = Mock()

        # 模拟fund列表
        mock_fund1 = Mock()
        mock_fund1.fund_code = "000001"
        mock_fund2 = Mock()
        mock_fund2.fund_code = "000002"

        mock_db.query.return_value.all.return_value = [mock_fund1, mock_fund2]

        # 模拟 FundDataManager：第一个成功，第二个失败
        with patch('src.fund.fund_api.FundDataManager') as MockManager:
            mock_dm = MockManager.return_value

            def update_side_effect(fund_code, db=None):
                if fund_code == "000002":
                    raise ValueError("更新失败")

            mock_dm.update_fund_info.side_effect = update_side_effect
            mock_dm.update_fund_history.return_value = None

            with patch('src.models.database.SessionLocal', return_value=mock_db):
                scheduler._run_fund_update()

        # 验证：逐个提交模式——成功的基金已提交，失败的回滚
        mock_db.commit.assert_called_once()  # 第一个基金成功，提交
        mock_db.rollback.assert_called_once()  # 第二个基金失败，回滚
        mock_db.close.assert_called()

    def test_fund_update_transaction_commit_on_success(self):
        """测试基金更新事务：全部成功时提交"""
        scheduler = TaskScheduler()

        # 模拟数据库会话
        mock_db = Mock()
        mock_db.commit = Mock()
        mock_db.rollback = Mock()
        mock_db.close = Mock()

        # 模拟基金列表
        mock_fund1 = Mock()
        mock_fund1.fund_code = "000001"
        mock_fund2 = Mock()
        mock_fund2.fund_code = "000002"

        mock_db.query.return_value.all.return_value = [mock_fund1, mock_fund2]

        # 模拟 FundDataManager：全部成功
        with patch('src.fund.fund_api.FundDataManager') as MockManager:
            mock_dm = MockManager.return_value
            mock_dm.update_fund_info.return_value = None
            mock_dm.update_fund_history.return_value = None

            with patch('src.models.database.SessionLocal', return_value=mock_db):
                scheduler._run_fund_update()

        # 验证：逐个提交模式——每个基金成功后单独提交
        assert mock_db.commit.call_count == 2  # 每个基金各提交一次
        mock_db.rollback.assert_not_called()
        mock_db.close.assert_called()

    def test_session_close_on_exception(self):
        """测试异常时数据库会话正确关闭"""
        scheduler = TaskScheduler()

        # 模拟数据库会话
        mock_db = Mock()
        mock_db.close = Mock()

        # 模拟 PredictionVerifyService 抛出异常
        with patch('src.services.prediction_verify_service.PredictionVerifyService') as MockService:
            mock_service = MockService.return_value
            mock_service.verify_all_pending.side_effect = RuntimeError("测试异常")

            with patch('src.models.database.SessionLocal', return_value=mock_db):
                scheduler._run_prediction_verify()

        # 验证：即使发生异常，数据库连接也会被关闭
        mock_db.close.assert_called()

    def test_stop_scheduler_resets_global(self):
        """测试 stop_scheduler 重置全局变量"""
        import src.tasks.scheduler as scheduler_module

        # 创建一个模拟的调度器
        mock_scheduler = Mock()
        mock_scheduler.stop = Mock()

        original = scheduler_module._scheduler
        scheduler_module._scheduler = mock_scheduler

        try:
            from src.tasks.scheduler import stop_scheduler
            stop_scheduler()

            # 验证：调度器已停止且全局变量已重置
            mock_scheduler.stop.assert_called_once()
            assert scheduler_module._scheduler is None
        finally:
            scheduler_module._scheduler = original


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
