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
                        lambda self, p: (p.fund_code, p.fund_name))
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


def test_a_live_fund_with_a_history_gap_is_never_closed(test_db, monkeypatch):
    """反面对照（最贵的一格）：库里这只基金**最近还在发净值**、只是缺中间那段 ⇒ 只锁不关。

    少了这道对照，"我们自己几天没同步"就会被读成"产品停更"，
    于是批量验证会替老板把他本可以验证的预测关掉。
    """
    from src.models.database import FundHistory

    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY - timedelta(days=1), fund_code='LIVE01')
    test_db.add(FundHistory(fund_code='LIVE01', nav_date=TODAY - timedelta(days=2),
                            nav=1.5))
    test_db.commit()
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history', 'message': '这段没有'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['closed_as_unverifiable'] is False
    assert p.is_deleted is False, '最近还在发净值的产品不许被当成停更关掉'
    assert p.next_verify_date == TODAY + timedelta(days=unverifiable_retry_days())


def test_the_second_structural_answer_on_a_dead_target_closes_the_row(test_db, monkeypatch):
    """第二次答"没有" + 库里最后一条净值早于窗口 ⇒ 收进回收站：原因写清、可恢复、不写结论。"""
    from src.models.database import FundHistory, PredictionChangeLog

    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY - timedelta(days=1), fund_code='DEAD01')
    test_db.add(FundHistory(fund_code='DEAD01', nav_date=TODAY - timedelta(days=400),
                            nav=1.02))
    test_db.commit()
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history', 'message': '这段没有'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['closed_as_unverifiable'] is True and result['held_until'] is None
    assert p.is_deleted is True and p.deleted_by == 'system'
    assert '回收站' in p.delete_reason and '不计入准确率' in p.delete_reason
    assert p.is_correct is None, '关闭不是判错：一个结论都不许写'
    # 留痕：台账里能翻到这一笔是谁动的、动之前长什么样
    logs = test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id == p.id).all()
    assert logs and logs[-1].source == 'system'
    # 活跃面彻底干净：既不在到期队列，也不在"结构性不可验"那一档
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}
    assert p.id not in {x.id for x in filter_unverifiable(test_db, as_of=TODAY)}


def test_a_refused_close_still_puts_the_lock_back(test_db, monkeypatch):
    """该关但**关不成**（归档咽喉拒了）⇒ 必须退回上锁，不许"既不锁也不报"（第 52 轮 A-6）。

    旧写法是一条 `else`：只有"判不出来关"那一支才上锁。于是咽喉拒收时（行已不在活跃面、
    并发被别的批次拿走等）这一轮什么都不写 ⇒ 它明天仍在到期队列里，每一批重问一次、
    每一次又试关一次，而回执上既没有 `held_until` 也没有 `closed_as_unverifiable`，
    页面上看不出这一行今天被问过。
    """
    from src.models.database import FundHistory
    from src.services.prediction_service import PredictionService

    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY - timedelta(days=1), fund_code='REF01')
    test_db.add(FundHistory(fund_code='REF01', nav_date=TODAY - timedelta(days=400),
                            nav=1.02))
    test_db.commit()
    monkeypatch.setattr(PredictionService, 'close_as_unverifiable',
                        lambda self, pid, note: False, raising=True)
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history', 'message': '这段没有'})

    result = svc.verify_prediction(p.id)

    held = TODAY + timedelta(days=unverifiable_retry_days())
    assert result['closed_as_unverifiable'] is False
    assert result['held_until'] == held.isoformat(), '关不成又不上锁 ⇒ 这批行每天都白跑一遍'
    test_db.refresh(p)
    assert p.is_deleted is False and p.next_verify_date == held
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}


def test_a_degenerate_endpoint_holds_the_row_out_of_the_due_queue(test_db, monkeypatch):
    """目标日落在休市日（起点与终点是同一条净值）也是**问过之后**的结构性结论。

    2026-09-27 在镜像上量到的形状：`512680 / 512170` 两条 `07-10 → 07-11`（周六），
    验证器每次都答 `same_nav_endpoint` ⇒ 任务 #8 那把锁当时只认 `no_source_history`，
    这两条就永远坐在「待验证到期」里，每次点按钮都白跑它们一遍。
    """
    p = _seed(test_db, target=TODAY - timedelta(days=3), fund_code='END01')
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint',
        'message': '目标日及之前只有那一条净值，起点与终点是同一条'})

    result = svc.verify_prediction(p.id)

    assert result['success'] is False
    held = TODAY + timedelta(days=unverifiable_retry_days())
    assert result['held_until'] == held.isoformat(), '退化端点没被锁 ⇒ 到期队列里永远有它'
    test_db.refresh(p)
    assert p.next_verify_date == held and p.is_correct is None
    assert classify(p, as_of=TODAY) == UNVERIFIABLE
    assert p.id not in {x.id for x in filter_due_for_verify(test_db, as_of=TODAY)}


