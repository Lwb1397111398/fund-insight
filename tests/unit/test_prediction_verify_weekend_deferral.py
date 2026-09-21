# -*- coding: utf-8 -*-
"""S7-1：目标日落在休市日的短期预测，不该被"基金数据不足"永远挡在门外。

复现的原始事实（本地镜像库，预测 1709：512680，周期 1 天，目标 2026-07-11 周六）：
`[起点, 目标日]` 窗口里只有 1 条净值（07-10 周五），而 `VERIFY_MIN_DATA_POINTS=2`，
于是判"基金数据不足…请更新基金数据"；可这个类后面**本来就有**"目标日是周末就用
此前最近净值验证"的分支（`weekend_previous`），只是被充分性门挡在前面走不到。
修法是只放宽判据（顺延 7 天能凑够就放行），**终点净值的取法一字不改**。
"""
from datetime import date

import pytest

from src.models.database import FundHistory
from src.services.prediction_verify_service import PredictionVerifyService

START = date(2026, 7, 10)        # 周五：预测起点
TARGET_SAT = date(2026, 7, 11)   # 周六：休市，没有净值
NEXT_MON = date(2026, 7, 13)


def _rows(db, code, days):
    for i, d in enumerate(days):
        db.add(FundHistory(fund_code=code, nav_date=d, nav=1.0 + i * 0.01))
    db.commit()


def _check(db, code, start, end, min_points=2, today=None):
    return PredictionVerifyService(db)._check_fund_data_availability(
        fund_code=code, nav_start_date=start, window_end=end, target_date=end,
        min_data_points=min_points, today=today or date(2026, 9, 21))


def test_weekend_target_defers_instead_of_claiming_missing_data(test_db):
    """窗口内只有 1 条，但顺延到下个交易日就有 2 条 ⇒ 必须放行并走周末分支。"""
    _rows(test_db, '512680', [date(2026, 7, 9), START, NEXT_MON])
    r = _check(test_db, '512680', START, TARGET_SAT)
    assert r['available'] is True, r
    assert r['reason'] == 'weekend_previous', r
    assert r['data_points'] == 1, '放行不该伪造点数'
    # 终点净值仍取目标日**之前**最近那条，不拿顺延后的价格冒充目标日
    assert r['latest_date'] == START, r


def test_genuinely_missing_history_still_fails(test_db):
    """顺延窗口内也凑不够点数：照旧判"数据不足"，不能为了绿灯放水。"""
    _rows(test_db, '515440', [date(2026, 9, 2), date(2026, 9, 3)])   # 远离目标日
    r = _check(test_db, '515440', date(2026, 7, 27), date(2026, 7, 28))
    assert r['available'] is False
    assert r['reason'] == 'insufficient_points', r


def test_no_nav_before_target_is_not_treated_as_a_weekend(test_db):
    """第 10 轮评审的窄化：目标日前一条净值都没有 = 缺历史，不是休市顺延。

    158038 就是这种：目标日 09-04，本地净值最早 09-07。放它过门的话，
    验证侧拿不到 `nav_date <= target` 的终点净值，只能靠 API 兜底取"当前最新"，
    那是未来函数 —— 准确率核心不允许这种口子。
    """
    _rows(test_db, '158038', [date(2026, 9, 7), date(2026, 9, 8)])
    r = _check(test_db, '158038', date(2026, 8, 28), date(2026, 9, 4))
    assert r['available'] is False, r
    assert r['reason'] == 'insufficient_points', r


def test_normal_weekday_case_is_untouched(test_db):
    """已能验证的常规预测结论不变（这道修改只放宽、不收紧）。"""
    t = date(2026, 7, 15)        # 周三，有净值
    _rows(test_db, '159915', [date(2026, 7, 14), t, date(2026, 7, 16)])
    r = _check(test_db, '159915', date(2026, 7, 14), t)
    assert r['available'] is True and r['reason'] == 'exact_target', r
