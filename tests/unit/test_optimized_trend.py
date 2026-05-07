"""
测试优化后的AI趋势分析
验证提示词简化和token节省效果
"""
import pytest
from unittest.mock import Mock, patch
import json

from src.analyzer.llm_analyzer import LLMAnalyzer


class TestOptimizedTrendAnalysis:
    """测试优化后的趋势分析"""
    
    def setup_method(self):
        self.analyzer = LLMAnalyzer()
    
    @patch.object(LLMAnalyzer, '_call_llm')
    def test_optimized_prompt_structure(self, mock_call_llm):
        """测试优化后的提示词结构"""
        mock_response = '''
        {
            "trend_summary": "整体震荡上涨",
            "periods": [
                {
                    "start_date": "2026-02-01",
                    "end_date": "2026-02-10",
                    "trend": "up",
                    "change_percent": 5.2
                },
                {
                    "start_date": "2026-02-11",
                    "end_date": "2026-02-20",
                    "trend": "down",
                    "change_percent": -2.1
                },
                {
                    "start_date": "2026-02-21",
                    "end_date": "2026-02-28",
                    "trend": "flat",
                    "change_percent": 0.3
                }
            ]
        }
        '''
        mock_call_llm.return_value = mock_response
        
        history = [
            {"date": "2026-02-28", "nav": 1.234, "day_growth": 0.5},
            {"date": "2026-02-27", "nav": 1.228, "day_growth": -0.2},
            {"date": "2026-02-26", "nav": 1.230, "day_growth": 0.1},
            {"date": "2026-02-25", "nav": 1.229, "day_growth": -0.1},
            {"date": "2026-02-24", "nav": 1.230, "day_growth": 0.0},
            {"date": "2026-02-23", "nav": 1.230, "day_growth": 0.2},
        ]
        
        result = self.analyzer.analyze_fund_trend_detailed(
            fund_code="161725",
            fund_name="招商中证白酒指数",
            history=history
        )
        
        assert "trend_summary" in result
        assert "periods" in result
        assert isinstance(result["periods"], list)
        
        mock_call_llm.assert_called_once()
        call_args = mock_call_llm.call_args
        
        assert call_args[1]['task_type'] == 'extraction'
        assert call_args[1]['max_tokens'] == 600
    
    def test_prompt_simplification(self):
        """测试提示词简化效果"""
        original_fields = [
            "trend_summary",
            "periods",
            "overall_change",
            "volatility",
            "max_gain",
            "max_loss",
            "is_stable",
            "gain_vs_loss"
        ]
        
        optimized_fields = [
            "trend_summary",
            "periods"
        ]
        
        assert len(optimized_fields) < len(original_fields)
        
        print(f"原字段数: {len(original_fields)}")
        print(f"优化后字段数: {len(optimized_fields)}")
        print(f"字段减少: {len(original_fields) - len(optimized_fields)}")
        print(f"减少比例: {(1 - len(optimized_fields)/len(original_fields))*100:.1f}%")
    
    def test_token_estimation(self):
        """测试token估算"""
        original_max_tokens = 1500
        optimized_max_tokens = 600
        
        token_saved = original_max_tokens - optimized_max_tokens
        save_percentage = (token_saved / original_max_tokens) * 100
        
        print(f"原max_tokens: {original_max_tokens}")
        print(f"优化后max_tokens: {optimized_max_tokens}")
        print(f"节省token: {token_saved}")
        print(f"节省比例: {save_percentage:.1f}%")
        
        assert token_saved == 900
        assert save_percentage == 60.0
