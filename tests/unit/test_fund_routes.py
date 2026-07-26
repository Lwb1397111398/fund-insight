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
    


class TestBugFix:
    """测试Bug修复"""
    
    def test_missing_imports_fixed(self):
        """测试缺失的导入已修复（BUG-003）"""
        from src.api.routes import funds

        assert hasattr(funds, 'json')
        assert hasattr(funds, 'FundInfo')
        assert hasattr(funds, 'FundHistory')
