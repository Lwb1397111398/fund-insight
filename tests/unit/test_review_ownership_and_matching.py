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
import ast
import re

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

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
    assert prediction.fund_code == 'KEEP01'


def test_verify_retags_instead_of_judging_on_another_fund(test_db):
    """第 18 轮 MAJOR-5：退到别的代码去判，就必须把行改标到那个代码上。

    改动前会红：`verify_prediction` 拿 B 算完结论，`prediction.fund_code` 仍然是 A、
    一条变更日志都不写 ⇒ "结论按 B 判、行上挂 A"继续新增（`scripts/audit_verdict_evidence.py`
    的 `verdict_under_other_fund` 那一族），而且因为回写从不发生，那个徽章在新数据上
    永远测不到 = 闸门是装的。
    """
    from datetime import date, timedelta

    from src.models.database import PredictionChangeLog
    from src.services.prediction_verify_service import has_verdict_trace

    _mapping(test_db, sector_name='R17改标', fund_code='BAD002',
             fund_name='某股票名挂在基金码', is_fetchable=False)
    _mapping(test_db, sector_name='R17改标', fund_code='GOOD02',
             fund_name='机器人ETF', reviewed=True)
    prediction = _prediction_with_code(test_db, 'BAD002', 'R17改标')
    # 旧标的上判出来的结论：改标必须一并清掉，不然准确率里留着一张别的基金的成绩单
    prediction.is_correct = True
    prediction.actual_change = 1.23
    prediction.status = 'verified'
    prediction.verify_count = 1
    # 目标日推远 ⇒ 本轮验证在匹配之后立刻返回"通道未开放"，正好只测"换标的时做了什么"
    prediction.target_date = date.today() + timedelta(days=30)
    test_db.commit()
    assert has_verdict_trace(prediction) is True

    service = PredictionVerifyService(test_db)
    res = service.verify_prediction(prediction.id)
    assert res.get('success') is False        # 本轮没判（通道未开放），但改标已经落库

    test_db.refresh(prediction)
    assert prediction.fund_code == 'GOOD02', '行上还挂着不可服务的 A'
    assert has_verdict_trace(prediction) is False, '旧标的的结论没跟着清掉'
    log = test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id == prediction.id,
        PredictionChangeLog.source == 'verify_unservable_code').first()
    assert log is not None, '改标没留痕 ⇒ 无法整批还原'
    assert (log.before_state or {}).get('fund_code') == 'BAD002'
    # 反向不变量：可服务的代码不许被这条路径动过
    assert test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id == prediction.id).count() == 1



def _persisted_stats(db, blogger_id):
    """从**表里**读统计列，绕开会话对象。

    `recalculate_blogger_stats(commit=False)` 会把值直接写进同一会话里的那个
    `Blogger` 对象 ⇒ "refresh 后拿它跟真值比"是个永远为假的自检
    （第 19 轮两份复评共同点出我这个写法）。
    """
    import sqlalchemy as sa
    from src.models.database import Blogger
    row = db.execute(sa.select(
        Blogger.total_predictions, Blogger.correct_predictions,
        Blogger.total_verify_score).where(Blogger.id == blogger_id)).one()
    return {'total_predictions': row[0], 'correct_predictions': row[1],
            'total_verify_score': float(row[2] or 0)}


def test_verify_side_retag_keeps_blogger_stats_honest(test_db):
    """第 19 轮 MAJOR-1：验证侧改标清了结论，博主统计列必须跟着重算。

    改动前会红：`retag_prediction` 的 `touched_bloggers` 没传 ⇒ 谁都不重算，
    而 `blogger_stats` 按 `verify_count>0` 现算；本轮验证如果判完，只会再
    `verified_delta=+1` ⇒ 同一行在"已验证数"里被计两次（评审在副本库实测 87 vs 真值 86）。
    """
    from datetime import date, timedelta

    from src.utils.blogger_stats import recalculate_blogger_stats

    _mapping(test_db, sector_name='R19改标', fund_code='BAD003',
             fund_name='某股票名挂在基金码', is_fetchable=False)
    _mapping(test_db, sector_name='R19改标', fund_code='GOOD03',
             fund_name='机器人ETF', reviewed=True)
    prediction = _prediction_with_code(test_db, 'BAD003', 'R19改标')
    prediction.is_correct = True
    prediction.actual_change = 1.23
    prediction.verify_count = 1
    prediction.verify_score = 100
    prediction.status = 'verified'
    prediction.target_date = date.today() + timedelta(days=30)
    test_db.commit()
    blogger = prediction.blogger
    recalculate_blogger_stats(test_db, blogger.id)
    assert _persisted_stats(test_db, blogger.id)['total_predictions'] == 1, \
        '前置：这条结论要被统计到'

    PredictionVerifyService(test_db).verify_prediction(prediction.id)

    # 旧标的的结论已被清掉 ⇒ 真值是 0；列上还留着 1 就是"没重算"
    assert _persisted_stats(test_db, blogger.id)['total_predictions'] == 0, \
        '清结论没重算统计列 ⇒ 后面重验的 +1 会变成双计'


