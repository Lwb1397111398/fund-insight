import inspect

from src.fund.fund_sync_manager import FundSyncManager


def test_sync_missing_funds_builds_existing_fund_code_map():
    """缺失基金同步应批量预加载基金代码，避免循环内逐条查询"""
    source = inspect.getsource(FundSyncManager.sync_missing_funds)

    assert "existing_fund_codes" in source
    assert "FundInfo.fund_code == pred.fund_code" not in source
