"""
测试多源基金数据API
测试修复的空异常捕获问题
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, date
import logging

from src.fund.multi_source_api import EastMoneyAPI, TonghuashunAPI, MultiSourceFundAPI, FundDataValidator


class TestEastMoneyAPI:
    """测试东方财富API"""
    
    def setup_method(self):
        self.api = EastMoneyAPI()
    
    @patch('src.fund.multi_source_api.requests.get')
    def test_get_nav_success(self, mock_get):
        """测试成功获取净值"""
        mock_response = Mock()
        mock_response.text = 'jsonpgz({"gsz":"1.234","gztime":"2026-03-08 15:00","gszzl":"2.5"})'
        mock_response.encoding = 'utf-8'
        mock_get.return_value = mock_response
        
        result = self.api.get_nav('161725')
        
        assert result is not None
        assert result['nav'] == 1.234
        assert result['day_growth'] == 2.5
        assert result['source'] == 'eastmoney'
    
    @patch('src.fund.multi_source_api.requests.get')
    def test_get_nav_failure(self, mock_get):
        """测试获取净值失败"""
        mock_get.side_effect = Exception("Network error")
        
        result = self.api.get_nav('161725')
        
        assert result is None
    
    @patch('src.fund.multi_source_api.requests.get')
    def test_get_history_with_invalid_data(self, mock_get):
        """测试历史数据解析时的异常处理（修复BUG-001）"""
        mock_response = Mock()
        mock_response.json.return_value = {
            'Data': {
                'LSJZList': [
                    {'FSRQ': '2026-03-08', 'DWJZ': '1.234', 'JZZZL': '2.5'},
                    {'FSRQ': 'invalid-date', 'DWJZ': 'invalid', 'JZZZL': 'invalid'},
                    {'FSRQ': '2026-03-06', 'DWJZ': '1.200', 'JZZZL': '1.5'},
                ]
            }
        }
        mock_response.encoding = 'utf-8'
        mock_get.return_value = mock_response
        
        result = self.api.get_history('161725', days=3)
        
        assert len(result) == 2
        assert result[0]['date'] == date(2026, 3, 8)
        assert result[1]['date'] == date(2026, 3, 6)


class TestTonghuashunAPI:
    """测试同花顺API"""
    
    def setup_method(self):
        self.api = TonghuashunAPI()
    
    @patch('src.fund.multi_source_api.requests.get')
    def test_get_history_with_invalid_data(self, mock_get):
        """测试历史数据解析时的异常处理（修复BUG-002）"""
        mock_response = Mock()
        mock_response.text = '''
        <tr>2026-03-08</tr>
        <tr>invalid-date</tr>
        <tr>2026-03-06</tr>
        '''
        mock_response.encoding = 'utf-8'
        mock_get.return_value = mock_response
        
        result = self.api.get_history('161725', days=3)
        
        assert isinstance(result, list)


class TestMultiSourceFundAPI:
    """测试多源基金数据API"""
    
    def setup_method(self):
        self.api = MultiSourceFundAPI()
    
    @patch.object(EastMoneyAPI, 'get_nav')
    def test_get_nav_with_validation_primary_success(self, mock_get_nav):
        """测试主数据源成功"""
        mock_get_nav.return_value = {
            'nav': 1.234,
            'nav_date': '2026-03-08',
            'day_growth': 2.5,
            'is_estimated': True,
            'source': 'eastmoney'
        }
        
        result = self.api.get_nav_with_validation('161725')
        
        assert result['nav'] == 1.234
        assert result['quality'] == 'normal'
        assert result['validated'] is False
    
    @patch.object(EastMoneyAPI, 'get_nav')
    @patch.object(TonghuashunAPI, 'get_nav')
    def test_get_nav_with_validation_backup_success(self, mock_tonghuashun, mock_eastmoney):
        """测试备用数据源成功"""
        mock_eastmoney.return_value = None
        mock_tonghuashun.return_value = {
            'nav': 1.234,
            'nav_date': '2026-03-08',
            'day_growth': 2.5,
            'is_estimated': False,
            'source': 'tonghuashun'
        }
        
        result = self.api.get_nav_with_validation('161725')
        
        assert result['nav'] == 1.234
        assert result['quality'] == 'normal'
    
    @patch.object(EastMoneyAPI, 'get_nav')
    @patch.object(TonghuashunAPI, 'get_nav')
    def test_get_nav_with_validation_all_failed(self, mock_tonghuashun, mock_eastmoney):
        """测试所有数据源失败"""
        mock_eastmoney.return_value = None
        mock_tonghuashun.return_value = None
        
        result = self.api.get_nav_with_validation('161725')
        
        assert result['nav'] is None
        assert result['quality'] == 'error'


class TestFundDataValidator:
    """测试基金数据验证器"""
    
    def setup_method(self):
        self.validator = FundDataValidator()
    
    def test_validate_nav_change_normal(self):
        """测试正常净值变化"""
        result = self.validator.validate_nav_change(1.0, 1.02)
        
        assert result['is_valid'] is True
        assert result['is_anomaly'] is False
        assert abs(result['change_pct'] - 2.0) < 0.01
    
    def test_validate_nav_change_anomaly(self):
        """测试异常净值变化"""
        result = self.validator.validate_nav_change(1.0, 1.15)
        
        assert result['is_valid'] is True
        assert result['is_anomaly'] is True
        assert abs(result['change_pct'] - 15.0) < 0.01
    
    def test_validate_nav_change_zero_prev(self):
        """测试前一日净值为零"""
        result = self.validator.validate_nav_change(0, 1.0)
        
        assert result['is_valid'] is False
        assert result['is_anomaly'] is True
    
    def test_validate_nav_change_none_prev(self):
        """测试前一日净值为None"""
        result = self.validator.validate_nav_change(None, 1.0)
        
        assert result['is_valid'] is False
        assert result['is_anomaly'] is True
    
    def test_validate_history_data_good(self):
        """测试良好历史数据"""
        history = [
            {'date': date(2026, 3, 8), 'nav': 1.02},
            {'date': date(2026, 3, 7), 'nav': 1.01},
            {'date': date(2026, 3, 6), 'nav': 1.00},
        ]
        
        result = self.validator.validate_history_data(history)
        
        assert result['quality'] in ['good', 'warning']
        assert result['anomaly_count'] == 0
    
    def test_validate_history_data_empty(self):
        """测试空历史数据"""
        result = self.validator.validate_history_data([])
        
        assert result['quality'] == 'error'
        assert result['anomaly_count'] == 0


class TestBugFix:
    """测试Bug修复"""
    
    @patch('src.fund.multi_source_api.requests.get')
    def test_empty_exception_catch_fixed(self, mock_get):
        """测试空异常捕获已修复（BUG-001, BUG-002）"""
        api = EastMoneyAPI()
        
        mock_response = Mock()
        mock_response.json.return_value = {
            'Data': {
                'LSJZList': [
                    {'FSRQ': 'invalid', 'DWJZ': 'invalid', 'JZZZL': 'invalid'},
                ]
            }
        }
        mock_response.encoding = 'utf-8'
        mock_get.return_value = mock_response
        
        result = api.get_history('161725', days=1)
        
        assert isinstance(result, list)
        assert len(result) == 0
