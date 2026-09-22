# -*- coding: utf-8 -*-
"""第 17 轮：owner 署名的唯一入口，与"自带标的不过体检"的验证侧后门。

两条不变量，都是被两份独立复评各自指出的：
1. `reviewed_by='owner' + owner_locked`（＝身份体检豁免）**只能由显式 `owner_confirm` 换来**。
   上一轮只把确认做在浏览器弹窗里，服务层对有机器证据的行仍然无条件盖 owner ⇒
   裸 POST 一次就买到批量被拒绝的免疫（MAJOR-1）。撤销入口同理必须存在（MAJOR-2）。
2. `match_fund_for_prediction` 的第一优先级"直接用预测自带的 fund_code"以前**不过身份体检**，
   而同一函数后面三条分支都过 ⇒ 已被判不可服务的代码仍会驱动验证，结论挂到错标的上
   （MAJOR-2 / 与 `scripts/audit_verdict_evidence.py` 的 `verdict_under_other_fund` 同族）。
"""
import pytest

from src.models.database import Blogger, FundInfo, Post, Prediction, SectorFundMapping
from src.services.prediction_verify_service import PredictionVerifyService
from src.services.sector_fund_service import SectorFundService


def _mapping(db, **kw):
    row = SectorFundMapping(is_active=True, **kw)
    db.add(row)
    db.commit()
    return row


def test_single_row_review_grants_owner_only_with_explicit_confirm(test_db):
    """有机器证据的行：不带 owner_confirm 只算"看过"，不许给署名与豁免。"""
    from datetime import datetime

    row = _mapping(test_db, sector_name='R17算力', fund_code='515070',
                   fund_name='人工智能ETF', reviewed=False,
                   match_source='agent', verified_at=datetime.now(), confidence=0.9)
    svc = SectorFundService(test_db)

    assert svc.mark_reviewed_by_id(row.id, reviewed=True, owner_confirm=False) is True
    test_db.refresh(row)
    assert row.reviewed is True
    assert row.owner_locked is not True, '不确认就给锁定 ⇒ 豁免成了默认赠品'
    assert row.reviewed_by != 'owner', '不确认就冒充老板署名'

    assert svc.mark_reviewed_by_id(row.id, reviewed=True, owner_confirm=True) is True
    test_db.refresh(row)
    assert row.owner_locked is True and row.reviewed_by == 'owner'


def test_cancel_review_clears_signature_and_lock(test_db):
    """撤销必须把署名与锁定一起收回去（页面那句"再点一次取消审查"得有落点）。"""
    from datetime import datetime

    row = _mapping(test_db, sector_name='R17储存', fund_code='512400',
                   fund_name='有色ETF', reviewed=False,
                   match_source='agent', verified_at=datetime.now())
    svc = SectorFundService(test_db)
    svc.mark_reviewed_by_id(row.id, reviewed=True, owner_confirm=True)
    test_db.refresh(row)
    assert row.owner_locked is True

    assert svc.mark_reviewed_by_id(row.id, reviewed=False) is True
    test_db.refresh(row)
    assert row.reviewed is False
    assert not row.owner_locked, '取消审查还留着锁定 = 永久豁免撤不掉'
    assert row.reviewed_by is None


def _prediction_with_code(db, code, sector):
    blogger = Blogger(name='R17测试博主', platform='wechat')
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, content='R17 测试帖子',
                post_date=__import__('datetime').date(2026, 6, 1))
    db.add(post)
    db.flush()
    prediction = Prediction(post_id=post.id, blogger_id=blogger.id, fund_code=code,
                            fund_name='自带代码', sector=sector, prediction_type='up',
                            prediction_content='上涨',
                            prediction_date=__import__('datetime').date(2026, 6, 1),
                            prediction_period='1周',
                            target_date=__import__('datetime').date(2026, 6, 8))
    db.add(prediction)
    db.commit()
    return prediction


def test_unservable_self_code_falls_back_to_the_sector_resolution(test_db):
    """自带代码被体检判不可服务时，改按板块重新解析，而不是照旧验证到错标的上。"""
    _mapping(test_db, sector_name='R17机器人', fund_code='BAD001',
             fund_name='某股票名挂在基金码', is_fetchable=False)      # 体检判不可服务
    _mapping(test_db, sector_name='R17机器人', fund_code='GOOD01',
             fund_name='机器人ETF', reviewed=True)
    db_session = test_db
    prediction = _prediction_with_code(db_session, 'BAD001', 'R17机器人')

    service = PredictionVerifyService(db_session)
    assert service.fund_code_is_servable('BAD001') is False
    assert service.fund_code_is_servable('GOOD01') is True
    # 体检没意见的代码一律放行（不能因为"没有映射行"就把历史预测卡死）
    assert service.fund_code_is_servable('UNKNOWN9') is True

    code, _name = service.match_fund_for_prediction(prediction)
    assert code != 'BAD001', '不可服务的自带代码仍在驱动验证 ⇒ 结论挂错标的'
    # 解析到哪一条（已审查映射 / 静态表）由既有优先级决定，这里只要求"换成了可服务的标的"
    assert service.fund_code_is_servable(code) is True


def test_servable_self_code_is_still_used_directly(test_db):
    """反向：体检没拒绝时不许无故改标的，否则每轮验证都可能换一只基金。"""
    _mapping(test_db, sector_name='R17半导体', fund_code='KEEP01',
             fund_name='半导体ETF', is_fetchable=True)
    prediction = _prediction_with_code(test_db, 'KEEP01', 'R17半导体')
    code, _name = PredictionVerifyService(test_db).match_fund_for_prediction(prediction)
    assert code == 'KEEP01'
