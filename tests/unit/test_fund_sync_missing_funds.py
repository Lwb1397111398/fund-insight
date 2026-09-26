import inspect

from src.fund.fund_sync_manager import FundSyncManager


def test_sync_missing_funds_builds_existing_fund_code_map():
    """缺失基金同步应批量预加载基金代码，避免循环内逐条查询"""
    source = inspect.getsource(FundSyncManager.sync_missing_funds)

    assert "existing_fund_codes" in source
    assert "FundInfo.fund_code == pred.fund_code" not in source


def _seed_sector_fund_and_bare_prediction(db, nav_days, code='510300',
                                          sector='宽基指数'):
    """一只挂着板块的基金 + 一条**没绑标的**的活预测（`sync_missing_funds` 的输入形状）。"""
    from datetime import date

    from src.models.database import Blogger, FundHistory, FundInfo, Post, Prediction

    blogger = Blogger(name="测试博主", platform="eastmoney")
    db.add(blogger)
    db.flush()
    post = Post(blogger_id=blogger.id, title="测试", content="内容",
                post_date=date(2026, 8, 1))
    db.add(post)
    db.flush()
    db.add(FundInfo(fund_code=code, fund_name="宽基ETF", sector_type=sector,
                    latest_nav=1.0, nav_date=nav_days[-1] if nav_days else None))
    for day in nav_days:
        db.add(FundHistory(fund_code=code, fund_name="宽基ETF", nav_date=day,
                           nav=1.0, day_growth=0.1))
    pred = Prediction(post_id=post.id, blogger_id=blogger.id,
                      prediction_type="up", prediction_date=date(2026, 8, 1),
                      target_date=date(2026, 8, 20), sector_type=sector,
                      status="pending", is_deleted=False)
    db.add(pred)
    db.commit()
    return pred


def test_a_row_the_evidence_gate_refused_is_not_counted_as_linked(test_db):
    """被证据门拒了的行，回执不许把它算进"关联了 N 个预测"。

    第 51 轮 B-2 修的是"什么都没做却说关联了 131 条"那一支；这是同一族的另一半：
    那一支确实**想**关联，但 `retag_prediction` 只回一个 bool，而"已经是这个标的"、
    "被证据门拒了"、"真改了标"三种结局在它身上是同一个 False ⇒
    以前 `linked += 1` 排在那个调用后面无条件执行，拒掉的行照样进数。
    样品是生产真形状：`003033` 那一族，末条净值停在 2020-12-08，窗口在 2026-08。
    """
    from datetime import date

    pred = _seed_sector_fund_and_bare_prediction(test_db, [date(2020, 12, 8)])

    result = FundSyncManager().sync_missing_funds(test_db)

    assert result["linked"] == 0, '拒掉的行被算成"关联了" ⇒ 回执又开始说谎'
    assert result["skipped_unservable"] == 1
    assert not pred.fund_code, '没绑上却说没绑上，行上必须真的还是空的'
    reason = result["details"][0]["reason"]
    assert "2020-12-08" in reason, '只说"跳过"不说为什么 ⇒ 老板无法判断要不要管'


def test_a_target_that_can_evidence_the_window_still_gets_linked(test_db):
    """反向对照：拦下的判据不许宽到把正常关联也打死（否则板块映射永久填不上空）。"""
    from datetime import date, timedelta

    start = date(2026, 8, 1)
    days = [date(2026, 8, 20) - timedelta(days=i) for i in range(4)][::-1]
    assert days[0] >= start
    pred = _seed_sector_fund_and_bare_prediction(test_db, days)

    result = FundSyncManager().sync_missing_funds(test_db)

    assert result["linked"] == 1 and result["skipped_unservable"] == 0
    assert pred.fund_code == '510300'


class _StubApi:
    """假基金域：详情答不出（None）/ 历史答几条 / 或干脆抛错，逐档可控。"""

    def __init__(self, info=None, history=(), boom=False, history_boom=False):
        self._info, self._history = info, list(history)
        self._boom, self._history_boom = boom, history_boom

    def _check(self):
        if self._boom:
            raise RuntimeError('网络断了')

    def get_fund_info(self, code):
        self._check()
        return self._info

    def get_fund_history(self, code, days=30):
        if self._history_boom:
            raise RuntimeError('历史接口断了')
        return list(self._history)


def _seed_archive(db, code, nav_days=()):
    from datetime import date

    from src.models.database import FundHistory, FundInfo
    db.add(FundInfo(fund_code=code, fund_name='基金' + code, sector_type='测试板块',
                    latest_nav=1.0, nav_date=nav_days[-1] if nav_days else date(2020, 1, 1)))
    for day in nav_days:
        db.add(FundHistory(fund_code=code, fund_name='基金' + code, nav_date=day,
                           nav=1.0, day_growth=0.1))
    db.commit()