def test_verify_side_retag_does_not_double_count_blogger_stats(test_db, monkeypatch):
    """把上一条补成"真跑到 +1 那一腿"（两份复评共同指出早退版测不到）。

    改标清结论后本轮就真的按新标的判完了：`is_newly_completed` 会给博主统计
    `verified_delta=+1`。少了那次重算，列上仍是旧的 1 ⇒ 同一行计成 2。
    """
    from datetime import date

    from src.models.database import FundHistory
    from src.services import prediction_verify_service as pvs_module
    from src.utils.blogger_stats import recalculate_blogger_stats

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 6, 9)
    monkeypatch.setattr(pvs_module, "date", FixedDate)

    _mapping(test_db, sector_name='R19双计', fund_code='BAD004',
             fund_name='某股票名挂在基金码', is_fetchable=False)
    _mapping(test_db, sector_name='R19双计', fund_code='GOOD04',
             fund_name='机器人ETF', reviewed=True)
    prediction = _prediction_with_code(test_db, 'BAD004', 'R19双计')
    prediction.is_correct = True
    prediction.actual_change = 1.0
    prediction.verify_count = 1
    prediction.verify_score = 80
    prediction.status = 'success'
    prediction.prediction_date = date(2026, 6, 1)
    prediction.target_date = date(2026, 6, 8)
    test_db.commit()
    recalculate_blogger_stats(test_db, prediction.blogger_id)
    # 只有新标的 GOOD04 有净值：不改标就判不完，改了标才判得完
    test_db.add_all([FundHistory(fund_code='GOOD04', nav_date=d, nav=n) for d, n in (
        (date(2026, 6, 1), 1.00), (date(2026, 6, 2), 1.02), (date(2026, 6, 3), 1.03),
        (date(2026, 6, 4), 1.04), (date(2026, 6, 5), 1.05), (date(2026, 6, 8), 1.06))])
    test_db.commit()

    result = PredictionVerifyService(test_db).verify_prediction(prediction.id)
    assert result['success'] is True, result          # 真的判完了
    assert result['data']['fund_code'] == 'GOOD04'

    persisted = _persisted_stats(test_db, prediction.blogger_id)
    truth = recalculate_blogger_stats(test_db, prediction.blogger_id, commit=False)
    assert persisted['total_predictions'] == 1, \
        '统计列 %s vs 现算真值 1 ⇒ 改标清掉的旧结论没重算，重验的 +1 变成双计' \
        % persisted['total_predictions']
    assert persisted['correct_predictions'] == truth['correct_predictions'], persisted
    assert abs(persisted['total_verify_score'] - truth['total_verify_score']) < 1e-6, \
        '累计分也不能留旧的那一轮（80 分不能算两次）'


def test_rollback_audit_does_not_judge_a_row_by_another_fund(test_db):
    """第 19 轮 MAJOR-3：标的已漂移的行，不许用"另一只基金缺数据"去撤它的结论。

    改动前会红：`rollback_invalid_verifications` 也调 `match_fund_for_prediction`，
    拿到的是 B 的代码，却拿它的数据充分性去判 A 的结论该不该撤 ⇒ 一个与这条结论
    无关的理由把记录抹了。现在这类行只数不撤（改标交给 `verify_prediction`）。
    """
    from datetime import date

    _mapping(test_db, sector_name='R19漂移', fund_code='BAD005',
             fund_name='某股票名挂在基金码', is_fetchable=False)
    _mapping(test_db, sector_name='R19漂移', fund_code='GOOD05',
             fund_name='机器人ETF', reviewed=True)      # GOOD05 一条净值都没有
    prediction = _prediction_with_code(test_db, 'BAD005', 'R19漂移')
    prediction.is_correct = True
    prediction.actual_change = 1.23
    prediction.verify_count = 1
    prediction.verify_score = 100
    prediction.status = 'success'      # 回溯审计只扫 success/failed 两种
    prediction.prediction_date = date(2026, 6, 1)
    prediction.target_date = date(2026, 6, 8)
    test_db.commit()

    result = PredictionVerifyService(test_db).rollback_invalid_verifications(
        dry_run=True, only_ids=[prediction.id])

    data = result['data']
    assert data['code_diverged'] == 1, data
    assert data['would_rollback'] == 0, \
        '按另一只基金缺数据就把这条结论判成"当年判错了" ⇒ 无关理由抹记录'
    test_db.refresh(prediction)
    assert prediction.is_correct is True


def test_name_lookup_cannot_resolve_back_to_an_unservable_code(test_db):
    """第 20 轮 MAJOR-1：第 2 步"按名字查 FundInfo"以前不过体检。

    `fund_info` 里"名字→代码"通常正好指回自带那只不可服务的基金 ⇒ 上一轮新加的改标门
    根本走不到（镜像 86% 的行死在这一步），而日志已经写了"改按板块重新解析标的"。
    """
    from src.models.database import FundInfo

    _mapping(test_db, sector_name='R20名字', fund_code='BAD006',
             fund_name='烂基金', is_fetchable=False)
    _mapping(test_db, sector_name='R20名字', fund_code='GOOD06',
             fund_name='机器人ETF', reviewed=True)
    # 名字与自带代码绑在一只"股票名挂在基金码"的档案上：这是镜像里 1392/1616 行的形状
    test_db.add(FundInfo(fund_code='BAD006', fund_name='烂基金'))
    test_db.commit()
    prediction = _prediction_with_code(test_db, 'BAD006', 'R20名字')
    prediction.fund_name = '烂基金'
    test_db.commit()

    service = PredictionVerifyService(test_db)
    code, _name = service.match_fund_for_prediction(prediction)
    assert code == 'GOOD06', \
        '按名字又解析回不可服务的 %s ⇒ 改标门不可达，日志在说谎' % code