def test_a_first_ask_never_closes_even_when_the_fund_has_stopped(test_db, monkeypatch):
    """第一次问出来只许上锁 —— 排期日期在**过去**不等于"上一轮是结构性结论"。

    2026-09-27 镜像实测抓到的：`should_close_as_stale_target` 的第①条以前只看
    "这根日期过了没有"，而到期队列里的行那根日期本来就是创建时排出来的（必然 ≤ 目标日 ≤ 今天）
    ⇒ 任何一条第一次判出结构性结论、且第②条也成立，就会被**直接**收进回收站，
    "问过两次"那句承诺当场为假。真正的凭据是**这根日期落在目标日之后**：
    创建排期被 `test_the_creation_schedule_never_writes_a_date_after_the_target` 夹在目标日之前，
    所以只有上一轮的锁会写在目标日之后。
    """
    from src.models.database import FundHistory

    target = TODAY - timedelta(days=3)
    p = _seed(test_db, target=target, next_verify=target, fund_code='DEAD01')
    test_db.add(FundHistory(fund_code='DEAD01', nav_date=target - timedelta(days=400),
                            nav=1.02))
    test_db.commit()
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'no_source_history', 'message': '这段没有'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['closed_as_unverifiable'] is False, '第一次问出来就关 ⇒ "问过两次"是句空话'
    assert p.is_deleted is False
    assert p.next_verify_date == TODAY + timedelta(days=unverifiable_retry_days())


def test_a_degenerate_endpoint_is_never_closed_even_on_the_second_ask(test_db, monkeypatch):
    """退化端点**只锁不关**，第二次问出来也不关（第 52 轮 A-1 逼出来的边界）。

    我上一版给它配了"库里目标日之后已有净值 ⇒ 那天不是交易日"这条关闭证据，被实测驳回：
    同一句证据还有第二种来路 —— **这只标的自己有数据洞**，补拉就能填。库里分得开吗？分不开
    （2026-09-27 镜像逐日数过：真休市的 2026-07-11 周六全库 1 行（货币基金照发），
    交易日的 2026-09-08 有 202 行）。关错的代价是"永久消失 + 一句假原因"，
    锁的代价只是"隔几天再问一次" ⇒ 宁可一直问。
    """
    p = _seed(test_db, target=TODAY - timedelta(days=3),
              next_verify=TODAY - timedelta(days=1), fund_code='END02')
    # 把**另外**两条关闭证据都摆足（库里末条净值远早于窗口起点 + 上一轮的锁已过点）：
    # 少了这两行，这条用例拒的是"净值覆盖不了窗口"，与"退化端点能不能关"这件事无关
    # —— 第 52 轮的变异 M2（把 same_nav_endpoint 塞进可关名单）在它上面打绿，就是这么暴露的。
    from src.models.database import FundHistory
    test_db.add(FundHistory(fund_code='END02', nav_date=TODAY - timedelta(days=400),
                             nav=1.02))
    test_db.commit()
    svc = _service(test_db, monkeypatch, {
        'available': False, 'reason': 'same_nav_endpoint', 'message': '那天没有独立净值'})

    result = svc.verify_prediction(p.id)

    test_db.refresh(p)
    assert result['closed_as_unverifiable'] is False, '这条关不得：证据分不清休市与数据洞'
    assert p.is_deleted is False
    assert result['held_until'] == (TODAY + timedelta(days=unverifiable_retry_days())).isoformat()


def test_the_structural_reasons_are_all_dispositioned(test_db):
    """「结构性」名单与"能不能关"两张名单必须拼得上、不重叠，加一档不登记就红。

    反空判（B-1 那一族）：把第三档塞进 `STRUCTURAL_VERDICT_REASONS` 而不登记处置，
    这条必须立刻判不符 ⇒ 它钉的是"以后加档的人别忘了回答要不要关"。
    """
    structural = set(lc.STRUCTURAL_VERDICT_REASONS)
    closable = set(lc.CLOSABLE_VERDICT_REASONS)
    lock_only = set(lc.LOCK_ONLY_VERDICT_REASONS)

    assert closable & lock_only == set(), '一档不许同时"能关"又"只锁"'
    assert structural == closable | lock_only, (
        '新增结构性结论要登记处置方式（能关 / 只锁），别让它掉到 if 的默认分支上：%s'
        % (structural ^ (closable | lock_only)))
    # 关闭那条路只认登记过的档；其余一律 False（第一次问出来之外也不许顺手关）
    for reason in lock_only:
        assert lc.should_close_as_stale_target(
            verdict_reason=reason, previous_hold=TODAY - timedelta(days=1),
            target_date=TODAY - timedelta(days=3),
            local_latest_nav=TODAY - timedelta(days=400),
            window_start=TODAY - timedelta(days=3), today=TODAY) is False

    tampered = structural | {'a_reason_nobody_disposed_of'}
    assert tampered != closable | lock_only, '空判对照：塞一档进来必须被上面那条发现'


