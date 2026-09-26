# -*- coding: utf-8 -*-
"""「结构性不可验」这把重问锁的判据（任务 #8）。

要钉的三件事，缺一不可：
1. **只有真问过、且数据源答"这段给不出"**才配锁 —— 别的失败（点数不够、没档案）
   明天可能就自愈，锁了它等于亲手把一条可验的预测藏起来；
2. 锁**会自己到期**，到点这条预测回到到期队列再问一次（不是终态、不是软删）；
3. 页面上**减出来的两个数要能加回去**（`due` + `unverifiable` ＝ 全部到期未判），
   否则"到期 0 条"会被读成"全都验完了"。

第 2 点还有一条**支撑它的不变式**（用例 4）：`next_verify_date` 的两个创建出口都
把它夹在目标日之前 ⇒ "已到期且这个日期晚于今天"只可能由验证器真问过之后写下。
哪天有人让排期越过目标日，"结构性不可验"就会开始冤枉人，所以这条得钉住。
"""
from datetime import date, timedelta

import pytest

from src.models.database import Blogger, Post, Prediction
from src.services import prediction_lifecycle as lc
from src.services.prediction_lifecycle import (
    DUE_UNVERIFIED, UNVERIFIABLE, classify, filter_due_for_verify,
    filter_unverifiable, unverifiable_retry_days,
)
from src.fund import backfill_proofs


TODAY = date.today()


def _seed(db, *, target, next_verify=None, fund_code='HOLD01', pred_type='up'):
    blogger = db.query(Blogger).filter(Blogger.name == '重问锁博主').first()
    if not blogger:
        blogger = Blogger(name='重问锁博主', platform='wechat')
        db.add(blogger)
        db.flush()
    post = Post(blogger_id=blogger.id, title='hold', content='hold',
                post_date=target - timedelta(days=7))
    db.add(post)
    db.flush()
    p = Prediction(post_id=post.id, blogger_id=blogger.id,
                   fund_code=fund_code, fund_name='重问锁基金', sector='测试',
                   prediction_type=pred_type, prediction_date=post.post_date,
                   prediction_period='1周', target_date=target, status='pending',
                   next_verify_date=next_verify, is_expired=False)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _service(db, monkeypatch, availability):
    """把验证器打到"只剩数据充分性判断"这一层：不外呼、不碰净值。"""
    import importlib
    from src.services.prediction_verify_service import PredictionVerifyService

    api = importlib.import_module('src.fund.fund_api')
    monkeypatch.setattr(api.fund_data_manager, 'backfill_history_range',
                        lambda *a, **k: False, raising=True)
    svc = PredictionVerifyService(db)
    monkeypatch.setattr(type(svc), 'match_fund_for_prediction',
                        lambda self, p: ('HOLD01', '重问锁基金'))
    monkeypatch.setattr(type(svc), '_check_fund_data_availability',
                        lambda self, **kw: dict(availability))
    return svc


def test_a_structural_verdict_holds_the_row_out_of_todays_queue(test_db, monkeypatch):
    """问过、数据源答"这段没有"⇒ 今天起不入队，且**一个结论都没写**。"""
    p = _seed(test_db, target=TODAY - timedelta(days=3))
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history',
        'message': '目标日附近这段历史净值不足'})

    result = svc.verify_prediction(p.id)

    assert result['success'] is False
    held = TODAY + timedelta(days=unverifiable_retry_days())
    assert result['held_until'] == held.isoformat()
    test_db.refresh(p)
    assert p.next_verify_date == held
    # 它不是结论：判对/判错、分数、状态全都不许动
    assert p.is_correct is None and p.verify_score in (0, None)

    as_of = TODAY
    assert classify(p, as_of=as_of) == UNVERIFIABLE
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=as_of)}
    assert {x.id for x in filter_unverifiable(test_db, as_of=as_of)} == {p.id}
    assert '结构性不可验' in (lc.due_skip_reason(p, as_of=as_of) or '')


def test_a_failure_that_was_never_asked_about_does_not_hold_the_row(test_db, monkeypatch):
    """反面对照：同样是"验不了"，`insufficient_points`（还没问过源端）不许被锁。

    没有这一条，上面那条用例对"逢失败就锁"的写法结构上不会红 —— 而那会把
    明天就能自愈的预测永久藏出到期队列。
    """
    p = _seed(test_db, target=TODAY - timedelta(days=3))
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'insufficient_points',
        'message': '基金数据不足，请更新基金数据后再验证'})

    result = svc.verify_prediction(p.id)

    assert result['held_until'] is None
    test_db.refresh(p)
    assert p.next_verify_date is None
    assert classify(p, as_of=TODAY) == DUE_UNVERIFIED
    assert p.id in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}


