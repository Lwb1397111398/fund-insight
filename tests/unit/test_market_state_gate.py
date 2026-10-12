"""市场软闸（src/analyzer/market_state.py）判据。

钉三件事：
① 四档分类与边界值（阈值改错 ⇒ 这些用例红）；
② 数据不足/缺数据 ⇒ unknown 且 enforce=False（绝不硬拦）；
③ 宽基退避顺序（510300 缺数据 ⇒ 退 510050）。
全部为纯本地净值计算，零网络、零 LLM。
"""
from datetime import date, timedelta

from src.models.database import FundHistory
from src.analyzer import market_state as ms


def _seed_nav(db, code, navs, start=date(2026, 9, 1), name=None):
    for i, v in enumerate(navs):
        db.add(
            FundHistory(
                fund_code=code,
                fund_name=name or f"{code}测试",
                nav_date=start + timedelta(days=i),
                nav=float(v),
                day_growth=0.0,
            )
        )
    db.commit()


def test_flat_then_crash_classifies_very_weak(test_db):
    # 20日窗口从 100 跌到 93（-7%），近 5 日 96→93 仍在跌 ⇒ 极弱
    navs = [100] * 15 + [110, 96, 95, 94, 93.5, 93]
    assert len(navs) == ms.WINDOW_DAYS + 1
    _seed_nav(test_db, "510300", navs)
    got = ms.compute_market_state(test_db)
    assert got["stance"] == "very_weak"
    assert got["enforce"] is True
    assert "空仓" in got["position_label"]
    assert got["index_code"] == "510300"


def test_mild_decline_classifies_weak(test_db):
    # r20 = -4%（≤ -3 但未到极弱线），r5 也在跌
    navs = [100] * 20 + [96]
    _seed_nav(test_db, "510300", navs)
    got = ms.compute_market_state(test_db)
    assert got["stance"] == "weak"
    assert got["enforce"] is True
    assert "减仓" in got["position_label"]


def test_deep_drawdown_is_weak_even_when_r20_positive(test_db):
    # 先冲高 116 再回落到 105：r20 +5%、r5 -4.5%、高点回撤 -9.5% ⇒ 回撤线也判弱势
    navs = [100, 100, 101, 102, 103, 104, 105, 106, 108, 110,
            112, 114, 116, 114, 112, 110, 108, 106, 105, 105, 105]
    _seed_nav(test_db, "510300", navs)
    got = ms.compute_market_state(test_db)
    assert got["stance"] == "weak"
    assert got["drawdown"] <= ms.WEAK_DRAWDOWN


def test_uptrend_classifies_strong(test_db):
    navs = [100] * 20 + [105]
    _seed_nav(test_db, "510300", navs)
    got = ms.compute_market_state(test_db)
    assert got["stance"] == "strong"
    assert got["enforce"] is False
    assert "可正常买入" in got["position_label"]


def test_sideways_classifies_range(test_db):
    navs = [100] * 20 + [101]
    _seed_nav(test_db, "510300", navs)
    got = ms.compute_market_state(test_db)
    assert got["stance"] == "range"
    assert got["enforce"] is False


def test_boundary_values_are_inclusive(test_db):
    # r20 恰好 -3% ⇒ weak（阈值取"≤"，边界归弱势）
    _seed_nav(test_db, "510300", [100] * 20 + [97])
    assert ms.compute_market_state(test_db)["stance"] == "weak"


def test_insufficient_nav_rows_returns_unknown(test_db):
    # 只有 4 个净值点（< MIN_ROWS=6）⇒ unknown，不瞎分档
    _seed_nav(test_db, "510300", [100, 99, 98, 97])
    got = ms.compute_market_state(test_db)
    assert got["stance"] == "unknown"
    assert got["enforce"] is False


def test_empty_history_returns_unknown_without_crashing(test_db):
    got = ms.compute_market_state(test_db)
    assert got["stance"] == "unknown"
    assert got["index_code"] is None
    assert "闸自动失效" in got["basis"]


def test_falls_back_to_secondary_index(test_db):
    # 510300 只有 2 行（不够），510050 有完整 21 行 ⇒ 退到 510050
    _seed_nav(test_db, "510300", [100, 90])
    _seed_nav(test_db, "510050", [100] * 20 + [105])
    got = ms.compute_market_state(test_db)
    assert got["index_code"] == "510050"
    assert got["stance"] == "strong"


def test_basis_mentions_recent_moves(test_db):
    _seed_nav(test_db, "510300", [100] * 20 + [96])
    got = ms.compute_market_state(test_db)
    assert "近5日" in got["basis"]
    assert got["nav_as_of"]  # 最新净值日必须给页面展示