def test_rollback_retags_drifted_rows_instead_of_abandoning_them(test_db):
    """第 20 轮 MAJOR-2：回溯审计扫到的漂移行，真跑时要就地改标。

    上一版只 `continue`（"交给 verify_prediction"），但本函数只扫已判行、
    到期队列只要 `is_correct IS NULL` ⇒ 那些行永远回不到改标那条腿。
    """
    from datetime import date

    _mapping(test_db, sector_name='R20接手', fund_code='BAD007',
             fund_name='某股票名挂在基金码', is_fetchable=False)
    _mapping(test_db, sector_name='R20接手', fund_code='GOOD07',
             fund_name='机器人ETF', reviewed=True)
    prediction = _prediction_with_code(test_db, 'BAD007', 'R20接手')
    prediction.is_correct = True
    prediction.actual_change = 1.23
    prediction.verify_count = 1
    prediction.verify_score = 100
    prediction.status = 'success'
    prediction.prediction_date = date(2026, 6, 1)
    prediction.target_date = date(2026, 6, 8)
    test_db.commit()
    from src.utils.blogger_stats import recalculate_blogger_stats
    recalculate_blogger_stats(test_db, prediction.blogger_id)

    service = PredictionVerifyService(test_db)
    dry = service.rollback_invalid_verifications(dry_run=True, only_ids=[prediction.id])
    assert dry['data']['code_diverged'] == 1
    assert '漂移' in dry['message'], dry['message']
    test_db.refresh(prediction)
    assert prediction.fund_code == 'BAD007', 'dry-run 不许动数据'

    wet = service.rollback_invalid_verifications(dry_run=False, only_ids=[prediction.id])
    assert wet['data']['code_diverged'] == 1, wet
    test_db.refresh(prediction)
    assert prediction.fund_code == 'GOOD07', '真跑完仍挂着不可服务的标的 = 没人管'
    assert prediction.is_correct is None and (prediction.verify_count or 0) == 0, \
        '旧标的判出来的结论必须一起清掉'
    from src.models.database import Blogger
    blogger = test_db.query(Blogger).filter_by(id=prediction.blogger_id).one()
    assert blogger.total_predictions == 0, \
        '清结论没重算统计列（第 19 轮 MAJOR-1 在第二个入口上重演）'
    assert [d['action'] for d in wet['data']['rollback_details']] == ['retagged']


def _two_drifted_plus_one_ok(test_db, sector='R21半成品'):
    """两条会漂移的行 + 一条正常行（正常那条用来触发循环里的异常）。"""
    from datetime import date

    _mapping(test_db, sector_name=sector, fund_code='BAD011',
             fund_name='某股票名挂在基金码', is_fetchable=False)
    _mapping(test_db, sector_name=sector, fund_code='BAD012',
             fund_name='另一只股票名挂在基金码', is_fetchable=False)
    _mapping(test_db, sector_name=sector, fund_code='GOOD11',
             fund_name='机器人ETF', reviewed=True)
    rows = []
    for code in ('BAD011', 'BAD012', 'GOOD11'):
        p = _prediction_with_code(test_db, code, sector)
        p.is_correct = True
        p.actual_change = 1.0
        p.verify_count = 1
        p.verify_score = 100
        p.status = 'success'
        p.prediction_date = date(2026, 6, 1)
        p.target_date = date(2026, 6, 8)
        rows.append(p)
    test_db.commit()
    return rows


def test_rollback_error_branch_leaves_no_half_committed_retag(test_db, monkeypatch):
    """第 21 轮 BLOCKER：共享函数不许中途提交，否则"未保存任何修改"是假话。

    改动前会红：`retag_if_drifted` 里无条件 `self.db.commit()` ⇒ 第三行抛错时
    前两行的改标 + 清结论已经落库，而函数返回 `success=False /"未保存任何修改"`。
    """
    import sqlalchemy as sa

    from src.models.database import Prediction

    rows = _two_drifted_plus_one_ok(test_db)

    def boom(self, **kw):
        raise RuntimeError('第 3 行的数据检查炸了')
    monkeypatch.setattr(PredictionVerifyService, '_check_fund_data_availability', boom)

    result = PredictionVerifyService(test_db).rollback_invalid_verifications(
        dry_run=False, only_ids=[r.id for r in rows])
    assert result['success'] is False, result

    # 从**表里**读：会话里的对象可能被中途提交过，那正是这个用例要抓的东西
    persisted = dict(test_db.execute(sa.select(Prediction.id, Prediction.fund_code)).all())
    assert persisted[rows[0].id] == 'BAD011', \
        '整批回滚了却留着已提交的改标 ⇒ 回执那句"未保存任何修改"是假话'
    assert persisted[rows[1].id] == 'BAD012', persisted
    verdicts = dict(test_db.execute(sa.select(Prediction.id, Prediction.is_correct)).all())
    assert verdicts[rows[0].id] is True and verdicts[rows[1].id] is True, \
        '旧结论也必须跟着回滚，不能只回滚一半'


def test_rollback_retag_is_recoverable_with_the_advertised_run_id(test_db):
    """第 21 轮 MAJOR-1：message 广告了 `--run-id`，日志就必须带那个 id。

    改动前会红：改标不传 run_id ⇒ `retag_prediction` 自动生成 `retag-verify_unservable_-<秒>`，
    与回执里广告的 run_id 两个集合互不相交 ⇒ 照着页面命令撤，一条都撤不回。
    """
    from src.models.database import PredictionChangeLog

    rows = _two_drifted_plus_one_ok(test_db, sector='R21还原')
    # 只放两条漂移行，第三行不动 ⇒ 不会走到数据检查
    service = PredictionVerifyService(test_db)
    result = service.rollback_invalid_verifications(
        dry_run=False, only_ids=[rows[0].id, rows[1].id])
    assert result['data']['code_diverged'] == 2, result

    run_id = result['data']['run_id']
    assert run_id and run_id in result['message']
    logged = test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id.in_([rows[0].id, rows[1].id])).all()
    assert logged, '改标没留痕'
    assert {l.run_id for l in logged} == {run_id}, \
        '日志里的 run_id 与广告出去的不是同一个 ⇒ restore_prediction_batch 撤不回来'
    assert {l.source for l in logged} == {'rollback_drifted_code'}, \
        '两个入口共用 source 就分不撤回溯那一批'
    # 解析出的新代码不许留着旧基金的名字
    test_db.refresh(rows[0])
    assert rows[0].fund_code == 'GOOD11'
    assert rows[0].fund_name in ('机器人ETF', ''), rows[0].fund_name


