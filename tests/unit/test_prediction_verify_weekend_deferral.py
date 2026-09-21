# -*- coding: utf-8 -*-
"""S7-2：目标日落在休市日的短期预测 —— 不能放宽成假结论，也不能混进"数据不足"。

复现的原始事实（本地镜像库，预测 1709：512680，周期 1 天，目标 2026-07-11 周六）：
`[起点, 目标日]` 窗口里只有 07-10 一条净值。上一轮（S7-1）的做法是放宽点数门放它过去，
结果 `weekend_previous` 分支取到的起点净值与终点净值是**同一条**，涨跌幅恒为 0，
今天真把它判成了「预测方向错误，最终涨跌+0.00%」并计入准确率 —— 那是假结论。
所以 S7-2 改成：起点与终点落在同一条净值时**拒判**（`same_nav_endpoint`），
并且把"缺目标日附近历史"（补拉最新数据永远补不到）与"净值太旧"（该更新）分开说。
"""
from datetime import date

import pytest

from src.models.database import Blogger, FundHistory, Post, Prediction
from src.fund import fund_api as fund_api_module
from src.services.prediction_verify_service import PredictionVerifyService

START = date(2026, 7, 10)        # 周五：预测起点，也是唯一一条落在目标日及之前的净值
TARGET_SAT = date(2026, 7, 11)   # 周六：休市，没有净值
PREV = date(2026, 7, 9)
NEXT_MON = date(2026, 7, 13)


def _rows(db, code, days):
    for i, d in enumerate(days):
        db.add(FundHistory(fund_code=code, nav_date=d, nav=1.0 + i * 0.01))
    db.commit()


def _check(db, code, start, end, min_points=2, today=None):
    return PredictionVerifyService(db)._check_fund_data_availability(
        fund_code=code, nav_start_date=start, window_end=end, target_date=end,
        min_data_points=min_points, today=today or date(2026, 9, 21))


@pytest.fixture(autouse=True)
def no_network_backfill(monkeypatch):
    """单测零网络：验证路径里的按需补拉一律桩掉（这些用例要测的是判据，不是抓取）。"""
    class _NoProbe:
        @staticmethod
        def backfill_history_range(*a, **kw):
            return 0

    monkeypatch.setattr(fund_api_module, 'fund_data_manager', _NoProbe(), raising=False)


def _seed(db, code, prediction_date, target_date):
    blogger = Blogger(name='休市测试博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, title='t', content='c', post_date=prediction_date)
    db.add(post)
    db.flush()
    p = Prediction(post_id=post.id, blogger_id=blogger.id, fund_code=code,
                   fund_name='测试基金', sector='测试', prediction_type='up',
                   prediction_date=prediction_date, prediction_period='1天',
                   target_date=target_date, status='pending', is_deleted=False)
    db.add(p)
    db.commit()
    return p


def test_single_nav_endpoint_is_refused_not_judged_flat(test_db):
    """1709 复现：窗口里只有起点那一条 ⇒ 拒判，且给的原因不是"数据不足"。"""
    _rows(test_db, '512680', [PREV, START, NEXT_MON])
    r = _check(test_db, '512680', START, TARGET_SAT)
    assert r['available'] is False, r
    assert r['reason'] == 'same_nav_endpoint', r
    assert '涨跌幅必然为 0' in r['message'], r


def test_refusing_a_degenerate_endpoint_writes_no_conclusion(test_db, monkeypatch):
    """拒判必须真的不落库：`is_correct` 保持 NULL，不产生 0% 的假结论。"""
    from src.services import prediction_verify_service as pvs

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 21)

    monkeypatch.setattr(pvs, 'date', FixedDate)
    _rows(test_db, '512680', [PREV, START])
    p = _seed(test_db, '512680', START, TARGET_SAT)
    result = PredictionVerifyService(test_db).verify_prediction(p.id)
    test_db.refresh(p)
    assert result.get('success') is False, result
    assert p.is_correct is None, '退化终点不该写结论'
    assert p.status == 'pending', p.status
    assert not p.end_nav_date, p.end_nav_date


def test_weekend_target_with_a_real_second_point_still_verifies(test_db):
    """收紧不能过头：起点 07-09、终点 07-10 是两条净值，周末目标日照旧按前值判定。"""
    _rows(test_db, '159206', [PREV, START, NEXT_MON])
    r = _check(test_db, '159206', PREV, TARGET_SAT)
    assert r['available'] is True, r
    assert r['reason'] == 'weekend_previous', r
    assert r['data_points'] == 2 and r['latest_date'] == START, r


def test_missing_history_near_target_no_longer_says_update_the_data(test_db):
    """515440/158038 类：本地最新净值**晚于**窗口终点 ⇒ 缺的是那段历史，
    提示"请更新基金数据"会把人带去跑同步（同步只补最近端，永远补不到）。"""
    _rows(test_db, '515440', [date(2026, 9, 2), date(2026, 9, 3)])
    r = _check(test_db, '515440', date(2026, 7, 27), date(2026, 7, 28))
    assert r['available'] is False and r['reason'] == 'insufficient_points', r
    assert '请更新基金数据' not in r['message'], r
    assert '补拉最新数据补不到这个窗口' in r['message'], r


def test_stale_nav_still_asks_for_an_update(test_db):
    """反向：最新净值确实**落后**于窗口终点（该更新数据）时，仍要说"请更新基金数据"。"""
    _rows(test_db, '518880', [date(2026, 5, 6), date(2026, 5, 7)])
    r = _check(test_db, '518880', date(2026, 7, 27), date(2026, 7, 28))
    assert r['available'] is False and r['reason'] == 'insufficient_points', r
    assert r['days_behind'] > 0 and '请更新基金数据' in r['message'], r


def test_normal_weekday_case_is_untouched(test_db):
    """已能验证的常规预测结论不变（这道修改只收紧退化情形，不动正常窗口）。"""
    t = date(2026, 7, 15)        # 周三，有净值
    _rows(test_db, '159915', [date(2026, 7, 14), t, date(2026, 7, 16)])
    r = _check(test_db, '159915', date(2026, 7, 14), t)
    assert r['available'] is True and r['reason'] == 'exact_target', r
