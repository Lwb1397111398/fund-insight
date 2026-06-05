"""
测试基金历史净值获取修复
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, date

from src.fund.fund_api import FundAPI, FundDataManager


class TestFundHistoryFix:
    """测试基金历史净值获取修复"""
    
    def setup_method(self):
        self.api = FundAPI()
        self.manager = FundDataManager()
    
    def _make_mock_response(self, json_data):
        """创建模拟响应对象"""
        mock_response = Mock()
        mock_response.json.return_value = json_data
        mock_response.encoding = 'utf-8'
        return mock_response

    def test_get_fund_history_with_data(self):
        """测试获取基金历史净值（有数据）"""
        mock_resp = self._make_mock_response({
            'Data': {
                'LSJZList': [
                    {'FSRQ': '2026-03-08', 'DWJZ': '1.234', 'JZZZL': '2.5'},
                    {'FSRQ': '2026-03-07', 'DWJZ': '1.204', 'JZZZL': '1.5'},
                    {'FSRQ': '2026-03-06', 'DWJZ': '1.186', 'JZZZL': '0.8'},
                ]
            }
        })
        self.api.session.get = Mock(return_value=mock_resp)

        result = self.api.get_fund_history('004752', days=30)

        assert len(result) == 3
        assert result[0]['date'] == date(2026, 3, 8)
        assert result[0]['nav'] == 1.234
        assert result[0]['growth'] == 2.5

    def test_get_fund_history_empty_list(self):
        """测试获取基金历史净值（空列表）"""
        mock_resp = self._make_mock_response({
            'Data': {'LSJZList': []}
        })
        self.api.session.get = Mock(return_value=mock_resp)

        result = self.api.get_fund_history('004752', days=30)

        assert len(result) == 0

    def test_get_fund_history_no_data(self):
        """测试获取基金历史净值（无数据字段）"""
        mock_resp = self._make_mock_response({})
        self.api.session.get = Mock(return_value=mock_resp)

        result = self.api.get_fund_history('004752', days=30)

        assert len(result) == 0

    def test_get_fund_history_invalid_data(self):
        """测试获取基金历史净值（无效数据）"""
        mock_resp = self._make_mock_response({
            'Data': {
                'LSJZList': [
                    {'FSRQ': 'invalid-date', 'DWJZ': 'invalid', 'JZZZL': 'invalid'},
                    {'FSRQ': '2026-03-07', 'DWJZ': '1.204', 'JZZZL': '1.5'},
                ]
            }
        })
        self.api.session.get = Mock(return_value=mock_resp)

        result = self.api.get_fund_history('004752', days=30)

        assert len(result) == 1
        assert result[0]['date'] == date(2026, 3, 7)


class TestFundSyncFix:
    """测试基金同步修复"""
    
    def test_update_all_funds_info_uses_actual_nav_date(self):
        """测试更新基金信息时使用实际净值日期"""
        from src.fund.fund_sync_manager import FundSyncManager

        manager = FundSyncManager()

        assert hasattr(manager, 'update_all_funds_info')

        import inspect
        source = inspect.getsource(manager.update_all_funds_info)

        # 验证使用 fund_api 获取基金信息
        assert 'fund_api.get_fund_info' in source
        # 验证使用实际净值日期而不是 date.today()
        assert 'nav_date' in source