def test_retag_destination_is_not_an_unservable_row(test_db):
    """第 22 轮 MAJOR-2：SQL 粗筛只看列，"改标的去处"还得过一次唯一判据。

    `servable_predicate()` 把 `is_fetchable IS NULL` 一律当可服务，而"不可服务"还有
    第二个事实源（`evidence.identity.verdict`）⇒ 不过一遍的话，被 verdict 否掉的行
    会被当成"可服务的替代标的"交出去，正好把上一轮修的洞从另一头再打开。
    """
    import json

    ghost = _mapping(test_db, sector_name='R22去处', fund_code='GHOST9',
                     fund_name='名字像基金其实不是', reviewed=True, is_fetchable=None,
                     evidence=json.dumps({'identity': {'verdict': 'not_a_fund'}}))
    good = _mapping(test_db, sector_name='R22去处', fund_code='GOOD22',
                    fund_name='机器人ETF', reviewed=False)
    from src.services.sector_identity_audit import row_unservable, servable_predicate
    assert row_unservable(ghost) is True
    # 前提要真的断言：SQL 粗筛**确实**放行这一行，否则这个用例什么都没测
    # （以前写的是 `servable_predicate() is not None`，那永远为真，第 23 轮 MINOR）
    admitted = (test_db.query(SectorFundMapping)
                .filter(SectorFundMapping.id == ghost.id, servable_predicate())
                .first())
    assert admitted is not None, '前提不成立：粗筛已经拦住它了，那这条用例测不到判据分叉'

    _mapping(test_db, sector_name='R22去处', fund_code='BAD022',
             fund_name='某股票名挂在基金码', is_fetchable=False)
    prediction = _prediction_with_code(test_db, 'BAD022', 'R22去处')
    code, _name = PredictionVerifyService(test_db).match_fund_for_prediction(prediction)
    assert code == 'GOOD22', \
        '第 3 步把被 verdict 否掉的 GHOST9 当成了可服务的替代标的（SQL 粗筛说了算）'


def test_ghost_mapping_row_cannot_serve_or_poison_the_cache(test_db):
    """第 23 轮 MAJOR-1/2：`get_fund_by_sector` 与 `_save_fund_mapping` 也吃同一把尺子。

    上一轮我只给"改标去处"那一条加了 `row_unservable`，另外两处仍只信 SQL 粗筛
    （`servable_predicate()` 把 `is_fetchable IS NULL` 一律当可服务）。同一形态第三次复现。
    危害不止是返回一次：`get_fund_by_sector` 会把结果**写进进程内缓存**，
    此后这个进程里所有帖子分析都拿这只"不是基金"的标的去匹配。
    """
    import json

    from src.services.sector_fund_service import SectorFundService

    ghost = _mapping(test_db, sector_name='R23幽灵', fund_code='GHOST9',
                     fund_name='名字像基金其实不是', reviewed=True, is_fetchable=None,
                     evidence=json.dumps({'identity': {'verdict': 'not_a_fund'}}))
    SectorFundService(test_db)._cache = {}
    SectorFundService(test_db)._cache_loaded = False

    hit = SectorFundService(test_db).get_fund_by_sector('R23幽灵')
    assert hit is None, 'DB 兜底把幽灵行当可服务标的返回了：%s' % hit
    assert 'R23幽灵' not in SectorFundService._cache, \
        '返回被否掉的行还写进进程缓存 ⇒ 之后所有分析都吃这一只'

    # `_save_fund_mapping`：同一个粗筛，幽灵行会被当成"该板块已有映射"从而跳过写入
    test_db.refresh(ghost)
    assert ghost.reviewed is True
    from src.analyzer.llm_analyzer import LLMAnalyzer
    LLMAnalyzer._save_fund_mapping(LLMAnalyzer.__new__(LLMAnalyzer), 'R23幽灵',
                                   '512170', '医疗ETF', reviewed=False, db=test_db)
    fresh = test_db.query(type(ghost)).filter(
        type(ghost).sector_name == 'R23幽灵',
        type(ghost).fund_code == '512170').first()
    assert fresh is not None, \
        '被 verdict 否掉的幽灵行被当成"这板块已经有映射了"，正确的标的行根本没写进去'
    assert fresh.reviewed is not True, '机器写入不许自带"已审查"'


# 谁可以盖"老板已确认"（＝身份体检豁免）。这张名单只许变短。
# 三条页面路径的**行为**由本文件上面那几条用例钉着；这条闸管的是"名单之外不许再多一处"。
# 第 44 轮 A 席 M-3：AGENTS.md 写着"豁免每一条来源都要显式令牌"，
# 但仓库里没有任何机器把这句话说成判据（同一条通病的第三次：`is_correct` 与 `fund_code`
# 的"唯一入口"当初也是只写在文档里，后来各自多出一个入口）。
# **这里不抄"共几条"**（第 45 轮我自己就加出了一处当时文档没登记的）：
# 真值就是下面这两张表的键集合，加一处不登记 ⇒ 本文件那条判据红。
IMMUNITY_GRANT_SITES = {
    # 键是 (文件, 最内层函数)，值是**那一处里的授予条数**（第 45 轮 A-m4：只按 (文件, 函数)
    # 收录时，同一个函数里再写一行授予不会新增条目 ⇒ 名单看不出"多了一处"）。
    ('src/services/sector_fund_service.py', 'mark_reviewed_by_id'): 2,    # 页面逐行审查
    ('src/services/sector_fund_service.py', 'batch_mark_reviewed'): 2,    # 页面批量审查
    ('src/services/sector_fund_service.py', 'update_mapping'): 2,         # 页面编辑保存
    ('scripts/seed_owner_proxies.py', 'main'): 2,                        # `--owner-confirm SEED-PROXY`
}
_IMMUNITY_FIELDS = {'owner_locked': (True,), 'reviewed_by': ('owner',)}


