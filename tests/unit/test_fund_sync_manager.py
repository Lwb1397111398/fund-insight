"""
测试基金同步管理器
验证datetime导入修复
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, date

from src.fund.fund_sync_manager import FundSyncManager


class TestFundSyncManager:
    """测试基金同步管理器"""
    
    def setup_method(self):
        self.manager = FundSyncManager()
    
    def test_datetime_import(self):
        """测试datetime导入是否正确"""
        from src.fund import fund_sync_manager
        
        assert hasattr(fund_sync_manager, 'datetime')
        assert hasattr(fund_sync_manager, 'date')
    
    @patch('src.fund.fund_sync_manager.fund_api')
    def test_update_all_funds_info_success(self, mock_fund_api):
        """测试更新所有基金信息成功"""
        mock_db = Mock()
        mock_fund1 = Mock()
        mock_fund1.fund_code = '161725'
        mock_fund1.fund_name = '招商中证白酒指数'
        mock_fund1.latest_nav = 1.0
        mock_fund1.day_growth = 0.0
        mock_fund1.week_growth = 0.0
        mock_fund1.month_growth = 0.0
        
        mock_fund2 = Mock()
        mock_fund2.fund_code = '110022'
        mock_fund2.fund_name = '易方达消费行业'
        mock_fund2.latest_nav = 1.0
        mock_fund2.day_growth = 0.0
        mock_fund2.week_growth = 0.0
        mock_fund2.month_growth = 0.0
        
        mock_db.query.return_value.all.return_value = [mock_fund1, mock_fund2]
        
        mock_fund_api.get_fund_info.side_effect = [
            {
                'nav': 1.234,
                'day_growth': 2.5,
                'week_growth': 5.0,
                'month_growth': 10.0
            },
            {
                'nav': 2.345,
                'day_growth': 1.5,
                'week_growth': 3.0,
                'month_growth': 8.0
            }
        ]
        
        result = self.manager.update_all_funds_info(mock_db)
        
        assert result['total'] == 2
        assert result['updated'] == 2
        assert result['failed'] == 0
        assert len(result['details']) == 2
    
    @patch('src.fund.fund_sync_manager.fund_api')
    def test_update_all_funds_info_partial_failure(self, mock_fund_api):
        """测试部分基金更新失败"""
        mock_db = Mock()
        mock_fund1 = Mock()
        mock_fund1.fund_code = '161725'
        mock_fund1.fund_name = '招商中证白酒指数'
        mock_fund1.latest_nav = 1.0
        mock_fund1.day_growth = 0.0
        mock_fund1.week_growth = 0.0
        mock_fund1.month_growth = 0.0
        
        mock_fund2 = Mock()
        mock_fund2.fund_code = '110022'
        mock_fund2.fund_name = '易方达消费行业'
        mock_fund2.latest_nav = 1.0
        mock_fund2.day_growth = 0.0
        mock_fund2.week_growth = 0.0
        mock_fund2.month_growth = 0.0
        
        mock_db.query.return_value.all.return_value = [mock_fund1, mock_fund2]
        
        mock_fund_api.get_fund_info.side_effect = [
            {
                'nav': 1.234,
                'day_growth': 2.5,
                'week_growth': 5.0,
                'month_growth': 10.0
            },
            None
        ]
        
        result = self.manager.update_all_funds_info(mock_db)
        
        assert result['total'] == 2
        assert result['updated'] == 1
        assert result['failed'] == 1
    
    @patch('src.fund.fund_sync_manager.fund_api')
    def test_update_all_funds_info_datetime_fix(self, mock_fund_api):
        """测试datetime.now()不再抛出NameError（BUG修复验证）"""
        mock_db = Mock()
        mock_fund = Mock()
        mock_fund.fund_code = '161725'
        mock_fund.fund_name = '测试基金'
        mock_fund.latest_nav = 1.0
        mock_fund.day_growth = 0.0
        mock_fund.week_growth = 0.0
        mock_fund.month_growth = 0.0
        
        mock_db.query.return_value.all.return_value = [mock_fund]
        
        mock_fund_api.get_fund_info.return_value = {
            'nav': 1.234,
            'day_growth': 2.5,
            'week_growth': 5.0,
            'month_growth': 10.0
        }
        
        result = self.manager.update_all_funds_info(mock_db)
        
        assert result['updated'] == 1
        assert result['failed'] == 0
        
        assert mock_fund.updated_at is not None
        assert isinstance(mock_fund.updated_at, datetime)
        assert mock_fund.nav_date is not None
        assert isinstance(mock_fund.nav_date, date)

    @patch('src.fund.fund_sync_manager.fund_api')
    def test_update_all_funds_info_keeps_existing_nav_date_when_api_date_invalid(self, mock_fund_api):
        """外部接口返回非法净值日期时，不用今天覆盖已有可信日期"""
        mock_db = Mock()
        mock_fund = Mock()
        mock_fund.fund_code = '161725'
        mock_fund.fund_name = '测试基金'
        mock_fund.latest_nav = 1.0
        mock_fund.day_growth = 0.0
        mock_fund.nav_date = date(2026, 7, 1)

        mock_db.query.return_value.all.return_value = [mock_fund]
        mock_fund_api.get_fund_info.return_value = {
            'nav': 1.234,
            'day_growth': 2.5,
            'nav_date': 'bad-date'
        }

        result = self.manager.update_all_funds_info(mock_db)

        assert result['updated'] == 1
        assert result['failed'] == 0
        assert mock_fund.nav_date == date(2026, 7, 1)
