# -*- coding: utf-8 -*-
"""`_backfill_missing_history` 的补洞判据（S4a 第 8 轮 MINOR-7）。

第 8 轮评审推翻了我当时的结论："80 条到期预测卡在窗口内净值记录不足，是本地镜像
数据薄，不是 bug"。实际是补拉逻辑**只看库内最早一天**：起点有数据就直接返回 0，
于是"首尾都有、中间断几天"的洞永远补不上 —— 生产同样中招，Render Cron 无限重试，
而 `prediction_lifecycle` 还报"结构性不可验 0"。

改完实测（本地镜像跑一遍到期批量验证）：净值行数 10418 → 10567，
新增验证成功 56 条、due_unverified 80 → 24。这里把这个判据钉成用例。
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, FundHistory


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db, code, days):
    """按"周末不发净值"造一段净值，days 里缺哪天就没有哪天。"""
    for d in days:
        db.add(FundHistory(fund_code=code, nav_date=d, nav=1.0))
    db.commit()


@pytest.fixture
def manager(monkeypatch):
    from src.fund.fund_api import FundDataManager
    m = FundDataManager.__new__(FundDataManager)     # 不打网络、不建 API 客户端
    calls = []

    class _Api:
        def get_fund_history_range(self, code, start, end):
            calls.append((code, start, end))
            day = start
            out = []
            while day <= end:                        # 数据源给的是完整逐日序列
                if day.weekday() < 5:
                    out.append({'date': day, 'nav': 1.5, 'growth': 0.0})
                day += timedelta(days=1)
            return out

    m.api = _Api()
    return m, calls


def test_hole_in_the_middle_is_backfilled(manager):
    """起点和终点都有数据、中间断一段：以前直接返回 0，现在必须补。"""
    m, calls = manager
    db = _session()
    code = '159805'
    start, end = date(2026, 7, 1), date(2026, 7, 31)
    have = [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5
            and not (date(2026, 7, 8) <= start + timedelta(days=i) <= date(2026, 7, 24))]
    _seed(db, code, have)
    assert m.backfill_history_range(code, start, end, db=db) > 0, '断档没补上'
    assert calls, '根本没去打数据源'
    n = db.query(FundHistory).filter(FundHistory.fund_code == code).count()
    assert n > len(have)


def test_dense_window_skips_the_network(manager):
    """窗口内密度够就不该打接口（本方法原意：别为每笔验证都请求数据源）。"""
    m, calls = manager
    db = _session()
    code, start, end = '510300', date(2026, 7, 1), date(2026, 7, 31)
    have = [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5]
    _seed(db, code, have)
    assert m.backfill_history_range(code, start, end, db=db) == 0
    assert calls == []


def test_short_window_does_not_demand_impossible_density(manager):
    """第 9 轮 MAJOR-2 + 第 12 轮 MAJOR-3：短窗口不要求"物理上放不下的条数"，
    但**目标日那天没有净值行**时必须真去问一次，否则结构性归因永远拿不到凭据。

    旧行为：起点被覆盖 + 窗口 <14 天 ⇒ 直接返回 0，从不发请求 ⇒
    验证侧只能在"猜它不可验"和"每天无限重试"之间二选一。
    新行为：问一次、把结果记成凭据；第二次同样的窗口凭据盖住 ⇒ 不再打接口。
    """
    m, calls = manager
    db = _session()
    code, start, end = '512680', date(2026, 7, 10), date(2026, 7, 11)
    _seed(db, code, [date(2026, 7, 10), date(2026, 7, 9)])
    assert m.backfill_history_range(code, start, end, db=db) >= 0
    assert len(calls) == 1, '目标日缺行却没问过数据源'
    db.commit()
    assert m.backfill_history_range(code, start, end, db=db) == 0
    assert len(calls) == 1, '已经问过并留下凭据，第二次不该再打接口'


def test_short_window_with_end_nav_covered_still_skips_the_network(manager):
    """目标日自己有净值行、窗口 <14 天 ⇒ 仍然免打接口（第 9 轮那条保护不许退化）。"""
    m, calls = manager
    db = _session()
    code, start, end = '512680', date(2026, 7, 10), date(2026, 7, 13)
    _seed(db, code, [date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 13)])
    assert m.backfill_history_range(code, start, end, db=db) == 0
    assert calls == []


def test_missing_start_still_backfills(manager):
    """整段起点就没有数据的老行为不能退化。"""
    m, calls = manager
    db = _session()
    code, start, end = '512480', date(2026, 7, 1), date(2026, 7, 31)
    _seed(db, code, [end - timedelta(days=i) for i in range(3)])
    assert m.backfill_history_range(code, start, end, db=db) > 0
    assert calls
