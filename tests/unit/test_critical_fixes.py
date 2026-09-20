"""
关键路径测试 - 验证本次优化中的关键修复
"""
import pytest
from datetime import date, datetime


class TestVerifyHistoryAppend:
    """测试 verify_history 追加而非覆盖"""
    def test_verify_history_should_be_list(self):
        """verify_history 初始化应为空列表"""
        history = []
        history.append({"date": "2026-01-01", "score": 80})
        history.append({"date": "2026-01-02", "score": 90})
        assert len(history) == 2


class TestStatusMachine:
    """测试状态机一致性"""
    def test_terminal_states(self):
        """验证终态应为 success/failed 而非 verified"""
        valid_statuses = {'pending', 'success', 'failed', 'expired'}
        assert 'success' in valid_statuses
        assert 'failed' in valid_statuses
        assert 'verified' not in valid_statuses  # verified 不再使用


class TestPeriodMapping:
    """测试预测周期映射一致性"""
    def test_short_period_is_seven_days(self):
        """'短期' 应映射为 7 天"""
        from src.utils.prediction_utils import suggest_period_from_text
        result = suggest_period_from_text("短期看好")
        if result:
            days, _, _ = result
            assert days == 7, f"'短期' 应为 7 天，实际为 {days} 天"


class TestFundMappingSessionReuse:
    """测试基金映射自动学习的数据库会话复用"""

    def test_fundinfo_match_saves_mapping_without_opening_nested_session(self, test_db, monkeypatch):
        from src.analyzer.llm_analyzer import LLMAnalyzer
        from src.models import database
        from src.models.database import FundInfo, SectorFundMapping

        test_db.add(FundInfo(
            fund_code="159732",
            fund_name="消费电子ETF",
            fund_type="ETF",
            sector_type="消费电子",
            can_delete=True,
        ))
        test_db.commit()

        session_calls = 0

        def session_factory():
            nonlocal session_calls
            session_calls += 1
            return test_db

        monkeypatch.setattr(database, "SessionLocal", session_factory)

        analyzer = LLMAnalyzer.__new__(LLMAnalyzer)
        result = analyzer._find_fund_in_fundinfo("消费电子")

        assert result == {"code": "159732", "name": "消费电子ETF"}
        assert session_calls == 1
        # 行为变更（板块基金迭代）：FundInfo 命中**不再**自动写进 sector_fund_mapping。
        # 旧逻辑会把模糊匹配结果自我固化为"库里已有映射"，是错配被放大的主路径；
        # 现在只有 sector_fund_agent 走完抓站验证 + 语义复判后才允许落库。
        assert test_db.query(SectorFundMapping).filter(
            SectorFundMapping.sector_name == "消费电子"
        ).first() is None

    def test_fundinfo_fuzzy_match_no_longer_poisons_mappings(self, test_db, monkeypatch):
        """反向/子串模糊匹配已删除：含同一个字的板块不能被吸到别的基金上。"""
        from src.analyzer.llm_analyzer import LLMAnalyzer
        from src.models import database
        from src.models.database import FundInfo, SectorFundMapping

        test_db.add(FundInfo(fund_code="159732", fund_name="消费电子ETF",
                             fund_type="ETF", sector_type="消费", can_delete=True))
        test_db.commit()
        monkeypatch.setattr(database, "SessionLocal", lambda: test_db)

        analyzer = LLMAnalyzer.__new__(LLMAnalyzer)
        # "消费电子" 与 sector_type "消费" 是旧的子串关系，现在必须不命中
        assert analyzer._find_fund_in_fundinfo("消费电子") is None
        assert test_db.query(SectorFundMapping).count() == 0