def test_only_one_place_decides_whether_a_failure_is_structural():
    """"哪些失败算结构性"这句话只许 `is_structural_verdict` 一处回答（第 52 轮 B-1）。

    仓库规矩：每句"唯一出处"都要有会红的判据，否则下一轮就多一个出处。
    判的是 AST 里的**比较运算**（`reason == 'xxx'`、`xxx in (…)`）——
    写下结论那一处（`reason='same_nav_endpoint'` 是关键字参数）与标签表
    （`REASON_LABELS` 的字典键）都不是"门槛"，不误伤。
    """
    import ast as _ast
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    reasons = set(lc.STRUCTURAL_VERDICT_REASONS)

    def reason_gates(node):
        hits = []
        for cmp_node in _ast.walk(node):
            if not isinstance(cmp_node, _ast.Compare):
                continue
            operands = [cmp_node.left] + list(cmp_node.comparators)
            literals = {o.value for o in operands
                        if isinstance(o, _ast.Constant) and isinstance(o.value, str)}
            # 抄第二把尺子有两种拼法：`reason == 'xxx'` 与 `reason in ('xxx', 'yyy')`。
            # 只判前一种的话，后者（把整张名单原样抄一遍）结构上不可能红（第 52 轮我自己
            # 写第一版时就是这样，是变异 M6 打绿之后补的）。
            for o in operands:
                if isinstance(o, (_ast.Tuple, _ast.List, _ast.Set)):
                    literals |= {e.value for e in o.elts
                                 if isinstance(e, _ast.Constant) and isinstance(e.value, str)}
            picked = literals & reasons
            if picked and any(not isinstance(o, _ast.Constant) for o in operands):
                hits.append(cmp_node.lineno)      # 一侧是字面量、另一侧是变量 ⇒ 拿它当门槛
        return hits

    offenders = []
    scanned = 0
    for base, _dirs, files in os.walk(os.path.join(root, 'src')):
        for name in files:
            if not name.endswith('.py'):
                continue
            rel = os.path.relpath(os.path.join(base, name), root).replace(os.sep, '/')
            scanned += 1
            for fn in [n for n in _ast.walk(_ast.parse(
                    open(os.path.join(root, rel), encoding='utf-8').read()))
                    if isinstance(n, _ast.FunctionDef)]:
                for lineno in reason_gates(fn):
                    offenders.append('%s:%d' % (rel, lineno))
    assert scanned > 50, '扫描面塌了（只数到 %d 个文件）⇒ 这条判据结构上不可能红' % scanned
    assert offenders == [], (
        '结构性判据被抄成第二把尺子：%s ⇒ 加一档要改 `STRUCTURAL_VERDICT_REASONS` 与两张处置名单'
        % offenders)
    # 反空判：现造两处违规都要被点名（一种比较、一种"把名单整个抄一遍"）
    fake = _ast.parse("def gate(reason):\n"
                      "    return reason == 'no_source_history'\n")
    assert reason_gates(fake), '扫描器自己恒空 ⇒ 这条判据等于没判'
    fake_tuple = _ast.parse("def gate(reason):\n"
                            "    return reason in ('no_source_history', 'same_nav_endpoint')\n")
    assert reason_gates(fake_tuple), '抄成元组名单的那一把看不见 ⇒ 判据有洞'


def test_the_close_decision_reads_both_conditions_not_just_the_answer(test_db):
    """那条升级规则的边界：缺任一个条件都不许关（每次调用拆一条门）。"""
    stale = dict(verdict_reason='no_source_history',
                 previous_hold=TODAY - timedelta(days=1),
                 target_date=TODAY - timedelta(days=3),
                 local_latest_nav=TODAY - timedelta(days=400),
                 window_start=TODAY - timedelta(days=3), today=TODAY)
    assert lc.should_close_as_stale_target(**stale) is True
    assert lc.should_close_as_stale_target(**{**stale, 'previous_hold': None}) is False
    assert lc.should_close_as_stale_target(**{**stale, 'verdict_reason': 'insufficient_points'}) is False
    # 那根日期落在自己的目标日**之前** ⇒ 它是创建排期写的，不是上一轮的锁 ⇒ 第一次问出来不关
    assert lc.should_close_as_stale_target(
        **{**stale, 'previous_hold': TODAY - timedelta(days=3)}) is False
    # 锁还没到点（未来）⇒ 这一轮根本不该被问到，更谈不上关
    assert lc.should_close_as_stale_target(
        **{**stale, 'previous_hold': TODAY + timedelta(days=1)}) is False
    # 库里最后一条正好落在窗口起点那天 ⇒ 不是"窗口之前就没发过"，不关
    assert lc.should_close_as_stale_target(
        **{**stale, 'local_latest_nav': TODAY - timedelta(days=3)}) is False


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
