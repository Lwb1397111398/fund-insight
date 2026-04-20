"""
Fund Insight - 基金博主分析系统
"""
from src.core import config
from src.analyzer import get_analyzer
from src.fund import fund_api, fund_data_manager

__all__ = ['config', 'get_analyzer', 'fund_api', 'fund_data_manager']
