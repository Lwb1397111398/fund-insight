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