def _looks_like_grant(field, node):
    """一个 AST 表达式**本身**是不是"写死的授予值"（真值语义，不是字面量 `True` 一种写法）。

    第 46 轮 A-M6 / B-M10：库里读的是 `if getattr(row, 'owner_locked', None)` ——
    **真值即免疫**，而登记表以前只认 `True` 字面量。于是 `= 2`、`= not False`、
    `= x or True`、`def grant(row, lock=True)` 都能静默新增一条豁免来源。
    反过来，"把已有的值搬过来"（`bool(other.owner_locked)`、`payload.get(...)`）
    仍然**不算**授予 —— 那是备份/序列化，误伤它等于把这条闸变成全仓噪声。
    """
    import ast
    if isinstance(node, ast.Constant):
        value = node.value
        if field == 'owner_locked':
            return value is True or (isinstance(value, int) and not isinstance(value, bool)
                                     and value != 0)
        return isinstance(value, str) and value.lower() == 'owner'
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = node.operand
        return isinstance(inner, ast.Constant) and inner.value in (False, 0, '', None)
    return False

# 字段名是变量、从 AST 看不见"写的是哪一列"的站点。这张表**不是免检名单**：
# 每条都要写明凭什么被相信，而且依据要能被别的用例复核（括号里点了文件名）。
# 新增一条而没有依据 ⇒ `test_only_the_registered_places_can_grant_owner_immunity` 变红。
IMMUNITY_OPAQUE_SITES = {
    ('src/api/routes/config.py', '_audit_apply_row'):
        '载荷先过 `_clean_row`：`owner_locked=True` 与 `reviewed_by="owner"` 在那里被剔掉，'
        '行为判据在 tests/unit/test_sector_mapping_audit_import.py',
    ('scripts/sweep_sector_mappings.py', 'restore'):
        '清单里的老板署名/锁定**默认不还原**，要还原得显式 --restore-owner-immunity；'
        '行为判据在 tests/unit/test_sweep_restore_owner_immunity.py',
}


