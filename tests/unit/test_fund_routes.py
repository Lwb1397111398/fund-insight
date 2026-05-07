"""
测试基金API路由
验证AI趋势分析修复
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import date
import json

from src.api.routes.funds import router


class TestFundRoutes:
    """测试基金路由"""
    
    def test_imports(self):
        """测试导入是否正确"""
        from src.api.routes import funds
        
        assert hasattr(funds, 'json')
        assert hasattr(funds, 'FundInfo')
        assert hasattr(funds, 'FundHistory')
    
    @patch('src.api.routes.funds.FundInfo')
    @patch('src.api.routes.funds.FundHistory')
    def test_analyze_trends_endpoint_exists(self, mock_fund_history, mock_fund_info):
        """测试趋势分析端点存在"""
        from src.api.routes.funds import analyze_all_fund_trends
        
        assert callable(analyze_all_fund_trends)
    
    @patch('src.api.routes.funds.FundInfo')
    def test_trend_status_endpoint_exists(self, mock_fund_info):
        """测试趋势状态端点存在"""
        from src.api.routes.funds import get_trend_status
        
        assert callable(get_trend_status)
    
    def test_trend_status_success(self):
        """测试趋势状态获取成功"""
        from src.api.routes.funds import get_trend_status
        import asyncio

        mock_db = Mock()

        # 每次 db.query() 返回独立的 mock，filter() 也返回 mock，count() 返回具体值
        query_mocks = []
        for count_val in [3, 2, 1]:  # total=3, analyzed=2, today_analyzed=1
            qm = Mock()
            qm.filter.return_value = qm  # filter() 返回自身，支持链式调用
            qm.count.return_value = count_val
            query_mocks.append(qm)

        mock_db.query.side_effect = query_mocks

        result = asyncio.run(get_trend_status(mock_db))

        assert result["success"] is True
        assert result["data"]["total"] == 3
        assert result["data"]["analyzed"] == 2
        assert result["data"]["pending"] == 1
        assert result["data"]["today_analyzed"] == 1


class TestBugFix:
    """测试Bug修复"""
    
    def test_missing_imports_fixed(self):
        """测试缺失的导入已修复（BUG-003）"""
        from src.api.routes import funds
        
        assert hasattr(funds, 'json')
        assert hasattr(funds, 'FundInfo')
        assert hasattr(funds, 'FundHistory')
        
        assert callable(funds.analyze_all_fund_trends)
        assert callable(funds.get_trend_status)
    
    def test_trend_status_endpoint_added(self):
        """测试趋势状态端点已添加（BUG-004）"""
        from src.api.routes.funds import get_trend_status
        
        assert get_trend_status is not None
        assert hasattr(get_trend_status, '__call__')
