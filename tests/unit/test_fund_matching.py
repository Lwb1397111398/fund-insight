"""基金匹配第0级快速路径：命中入库基金零网络，未命中原样降级。"""
from types import SimpleNamespace

import pytest

from src.models.database import FundInfo
from src.utils.fund_matching import match_fund_with_fallback


class _ExplodingManager:
    """一旦走到第1级 auto_add（= 发起外网验证）就让测试失败。"""

    def auto_add_fund_for_prediction(self, sector, db):
        raise RuntimeError("不应触达第1级（会发起外网 HTTP）")

    def get_category_for_sector(self, sector):
        return "其他"


class _RecordingManager:
    def __init__(self, fund_code="888888", fund_name="自动添加基金"):
        self.calls = []
        self._fund = SimpleNamespace(fund_code=fund_code, fund_name=fund_name)

    def auto_add_fund_for_prediction(self, sector, db):
        self.calls.append(sector)
        return True, "ok", self._fund

    def get_category_for_sector(self, sector):
        return "其他"


class _FakeAnalyzer:
    def get_fund_for_sector(self, sector):
        return None


def _add_fund(db, code, name):
    db.add(FundInfo(fund_code=code, fund_name=name, sector_type="测试", latest_nav=1.0, day_growth=0.1))
    db.commit()


def test_fast_path_returns_db_fund_without_touching_auto_manager(test_db):
    # 白酒 → 161725（硬编码映射表），且该基金已入库
    _add_fund(test_db, "161725", "招商中证白酒指数 (LOF)A")

    code, name = match_fund_with_fallback(
        pred={"sector": "白酒"},
        sector="白酒",
        fund_auto_manager=_ExplodingManager(),
        llm_analyzer=_FakeAnalyzer(),
        db=test_db,
    )

    assert code == "161725"
    assert name == "招商中证白酒指数 (LOF)A"


def test_fast_path_alias_sector_also_hits(test_db):
    _add_fund(test_db, "161725", "招商中证白酒指数 (LOF)A")

    code, _ = match_fund_with_fallback(
        pred={}, sector="茅台",  # 黑话别名 → 白酒
        fund_auto_manager=_ExplodingManager(),
        llm_analyzer=_FakeAnalyzer(),
        db=test_db,
    )
    assert code == "161725"


def test_fast_path_falls_through_when_fund_not_in_db(test_db):
    manager = _RecordingManager()
    code, name = match_fund_with_fallback(
        pred={}, sector="白酒",
        fund_auto_manager=manager,
        llm_analyzer=_FakeAnalyzer(),
        db=test_db,
    )
    # 库里没有 161725 → 交给第1级自动添加入库（保留冷启动语义）
    assert manager.calls == ["白酒"]
    assert (code, name) == ("888888", "自动添加基金")


def test_fast_path_skips_bond_fund_even_if_code_matches(test_db):
    # 极端防御：同号基金名称含"债"时不快速返回，交给后续级别
    _add_fund(test_db, "161725", "某某债券基金")
    manager = _RecordingManager()

    code, _ = match_fund_with_fallback(
        pred={}, sector="白酒",
        fund_auto_manager=manager,
        llm_analyzer=_FakeAnalyzer(),
        db=test_db,
    )
    assert manager.calls == ["白酒"]
    assert code == "888888"


def test_unmapped_sector_goes_straight_to_auto_manager(test_db):
    manager = _RecordingManager()
    code, _ = match_fund_with_fallback(
        pred={}, sector="不存在的板块XYZ",
        fund_auto_manager=manager,
        llm_analyzer=_FakeAnalyzer(),
        db=test_db,
    )
    assert manager.calls == ["不存在的板块XYZ"]
    assert code == "888888"
