# -*- coding: utf-8 -*-
"""零网络护栏自身的用例（第 17 轮 MINOR-1）。

夹具 `_block_real_http` 是"单测不许打真接口"这条承诺的唯一落点 —— 承诺本身如果失效，
最坏的情况正是它要防的那种：用例全绿、结论却取决于第三方实时数据。
所以护栏自己也得有一条会红的用例：真去请求一个域名，必须被挡下来。

`example.invalid` 是保留的不可解析域名：万一夹具被删，这里会以
"ConnectionError 而非 AssertionError / DID NOT RAISE"失败，同样报红。
"""
import pytest


def test_requests_leg_is_blocked():
    import requests

    with pytest.raises(AssertionError, match='测试禁止真实外呼'):
        requests.get('https://example.invalid/ping', timeout=1)


def test_httpx_leg_is_blocked():
    """LLM 那一腿走 `openai → httpx`，只掐 requests 等于承诺兑现了一半。"""
    httpx = pytest.importorskip('httpx')

    with pytest.raises(AssertionError, match='测试禁止真实外呼'):
        httpx.get('https://example.invalid/ping', timeout=1)


def test_the_guard_does_not_break_offline_stubs():
    """护栏只管真外呼：模块级桩（`backfill_history_range` 之类）照样能用。"""
    from src.fund.fund_api import FundDataManager

    class _Api:
        def get_fund_history_range(self, code, start, end):
            return []

    m = FundDataManager.__new__(FundDataManager)
    m.api = _Api()
    assert m.api.get_fund_history_range('510300', None, None) == []
