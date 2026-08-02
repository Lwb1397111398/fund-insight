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

    def test_single_verify_calls_history_once(self):
        """单次验证只调一次历史接口，且实时接口走 allow_fallback=False，避免重复请求被限流"""
        api = _make_api()
        with patch.object(api, 'get_fund_info', return_value=None) as mock_info, \
             patch.object(api, 'get_fund_history', return_value=[{'date': '2026-07-31', 'nav': 1.0}]) as mock_hist:
            result = api.verify_fund_fetchable('512480')
        assert result['ok'] is True
        assert result['history_count'] == 1
        assert result['nav_date'] == '2026-07-31'  # 实时无日期时用历史最新兜底
        assert mock_hist.call_count == 1
        assert mock_info.call_args.kwargs.get('allow_fallback') is False

    def test_batch_dedup_and_problems(self):
        """批量验证：相同代码只请求一次，正确汇总问题基金"""
        api = _make_api()
        calls = {'info': 0, 'hist': 0}

        def fake_info(code, allow_fallback=True):
            calls['info'] += 1
            if code == '512480':
                return {'fund_code': code, 'fund_name': '半导体ETF', 'nav': 1.0, 'nav_date': '2026-07-31'}
            return None

        def fake_hist(code, days=7):
            calls['hist'] += 1
            return [{'date': '2026-07-31', 'nav': 1.0}] if code == '512480' else []

        with patch.object(api, 'get_fund_info', side_effect=fake_info), \
             patch.object(api, 'get_fund_history', side_effect=fake_hist):
            items = [
                {'sector_name': '半导体', 'fund_code': '512480', 'fund_name': '半导体ETF'},
                {'sector_name': '芯片', 'fund_code': '512480', 'fund_name': '芯片ETF'},
                {'sector_name': '幽灵板块', 'fund_code': '999999', 'fund_name': '不存在'},
            ]
            result = api.verify_funds_batch(items, delay=0)

        assert result['total'] == 3
        assert result['checked_codes'] == 2  # 512480、999999 各一次
        assert calls['info'] == 2
        assert calls['hist'] == 2
        assert result['ok_count'] == 2
        assert result['problem_count'] == 1
        assert result['problems'][0]['fund_code'] == '999999'
        assert result['problems'][0]['sector_name'] == '幽灵板块'
        assert result['problems'][0]['ok'] is False

    def test_batch_empty(self):
        api = _make_api()
        result = api.verify_funds_batch([], delay=0)
        assert result['total'] == 0
        assert result['problem_count'] == 0
        assert result['results'] == []
        assert result['problems'] == []
