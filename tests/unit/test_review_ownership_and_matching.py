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
    assert servable_predicate() is not None, '前置：SQL 侧那道粗筛确实放行它'

    _mapping(test_db, sector_name='R22去处', fund_code='BAD022',
             fund_name='某股票名挂在基金码', is_fetchable=False)
    prediction = _prediction_with_code(test_db, 'BAD022', 'R22去处')
    code, _name = PredictionVerifyService(test_db).match_fund_for_prediction(prediction)
    assert code == 'GOOD22', \
        '第 3 步把被 verdict 否掉的 GHOST9 当成了可服务的替代标的（SQL 粗筛说了算）'