def test_a_fund_whose_profile_endpoint_is_silent_still_gets_its_nav_updated(test_db, monkeypatch):
    """详情接口答不出但历史接口在给行 ⇒ 净值必须照拉，且**不算更新失败**。

    生产形状：`000725`（大成添利宝货币B，真货币基金）详情接口取不到、净值一直在发。
    旧写法在 `get_fund_info` 回 None 时直接记一条失败、**连历史都不拉** ⇒
    它一天天变旧，在页面上长成老板点名要清零的那一档"无法更新的基金"，
    而回执那句"失败 N 个"把一个真基金混在垃圾码里。
    """
    from datetime import date, timedelta

    from src.fund import fund_sync_manager as fsm
    from src.models.database import FundHistory

    _seed_archive(test_db, '000725')
    days = [date(2026, 9, 20) + timedelta(days=i) for i in range(3)]
    history = [{'date': d, 'nav': 1.0 + i, 'growth': 0.1} for i, d in enumerate(days)]
    monkeypatch.setattr(fsm, 'fund_api', _StubApi(info=None, history=history))

    result = fsm.FundSyncManager().update_all_funds_info(test_db)

    assert result['failed'] == 0, '货币基金被算成更新失败 ⇒ "失败 N 个"里混进了真基金'
    assert result['nav_only'] == 1
    assert sorted(r.nav_date for r in
                  test_db.query(FundHistory).filter(FundHistory.fund_code == '000725')) == days


def test_a_code_the_fund_domain_does_not_know_is_not_counted_as_a_failure(test_db, monkeypatch):
    """两个接口都答不出、库里一行净值都没有 ⇒ 单独一档"查无此码"，不进 failed。

    生产形状：`603758`（秦安股份，A 股代码混进基金库）
    `fund_info 1 行 / 净值 0 行 / 活预测 0 / 映射 0` ⇒ 它唯一的作用就是每次
    `update-all` 回执那行"失败 1 个"，逼老板去查一个不存在的问题。
    """
    from src.fund import fund_sync_manager as fsm

    _seed_archive(test_db, '603758')
    monkeypatch.setattr(fsm, 'fund_api', _StubApi(info=None, history=[]))

    result = fsm.FundSyncManager().update_all_funds_info(test_db)

    assert result['failed'] == 0 and result['unsyncable'] == 1
    assert result['unsyncable_funds'][0]['fund_code'] == '603758', \
        '查无此码必须逐行点名，不许只报一个数（那句"失败 N 只就是那 N 个垃圾码"归错过一次）'


def test_a_real_endpoint_outage_on_a_fund_that_has_nav_stays_a_failure(test_db, monkeypatch):
    """对照：真故障（两个接口都抛错）而库里**有**净值 ⇒ 仍算失败并可重试，不许被新桶吞掉。"""
    from datetime import date

    from src.fund import fund_sync_manager as fsm

    _seed_archive(test_db, '510300', [date(2026, 9, 18), date(2026, 9, 19)])
    # 详情答不出（None）+ 历史接口抛错 ⇒ 才走到"这次真没答上、库里还有历史"那一档。
    # （详情接口直接抛错是另一条路：外层 except 记失败，原因就是那句异常。）
    monkeypatch.setattr(fsm, 'fund_api', _StubApi(info=None, history_boom=True))

    result = fsm.FundSyncManager().update_all_funds_info(test_db)

    assert result['failed'] == 1 and result['unsyncable'] == 0 and result['nav_only'] == 0
    assert '2 行历史' in result['failed_funds'][0]['reason'], \
        '失败原因没说"库里还有历史" ⇒ 与"查无此码"分不清'


def test_the_evidence_ruler_has_exactly_one_call_site_in_the_sync_manager():
    """`retag_prediction` 与回执问的是**同一把尺子**，不是各抄一份。

    第 45 轮的通则："唯一出处"没有测试钉着，下一轮就会多一个出处。
    回执若自己再判一次窗口，就会出现"实跑绑上了、预览说会拒"（或反之）——
    那正是第 51 轮 B-2 与任务 #105 那一族最难查的形状。
    """
    import ast as _ast

    def _gap_calls(node):
        # 只数**真的调用**：注释与 docstring 里提一次函数名不算一处实现
        # （第 43 轮"说明文买不到判据"的同一族，反过来也成立）
        return [n for n in _ast.walk(node)
                if isinstance(n, _ast.Call)
                and getattr(n.func, 'id', '') == 'target_cannot_evidence_window']

    source = inspect.getsource(FundSyncManager)
    methods = {n.name: n for n in _ast.walk(_ast.parse(source))
               if isinstance(n, _ast.FunctionDef)}
    total = sum(len(_gap_calls(fn)) for fn in methods.values())
    assert total == 1, '同步器里又多出一处窗口判据：%s ⇒ 回执与实跑会各自漂' % total

    # 那唯一一处必须待在 `retag_gap` 里（别的调用方都问它拿理由）
    holders = [name for name, fn in methods.items() if _gap_calls(fn)]
    assert holders == ['retag_gap'], '窗口判据不在 retag_gap 里（%s）⇒ 回执没法问它拿原因' % holders

