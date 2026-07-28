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


class TestFundInfoFallback:
    """fundgz 实时接口失效时，get_fund_info 应走历史净值兜底"""

    def setup_method(self):
        self.api = FundAPI()

    def test_get_fund_info_falls_back_to_history_when_realtime_returns_404_html(self):
        """实时估值接口返回 404 HTML 时，用最近一天历史净值兜底"""
        mock_resp = Mock()
        mock_resp.text = '<!doctype html><html><title>页面未找到</title></html>'
        mock_resp.encoding = 'utf-8'
        self.api.session.get = Mock(return_value=mock_resp)

        with patch.object(self.api, 'get_fund_history', return_value=[
            {'date': date(2026, 7, 28), 'nav': 0.4254, 'growth': -0.37}
        ]) as mock_hist:
            result = self.api.get_fund_info('159883')

        mock_hist.assert_called_once_with('159883', days=1)
        assert result is not None
        assert result['fund_code'] == '159883'
        assert result['nav'] == 0.4254
        assert result['nav_date'] == '2026-07-28'
        assert result['day_growth'] == -0.37
        # 兜底数据不应伪造名称/类型，避免覆盖库内已有字段
        assert result['fund_name'] is None
        assert result['fund_type'] is None

    def test_get_fund_info_falls_back_to_history_on_timeout(self):
        """实时接口超时/网络异常时，同样应尝试历史净值兜底"""
        import requests
        self.api.session.get = Mock(side_effect=requests.exceptions.Timeout('timeout'))

        with patch.object(self.api, 'get_fund_history', return_value=[
            {'date': date(2026, 7, 27), 'nav': 1.23, 'growth': 1.5}
        ]):
            result = self.api.get_fund_info('000001')

        assert result is not None
        assert result['nav'] == 1.23
        assert result['nav_date'] == '2026-07-27'
        assert result['day_growth'] == 1.5

    def test_get_fund_info_returns_none_when_realtime_and_history_both_fail(self):
        mock_resp = Mock()
        mock_resp.text = '<html>not found</html>'
        mock_resp.encoding = 'utf-8'
        self.api.session.get = Mock(return_value=mock_resp)

        with patch.object(self.api, 'get_fund_history', return_value=[]):
            assert self.api.get_fund_info('999999') is None


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

    def test_update_all_funds_info_succeeds_via_history_fallback(self):
        """get_fund_info 走历史兜底时，update_all_funds_info 应成功更新净值"""
        from src.fund.fund_sync_manager import FundSyncManager

        manager = FundSyncManager()
        mock_db = Mock()
        mock_fund = Mock()
        mock_fund.fund_code = '159883'
        mock_fund.fund_name = '医疗ETF'
        mock_fund.latest_nav = 0.43
        mock_fund.day_growth = 0.0
        mock_fund.nav_date = date(2026, 7, 22)
        mock_db.query.return_value.all.return_value = [mock_fund]

        with patch('src.fund.fund_sync_manager.fund_api') as mock_api:
            mock_api.get_fund_info.return_value = {
                'fund_code': '159883',
                'fund_name': None,
                'fund_type': None,
                'nav': 0.4254,
                'nav_date': '2026-07-28',
                'day_growth': -0.37,
            }
            mock_api.get_fund_history.return_value = [
                {'date': date(2026, 7, 28), 'nav': 0.4254, 'growth': -0.37}
            ]
            result = manager.update_all_funds_info(mock_db)

        assert result['updated'] == 1
        assert result['failed'] == 0
        assert mock_fund.latest_nav == 0.4254
        assert mock_fund.nav_date == date(2026, 7, 28)
        assert mock_fund.day_growth == -0.37
