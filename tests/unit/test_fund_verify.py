"""
基金抓取验证（verify_fund_fetchable）单元测试

背景：板块映射里曾配置过抓取不到历史数据的基金代码，
导致预测验证永远缺数据。保存/审查前必须先过抓取验证。
"""
from unittest.mock import patch

from src.fund.fund_api import FundAPI


def _make_api() -> FundAPI:
    return FundAPI()


class TestVerifyFundFetchable:
    def test_ok_with_name(self):
        api = _make_api()
        with patch.object(api, 'get_fund_info', return_value={
            'fund_code': '512480',
            'fund_name': '半导体ETF',
            'nav': 1.234,
            'nav_date': '2026-07-31',
        }), patch.object(api, 'get_fund_history', return_value=[{'date': '2026-07-31', 'nav': 1.234}] * 5):
            result = api.verify_fund_fetchable('512480', '半导体ETF')
        assert result['ok'] is True
        assert result['api_name'] == '半导体ETF'
        assert result['api_nav'] == 1.234
        assert result['nav_date'] == '2026-07-31'
        assert result['history_count'] == 5
        assert '验证通过' in result['message']

    def test_ok_without_name_falls_back_to_history(self):
        """实时接口失效（名称 None）但历史可抓，仍应通过"""
        api = _make_api()
        with patch.object(api, 'get_fund_info', return_value={
            'fund_code': '001594',
            'fund_name': None,
            'nav': 1.8104,
            'nav_date': '2026-07-31',
        }), patch.object(api, 'get_fund_history', return_value=[{'date': '2026-07-31', 'nav': 1.8104}]):
            result = api.verify_fund_fetchable('001594', '错的名称')
        assert result['ok'] is True
        assert result['api_name'] is None
        assert result['history_count'] == 1

    def test_fail_when_no_data(self):
        """信息接口 None 且历史为空 → 抓取不到"""
        api = _make_api()
        with patch.object(api, 'get_fund_info', return_value=None), \
             patch.object(api, 'get_fund_history', return_value=[]):
            result = api.verify_fund_fetchable('999999', '不存在的基金')
        assert result['ok'] is False
        assert result['history_count'] == 0
        assert '验证失败' in result['message']

    def test_fail_when_info_empty_and_history_empty(self):
        api = _make_api()
        with patch.object(api, 'get_fund_info', return_value={
            'fund_code': '000001', 'fund_name': None, 'nav': 0, 'nav_date': None,
        }), patch.object(api, 'get_fund_history', return_value=[]):
            result = api.verify_fund_fetchable('000001')
        assert result['ok'] is False
        assert '验证失败' in result['message']

    def test_bad_code_format(self):
        api = _make_api()
        for bad in ['', '12345', '1234567', 'abc123', None]:
            result = api.verify_fund_fetchable(bad or '')
            assert result['ok'] is False
            assert '格式' in result['message']

    def test_code_stripped(self):
        api = _make_api()
        with patch.object(api, 'get_fund_info', return_value={
            'fund_code': '512480', 'fund_name': '半导体ETF', 'nav': 1.0, 'nav_date': '2026-07-31',
        }), patch.object(api, 'get_fund_history', return_value=[{'nav': 1.0}]):
            result = api.verify_fund_fetchable(' 512480 ')
        assert result['ok'] is True
        assert result['code'] == '512480'

    def test_api_exceptions_do_not_crash(self):
        """两个接口都抛异常时返回失败而不是抛出"""
        api = _make_api()
        with patch.object(api, 'get_fund_info', side_effect=RuntimeError('boom')), \
             patch.object(api, 'get_fund_history', side_effect=RuntimeError('boom')):
            result = api.verify_fund_fetchable('512480')
        assert result['ok'] is False
        assert '验证失败' in result['message']
