# -*- coding: utf-8 -*-
""""结论证据是否还复现得出来"这条派生判据，与改标必须清结论的硬规矩。

背景（第 16~18 轮）：全库 1163 条已判结论里有 250 条（21.5%），其端点净值在**当前标的**
的净值表里已经查不出来 —— 其中 53 条是"改标后结论没重算"（存的净值只等于改标前那只基金
当天的值，例 id=1903：现挂 512170、结论却来自 512010 在 07-16 的 0.3788）。
历轮"标量 vs 自家台账"自洽检查看不见它，因为两者是同一次验证一起写的。

这里钉住三件事：
1. 判据本身（含"改标后又按新标的重验过"不能算挂错）；
2. **改标必须同时清掉旧结论并留痕** —— 这是防复发的正解，光清理存量数据只是打地鼠；
3. 列表接口把状态带出去（前端的 ⚠ 标记读它）。
"""
from datetime import date

from src.models.database import (
    Blogger, FundHistory, Prediction, PredictionChangeLog, Post)
from src.fund.fund_sync_manager import FundSyncManager
from src.services.prediction_query_service import PredictionQueryService
from src.services.verdict_evidence import evidence_status

D = date(2026, 7, 16)


def _row(**kw):
    defaults = dict(id=1, fund_code='512170', end_nav_date=D, end_nav=0.3788,
                    is_correct=True)
    defaults.update(kw)
    return type('R', (), defaults)()


def test_evidence_status_covers_every_outcome():
    p = _row()
    # 当前标的、那一天、那个值 —— 证据还在，结论可复现
    assert evidence_status(p, {('512170', D): 0.3788}) is None
    assert evidence_status(p, {('512170', D): 0.3788000001}) is None      # 浮点末位不算漂移
    assert evidence_status(p, {}) == 'nav_row_missing'
    assert evidence_status(p, {('512170', D): 0.3335}) == 'nav_rewritten'
    # 写下结论时挂的是另一只基金，且那只基金当天正是这个值 ⇒ 真·挂错标的
    assert evidence_status(p, {('512170', D): 0.3335, ('512010', D): 0.3788},
                           verified_code='512010') == 'verdict_under_other_fund'


def test_reverified_after_retag_is_not_counted_as_mis_homed():
    """改标**之后**又按新标的重验过的行，不能因为"曾经改过标"就算成挂错。

    这正是我第一版归责报出 97 条、实际只有几十条的原因。
    """
    p = _row()
    only_new = {('512170', D): 0.3788, ('512010', D): 0.9}
    assert evidence_status(p, only_new, verified_code='512010') is None


def test_unverified_rows_are_never_flagged():
    assert evidence_status(_row(is_correct=None), {}) is None
    assert evidence_status(_row(end_nav=None, end_nav_date=None), {}) is None


def _seed_verified_prediction(db):
    blogger = Blogger(name='S9改标测试博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, content='S9 测试帖子', post_date=date(2026, 7, 1))
    db.add(post)
    db.flush()
    for code, nav in (('512010', 0.3788), ('512170', 0.3335)):
        db.add(FundHistory(fund_code=code, nav_date=D, nav=nav))
    prediction = Prediction(
        post_id=post.id, blogger_id=blogger.id, fund_code='512010',
        fund_name='医药ETF', sector='医药', prediction_type='up',
        prediction_content='上涨', prediction_date=date(2026, 7, 1),
        prediction_period='2周', target_date=D, status='success', is_correct=True,
        verify_count=1, verify_score=100, end_nav=0.3788, end_nav_date=D,
        actual_change=1.0, is_expired=True)
    db.add(prediction)
    db.commit()
    return prediction


def test_retagging_clears_the_verdict_and_leaves_a_log(test_db):
    """换标的必须把旧标的判出的结论一起清掉 —— 准确率的地基不能跟着换。"""
    prediction = _seed_verified_prediction(test_db)

    cleared = FundSyncManager.retag_prediction(
        test_db, prediction, '512170', '半导体ETF', source='unit-test')
    test_db.commit()

    assert cleared is True
    test_db.refresh(prediction)
    assert prediction.fund_code == '512170'
    assert prediction.is_correct is None, '旧标的判出的结论还挂在行上'
    assert prediction.status == 'pending' and prediction.verify_score is None
    assert prediction.end_nav is None and prediction.verify_count == 0

    log = test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id == prediction.id).order_by(
        PredictionChangeLog.id.desc()).first()
    assert log is not None and 'fund_code' in (log.changed_fields or []), \
        '改标没留痕 ⇒ 事后无法归责（audit 里那 53 条就是这么来的）'
    assert log.before_state['fund_code'] == '512010'
    assert log.before_state['is_correct'] is True


def test_retag_without_change_is_a_no_op(test_db):
    prediction = _seed_verified_prediction(test_db)
    assert FundSyncManager.retag_prediction(
        test_db, prediction, '512010', '医药ETF', source='unit-test') is False
    assert test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id == prediction.id).count() == 0
    test_db.refresh(prediction)
    assert prediction.is_correct is True, '无事也要清结论就是数据破坏'


def test_list_endpoint_exposes_the_derived_badge(test_db):
    """前端 ⚠ 标记读的是接口字段；字段名一旦漂移，页面会安静地永远不亮。"""
    prediction = _seed_verified_prediction(test_db)
    # 把结论改成"512170 那天其实不是这个值"，模拟净值被就地改写
    prediction.fund_code = '512170'
    test_db.commit()

    payload = PredictionQueryService(test_db).search(page=1, page_size=50)
    row = next(r for r in payload['data'] if r['id'] == prediction.id)
    assert row['evidence_status'] == 'nav_rewritten', row['evidence_status']
    assert '净值' in row['evidence_note']