def _grants_immunity(root):
    """AST 扫"把老板署名/锁定**写成授予值**"的代码点：`{(文件, 函数): 条数}` + 看不清的集合。

    认的写法（**完整清单就是用例里那个 `shapes` 字典**——加一种拼写就去那儿加一个样品，
    别在这里抄条数：上一轮写"三种"、这轮写"七种"，两种都没跟上代码本身）：
      ① `row.owner_locked = True`；② `{'reviewed_by': 'owner'}`（字典字面量）；
      ③ `setattr(row, 'owner_locked', True)` **以及** `object.__setattr__(row, …)`；
      ④ `stmt.values(owner_locked=True)` / `update(reviewed_by='owner')` —— **关键字参数**，
         这正是本仓在用的 SQLAlchemy 批量写法（`src/api/main.py:528 sa_insert(...).values(**…)`)；
      ⑤ 值来自模块常量（`OWNER = True` 然后 `row.owner_locked = OWNER`）；
      ⑥ 值是三目 / `x or True` / `bool(True)` —— 有一臂写死授予值就算；
      ⑦ 带类型标注的赋值、下标赋值（`row['reviewed_by'] = 'owner'`）、`setdefault`；
      ⑧ 裸 SQL：`UPDATE sector_fund_mapping SET owner_locked = true`（列名与值都在字符串里）；
      ⑨ **真值语义**：库里读的是 `if getattr(row, 'owner_locked', None)` ⇒ 真值即免疫，
         所以 `= 2`、`= not False`、`def grant(row, lock=True)` 都算写死授予（第 46 轮 A-M6/B-M10）。
    只认"看得见的授予值"：`payload.get('owner_locked')`、`bool(m.owner_locked)` 这类
    **搬运已有值**的写法不算授予（那条腿由
    `test_purge_junk_funds.py::test_restore_refuses_to_regrant_owner_immunity` 钉）。
    值来路看不见（跨模块常量、外层变量、`{**payload}`）⇒ **也不算"写死授予"**，
    这一档归载荷驱动（`_clean_row` 剔列 + 行为判据），这里不另开登记通道 —— 第一版开了，
    当场把 5 处正常代码变成"待解释"，那张表就从"要依据"退化成"盖章"，正是要防的事。
    字段名本身是变量时（`setattr(row, k, v)`）看不见写的是哪一列 ⇒ 不猜"没事"，
    落进第二个返回值 `opaque`，由用例要求它要么消失、要么写明去向。
    """
    import ast
    found, opaque = {}, set()
    for py in sorted(root.rglob('*.py')):
        # 只放过 `__pycache__` 与本机草稿。以前这里写的是 `startswith(('_tmp_', '__'))`
        # ⇒ 全仓每个 `__init__.py` 整族免检（第 45 轮 A-M6：`src/services/__init__.py`、
        # `src/fund/__init__.py` 正是本仓真放代码的地方 —— 与第 39 轮"前缀整族豁免"同一课）
        if '__pycache__' in str(py) or py.name.startswith('_tmp_'):
            continue
        try:
            tree = ast.parse(py.read_text(encoding='utf-8', errors='replace'))
        except SyntaxError:
            found[('%s（解析不了）' % py.name, '<unknown>')] = 1  # fail-closed：坏文件按可疑处理
            continue
        try:
            rel = str(py.relative_to(root.parent)).replace('\\', '/')
        except ValueError:
            rel = str(py).replace('\\', '/')

        # 模块级常量：`OWNER = True` 这种"给授予值起个名字"的写法
        granted_by_name = {}
        for st in tree.body:
            if isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant):
                for t in st.targets:
                    if isinstance(t, ast.Name):
                        for field, vals in _IMMUNITY_FIELDS.items():
                            if st.value.value in vals:
                                granted_by_name.setdefault(t.id, set()).add(field)

        # 参数默认值也算"写死的授予值"：`def grant(row, lock=True): row.owner_locked = lock`
        # 与 `row.owner_locked = True` 是同一件事，只是隔了一个名字（第 46 轮 B-M10）。
        param_defaults = set()
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            positional = list(fn.args.args)[len(fn.args.args) - len(fn.args.defaults):]
            for name, default in zip(positional, fn.args.defaults):
                for field in _IMMUNITY_FIELDS:
                    if _looks_like_grant(field, default):
                        param_defaults.add(name.arg)
            for kw, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
                for field in _IMMUNITY_FIELDS:
                    if default is not None and _looks_like_grant(field, default):
                        param_defaults.add(kw.arg)
        # 裸 SQL 的授予：`UPDATE sector_fund_mapping SET owner_locked = true`
        # 列名与值都在一条字符串里，AST 的"赋值/字典/关键字参数"三条都看不见（B-M10 第 6 条）
        _RAW_SQL_GRANT = re.compile(
            r"(owner_locked\s*=\s*(?:true|1|'1'|\"1\"))|(reviewed_by\s*=\s*'owner')", re.I)

        def _literal_grant(field, node, nested=False):
            """这个表达式里有没有**看得见**的授予值（常量 / 三目的某一臂 / 模块常量）。

            第 46 轮 A-M6 / B-M10 把"看得见"扩到真值语义：`row.owner_locked = 2`、
            `= not False`、`= x or True` 都是**写死要授予**，只认 `True` 字面量等于给
            "换个真值写法"留门；反过来，值来路看不见（跨模块常量、别的文件的配置）时
            不猜"没事"，落进 `opaque` 让用例问一句。
            """
            if isinstance(node, (ast.Constant, ast.UnaryOp)):
                return _looks_like_grant(field, node)
            if isinstance(node, ast.IfExp):           # 有一臂写死授予值就算（另一臂可以是 None）
                return (_literal_grant(field, node.body, True)
                        or _literal_grant(field, node.orelse, True))
            if isinstance(node, ast.BoolOp):          # `x or True` 的右臂就是写死的授予
                return any(_literal_grant(field, v, True) for v in node.values)
            if isinstance(node, ast.Call):            # `bool(True)`：包一层不改变"写死"这件事
                fn = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
                if fn in ('bool', 'int') and node.args:
                    return _literal_grant(field, node.args[0], True)
            if isinstance(node, ast.Name):
                if field in granted_by_name.get(node.id, ()):
                    return True
                if node.id in param_defaults:         # `def grant(row, lock=True)`
                    return True
                # 名字来路看不见（跨模块常量、外层变量）⇒ **不算"写死授予值"**，也不新开一条
                # opaque 通道：本仓库里这样的写法今天就有五处（`row.owner_locked = 某布尔`），
                # 把它们登记进 `IMMUNITY_OPAQUE_SITES` 等于给这张表盖章——而"盖章"正是这张表
                # 存在的理由要防的事（第 46 轮我自己第一版就是这么把 5 处正常代码变成"待解释"）。
                # 这一档的真实归属是**载荷驱动**那条路：值从清单/请求体来 ⇒
                # 由 `_clean_row` 剔列 + `test_sector_mapping_audit_import.py` 的行为判据管，
                # 与 purge/sweep 的还原腿同一档。
            return False

        def _enclosing(lineno):
            """包住这一行的**最内层**函数名（挑最里层，否则同一文件里的名字会串位）。"""
            best, best_line = '<module>', -1
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                        and n.lineno <= lineno <= (n.end_lineno or n.lineno) \
                        and n.lineno > best_line:
                    best, best_line = n.name, n.lineno
            return best

        def _hit(lineno):
            key = (rel, _enclosing(lineno))
            found[key] = found.get(key, 0) + 1

        # 这个文件是不是"做豁免生意"的（字段名以字面量出现过）。只在这种文件里，
        # "字段名是变量的 setattr"才值得问一句 —— 否则每个 `setattr(obj, name, v)` 都会红。
        mentions_immunity = any(isinstance(c, ast.Constant) and c.value in _IMMUNITY_FIELDS
                                for c in ast.walk(tree))

        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):     # 带类型标注的赋值同一条路
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for tgt in targets:
                    if isinstance(tgt, ast.Attribute) and tgt.attr in _IMMUNITY_FIELDS \
                            and _literal_grant(tgt.attr, node.value):
                        _hit(node.lineno)
                    elif isinstance(tgt, ast.Subscript) and node.value is not None:
                        # `row['reviewed_by'] = 'owner'`：列名是**下标的键**，不是被索引的对象
                        key = tgt.slice
                        if isinstance(key, ast.Constant) and key.value in _IMMUNITY_FIELDS \
                                and _literal_grant(key.value, node.value):
                            _hit(node.lineno)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                # 裸 SQL 的授予（列名和值都在一条字符串里，赋值/字典/关键字参数三条都看不见）
                strs = [c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant)
                        and isinstance(c.value, str)]
                if any(_RAW_SQL_GRANT.search(s or '') for s in strs):
                    _hit(node.lineno)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if key is None or not isinstance(key, ast.Constant):
                        continue        # `{**payload}` 的散开键看不见列名 ⇒ 归载荷驱动那一档
                                        # （这里不新开 opaque 通道，理由见 `_literal_grant` 上面）
                    if isinstance(key, ast.Constant) and key.value in _IMMUNITY_FIELDS \
                            and _literal_grant(key.value, value):
                        _hit(node.lineno)
            elif isinstance(node, ast.Call):
                fname = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
                if fname in ('setattr', '__setattr__') and len(node.args) == 3:
                    field = node.args[1]
                    if isinstance(field, ast.Constant) and field.value in _IMMUNITY_FIELDS \
                            and _literal_grant(field.value, node.args[2]):
                        _hit(node.lineno)
                    elif not isinstance(field, ast.Constant) and mentions_immunity:
                        # 字段名是变量（`[setattr(row, k, v) for k, v in pairs]`）：
                        # 看不见写的是哪一列 ⇒ 不猜"没事"，也不硬算成授予，交给用例问一句
                        opaque.add((rel, _enclosing(node.lineno)))
                if fname in ('setdefault', '__setitem__') and len(node.args) >= 2:
                    # `payload.setdefault('owner_locked', True)` / `row['owner_locked'] = True`
                    # 走的都是"字典式写入"，与 `setattr` 同一件事（第 46 轮 A-M6 第 4 条）。
                    # 列名是变量的那一种**不另开 opaque 通道**——全仓的 `setdefault(k, v)`
                    # 都会被卷进来（第一版就抓到 3 处正常代码），理由见 `_literal_grant` 上面。
                    field = node.args[0]
                    if isinstance(field, ast.Constant) and field.value in _IMMUNITY_FIELDS \
                            and _literal_grant(field.value, node.args[1]):
                        _hit(node.lineno)
                for kw in (node.keywords or []):
                    # `.values(owner_locked=True)` / `update(reviewed_by='owner')` 这一族
                    if kw.arg in _IMMUNITY_FIELDS and _literal_grant(kw.arg, kw.value):
                        _hit(node.lineno)
            elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                # 批量写的数据源：`(("owner_locked", True),)` —— 字段名与授予值成对出现，
                # 即使真正的 `setattr` 在别处（列表推导里），这一对本身就是"写死要授予"。
                strs = [c.value for c in ast.walk(node) if isinstance(c, ast.Constant)]
                if any(isinstance(s, str) and s in _IMMUNITY_FIELDS for s in strs) and any(
                        isinstance(v, bool) and v or v == 'owner' for v in strs):
                    _hit(node.lineno)
    return found, opaque