def test_the_lock_expires_and_the_prediction_asks_again(test_db, monkeypatch):
    """到重问日它必须自己回到到期队列 —— 这是"锁"和"终态"的唯一区别。"""
    p = _seed(test_db, target=TODAY - timedelta(days=3))
    _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history',
        'message': '目标日附近这段历史净值不足'}).verify_prediction(p.id)
    test_db.refresh(p)
    held = p.next_verify_date

    # 锁住的是"重问日之前"：那一天之前不入队，到了那天自己回队（边界在 SQL 与
    # classify 两处都是 `hold > as_of`，写反一天就等于这批永远不回队）
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=held - timedelta(days=1))}
    assert p.id in {x.id for x in filter_due_for_verify(test_db, as_of=held)}
    assert classify(p, as_of=held) == DUE_UNVERIFIED
    assert filter_unverifiable(test_db, as_of=held) == []
    # 锁的天数跟着凭据 TTL 走，不是拍出来的常数
    assert held == TODAY + timedelta(days=int(backfill_proofs.EMPTY_TTL_DAYS) + 1)


@pytest.mark.parametrize('period_days', [0, 1, 5, 6, 11, 30, 45])
def test_the_creation_schedule_never_writes_a_date_after_the_target(period_days):
    """不变式：两个创建出口夹出来的 `next_verify_date` 不得晚于目标日。

    晚于目标日的排期会被 `is_held_unverifiable` 读成"数据源给不出这段"⇒ 冤枉一条
    本来可验的预测。这条用例就是在防那一天。
    """
    from src.services.prediction_service import PredictionService
    from src.analyzer.llm_analyzer import LLMAnalyzer

    pred_date = TODAY - timedelta(days=9)
    target = pred_date + timedelta(days=period_days)
    for value in (PredictionService._calculate_next_verify_date(pred_date, target),
                  LLMAnalyzer.calculate_next_verify_date(LLMAnalyzer.__new__(LLMAnalyzer),
                                                         pred_date, target)):
        assert value <= target, f'排期 {value} 越过了目标日 {target}：结构性归因会开始冤枉人'


def test_the_list_route_splits_the_two_counts_and_they_add_up(test_db, monkeypatch):
    """路由级（不是私有方法级）：`due` + `unverifiable` 必须等于全部到期未判。

    只看服务层的私有方法，绿灯替不了"页面上真有这个按钮、接口真给这个数"。
    """
    from src.api.routes.predictions import get_predictions

    held = _seed(test_db, target=TODAY - timedelta(days=4))
    plain = _seed(test_db, target=TODAY - timedelta(days=2), fund_code='HOLD02')
    soon = _seed(test_db, target=TODAY + timedelta(days=9), fund_code='HOLD03')
    lc.apply_unverifiable_hold(held, as_of=TODAY)
    test_db.commit()

    def facets():
        return get_predictions(db=test_db, exclude_flat=True)['meta']['facets']

    def ids(lifecycle):
        page = get_predictions(db=test_db, lifecycle=lifecycle, exclude_flat=True)
        return {row['id'] for row in page['data']}

    assert facets()['due'] == 1 and facets()['unverifiable'] == 1
    assert facets()['due'] + facets()['unverifiable'] == 2      # 到期未判总数没被吞
    assert ids('due') == {plain.id}
    assert ids('unverifiable') == {held.id}
    assert facets()['upcoming'] == 1 and soon.id not in ids('due')
    # 两档互斥：同一条预测不许既"今天可验"又"结构性不可验"
    assert not (ids('due') & ids('unverifiable'))


def test_the_batch_receipt_says_why_a_row_was_not_verified(test_db, monkeypatch):
    """批量验证的回执必须点名被压住的那些，而不是让它们从"到期不验证"里消失。

    `verify_all_pending` 早就把 `filter_unverifiable` 接进了 `skipped`，
    上一批 `filter_unverifiable` 恒为空 ⇒ 那条接线从来没被走过。这条用例是它第一次有人测。
    """
    from src.services.prediction_verify_service import PredictionVerifyService

    held = _seed(test_db, target=TODAY - timedelta(days=4), fund_code='HOLD09')
    lc.apply_unverifiable_hold(held, as_of=TODAY)
    test_db.commit()

    result = PredictionVerifyService(test_db).verify_all_pending(as_of=TODAY)

    assert result['success'] is True
    reasons = {s['prediction_id']: s['reason'] for s in result['data']['skipped']}
    assert held.id in reasons, '被压住的预测既不在队列里、也不在回执里 = 没人知道它去哪了'
    assert '结构性不可验' in reasons[held.id]
    assert held.id not in {r['prediction_id'] for r in result['data']['results']}


def test_when_the_nav_arrives_the_lock_is_taken_off(test_db, monkeypatch):
    """净值后来补上了 ⇒ 锁必须撤掉，否则这条预测永远带着一个已失效的标签。"""
    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY + timedelta(days=5))
    svc = _service(test_db, monkeypatch, {
        'available': True, 'message': '数据充足', 'data_points': 9,
        'latest_date': p.target_date, 'end_nav_age_days': 0, 'reason': 'exact_target'})
    # 这一支只关心"锁撤没撤"，后面取净值的腿一律不真外呼（conftest 会拦，但拦在这里
    # 只会让用例红得不相关）
    monkeypatch.setattr(type(svc), 'get_nav_by_date', lambda self, *a, **k: None)

    svc.verify_prediction(p.id)      # 后面会因取不到净值而停住，锁也该已经撤了

    test_db.refresh(p)
    assert p.next_verify_date is None
    assert classify(p, as_of=TODAY) == DUE_UNVERIFIED
    assert p.is_correct is None