def test_only_the_registered_places_can_grant_owner_immunity(tmp_path):
    """"豁免只有这几条来源"从今天起有机器钉着：新增一个授予点就地变红。

    名单的值是**条数**（第 45 轮 A-m4：以前键只有 (文件, 函数)，同一个函数里再写一行授予
    不会新增任何条目 ⇒ 名单看不出"多了一处"）。
    控制断言分三半：
    ① 仓库里必须**真的**找得到已登记的这几处（0 命中 = 尺子坏了，不是"很干净"）；
    ② 临时目录现造六种写法（属性赋值 / 字典字面量 / `setattr` / `object.__setattr__` /
       SQLAlchemy 的 `.values(owner_locked=True)` / 模块常量 / 三目），每一种都必须被点名；
    ③ 一处"搬运已有值"（备份、序列化）不许被点名，否则判据过宽、正常代码全都红。
       另有一处"字段名是变量"的写法落进 `opaque`（不猜它没事，也不硬算成授予）。
    """
    repo = ROOT
    src_found, src_opaque = _grants_immunity(repo / 'src')
    scr_found, scr_opaque = _grants_immunity(repo / 'scripts')
    found, opaque = dict(src_found), set(src_opaque) | set(scr_opaque)
    found.update(scr_found)
    assert found, '一条授予点都没找到 ⇒ `_grants_immunity` 的判据形状与代码脱节了，这条是空判'
    assert set(found) <= set(IMMUNITY_GRANT_SITES), (
        '新增了写死"老板已确认"的代码点：%s ⇒ 体检豁免只能由页面上的显式确认或已登记的脚本旗子'
        '给出；真要加一条，先在 AGENTS.md 把来源那段话改对（那一段现在不抄条数，抄的是这两张表），'
        '并挂上它自己的显式令牌与用例'
        % sorted(set(found) - set(IMMUNITY_GRANT_SITES)))
    assert found == IMMUNITY_GRANT_SITES, (
        '登记名单与实际授予点对不上了（多：%s / 少：%s / 条数不符：%s）⇒ '
        '名单里的条目要么已经换掉了就删掉，要么是判据漏了形状'
        % (sorted(set(found) - set(IMMUNITY_GRANT_SITES)),
           sorted(set(IMMUNITY_GRANT_SITES) - set(found)),
           sorted((k, v, IMMUNITY_GRANT_SITES.get(k)) for k, v in found.items()
                  if IMMUNITY_GRANT_SITES.get(k) != v)))
    # "字段名是变量的 setattr"从 AST 看不见写的是哪一列 ⇒ 既不静默放过，也不硬算成授予：
    # 每一条都得登记在下面这张表里并写明**凭什么被相信**（上游剔掉了授予值且有行为判据 /
    # 默认拒绝、要显式旗子）。新增一条而没人写依据 ⇒ 这条变红。
    assert opaque <= set(IMMUNITY_OPAQUE_SITES), (
        '多出这些"字段名是变量"的授予写法：%s ⇒ 先看清楚它到底能写哪一列，再登记并写明依据'
        % sorted(opaque - set(IMMUNITY_OPAQUE_SITES)))
    assert set(IMMUNITY_OPAQUE_SITES) <= opaque, (
        '登记过的动态 setattr 站点不见了：%s ⇒ 代码里真删掉了就把条目删掉，'
        '别留一条谁也复核不了的旧名单' % sorted(set(IMMUNITY_OPAQUE_SITES) - opaque))
    for key, reason in IMMUNITY_OPAQUE_SITES.items():
        assert reason.strip(), '登记了 %s 却没写依据 ⇒ 这张表成了盖章' % (key,)

    pkg = tmp_path / 'sample'
    pkg.mkdir()
    (pkg / '__init__.py').write_text(
        'def grant(row):\n    row.owner_locked = True\n', encoding='utf-8')
    shapes = {
        '_a_attr.py': 'def grant(row):\n    row.owner_locked = True\n',
        '_b_dict.py': 'def grant(row):\n    return {"reviewed_by": "owner", "id": row.id}\n',
        '_c_setattr.py': 'def grant(row):\n    setattr(row, "reviewed_by", "owner")\n',
        '_d_object_setattr.py': 'def grant(row):\n'
                                '    object.__setattr__(row, "owner_locked", True)\n',
        '_e_values_kw.py': 'def grant(stmt):\n'
                           '    return stmt.values(owner_locked=True, reviewed_by="owner")\n',
        '_f_module_const.py': 'OWNER = True\n\n\ndef grant(row):\n    row.owner_locked = OWNER\n',
        '_g_ternary.py': 'def grant(row, ok):\n'
                         '    row.reviewed_by = "owner" if ok else None\n',
        # 字段名是变量的 setattr（列表推导里批量授予）⇒ 要么点名成授予，要么落进 opaque，
        # 两条都不许静默走过
        '_i_dynamic_field.py': 'PAIRS = (("owner_locked", True),)\n\n'
                               'def grant(row):\n'
                               '    return [setattr(row, k, v) for k, v in PAIRS]\n',
        # ↓ 第 46 轮 A-M6 / B-M10：真值语义与"隔着一次写入"的四种日常写法
        '_k_truthy_int.py': 'def grant(row):\n    row.owner_locked = 2\n',
        '_l_not_false.py': 'def grant(row):\n    row.owner_locked = not False\n',
        '_m_or_arm.py': 'def grant(row, lock):\n    row.owner_locked = lock or True\n',
        '_n_annassign.py': 'def grant(row):\n    row.owner_locked: bool = True\n',
        '_o_default_arg.py': 'def grant(row, lock=True):\n    row.owner_locked = lock\n',
        '_p_setdefault.py': 'def grant(payload):\n    payload.setdefault("owner_locked", True)\n',
        '_q_subscript.py': 'def grant(row):\n    row["reviewed_by"] = "owner"\n',
        '_r_raw_sql.py': 'import sqlalchemy as sa\n\n'
                         'def grant(db):\n'
                         '    db.execute(sa.text("UPDATE sector_fund_mapping '
                         'SET owner_locked = true WHERE id = 7"))\n',
        # 反向对照：与豁免无关的文件里，`setattr(obj, name, value)` 不该被问一句
        '_j_unrelated_setattr.py': 'def shape(obj, name, value):\n'
                                   '    return setattr(obj, name, value)\n',
        # 反向样品：把已有的值搬过去（备份/序列化），不是"授予"
        '_h_moves_values.py': 'def grant(row, other):\n'
                              '    row.owner_locked = bool(other.owner_locked)\n'
                              '    return {"reviewed_by": other.reviewed_by}\n',
    }
    for name, body in shapes.items():
        (pkg / name).write_text(body, encoding='utf-8')
    made, made_opaque = _grants_immunity(pkg)
    for name in ('_a_attr.py', '_b_dict.py', '_c_setattr.py', '_d_object_setattr.py',
                 '_e_values_kw.py', '_f_module_const.py', '_g_ternary.py',
                 '_k_truthy_int.py', '_l_not_false.py', '_m_or_arm.py', '_n_annassign.py',
                 '_o_default_arg.py', '_p_setdefault.py', '_q_subscript.py', '_r_raw_sql.py'):
        assert any(name in f for f, _n in made), \
            '这些写法认不出 %s ⇒ 换这种拼写就能绕过这道棘轮' % name
    assert made.get(('sample/__init__.py', 'grant')) == 1, \
        '`__init__.py` 整族被豁免 ⇒ 而本仓的包 `__init__` 是真放代码的地方（A-M6）：%s' % sorted(made)
    assert not any('_h_moves_values.py' in f for f, _n in made), \
        '"把已有值搬过去"被判成授予 ⇒ 判据过宽，备份/序列化代码都会红：%s' % sorted(made)
    assert any('_i_dynamic_field.py' in pair[0] for pair in made_opaque), \
        '字段名是变量的 setattr 被静默放过 ⇒ 既不算授予也不报错，正是最该问一句的形状：%s' \
        % sorted(made_opaque)
    assert not any('_j_unrelated_setattr.py' in pair[0] for pair in made_opaque), \
        '与豁免无关的 `setattr(obj, name, v)` 也被问一句 ⇒ 判据过宽，全仓到处是假警报：%s' \
        % sorted(made_opaque)
    # 同一函数里再写一行授予，条数必须变（这就是"名单看不出多了一处"的那个洞）
    (pkg / '_e_values_kw.py').write_text(
        'def grant(stmt, row):\n'
        '    row.owner_locked = True\n'
        '    return stmt.values(owner_locked=True, reviewed_by="owner")\n', encoding='utf-8')
    again, _ = _grants_immunity(pkg)
    assert again[('sample/_e_values_kw.py', 'grant')] >= 3, \
        '同一函数里多写一处授予，名单上的条目数却没变 ⇒ 键的粒度还是太粗：%s' % again
