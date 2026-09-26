# -*- coding: utf-8 -*-
""""结论证据是否还复现得出来"这条派生判据，与改标必须清结论的硬规矩。

背景（第 16~18 轮）：全库 1163 条已判结论里有 250 条（21.5%），其端点净值在**当前标的**
的净值表里已经查不出来 —— 其中 53 条是"改标后结论没重算"（存的净值只等于改标前那只基金
当天的值，例 id=1903：现挂 512170、结论却来自 512010 在 07-16 的 0.3788）。
历轮"标量 vs 自家台账"自洽检查看不见它，因为两者是同一次验证一起写的。

这里钉住四件事：
1. 判据本身（含"改标后又按新标的重验过"不能算挂错）；
2. **改标必须同时清掉旧结论并留痕** —— 这是防复发的正解，光清理存量数据只是打地鼠；
3. 列表接口把状态带出去（前端的 ⚠ 标记读它）；
4. 改标那道**证据门**（任务 #100/#105）：不许把预测绑到"这段窗口它给不出净值证据"的
   标的上 —— 那等于当场制造一条到期也判不了的预测，正是老板点名要清零的那一档。
"""
import os
from datetime import date, timedelta

from src.models.database import (
    Blogger, FundHistory, FundInfo, Prediction, PredictionChangeLog, Post)
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
        # 窗口内三笔：改标证据门（任务 #100）要求新标的在这段窗口里给得出
        # `VERIFY_MIN_DATA_POINTS` 个比较点，只留 07-16 那一笔的话它当场就会拒 ——
        # 而本函数要测的是"改标必须清结论"，不是那道门（门的判据在下面几条）。
        for off, extra in ((8, -0.01), (4, -0.02), (0, 0.0)):
            db.add(FundHistory(fund_code=code, nav_date=D - timedelta(days=off),
                               nav=round(nav + extra, 4)))
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


def test_retag_refuses_to_bind_onto_a_target_that_stopped_publishing(test_db, capsys):
    """不许把预测改到"净值覆盖不了这段窗口"的标的上 —— 那等于当场制造一条验不了的预测。

    生产实测（2026-09-26，q.py --production 只读）：`003033` 末条净值停在 2020-12-08，
    却有 37 条活预测挂在它身上，台账里 57 行 `action=maintenance_sync /
    source=sector_mapping` ⇒ 就是这条改标路把它们绑上去的。
    """
    prediction = _seed_verified_prediction(test_db)
    test_db.add(FundHistory(fund_code='003033', nav_date=date(2020, 12, 8), nav=1.173))
    test_db.commit()

    cleared = FundSyncManager.retag_prediction(
        test_db, prediction, '003033', '南方荣冠定开混合', source='sector_mapping')
    test_db.commit()

    assert cleared is False
    test_db.refresh(prediction)
    assert prediction.fund_code == '512010', '拒了却已经写进行 ⇒ 这句拒绝是摆设'
    assert prediction.is_correct is True
    assert test_db.query(PredictionChangeLog).filter(
        PredictionChangeLog.prediction_id == prediction.id).count() == 0
    said = capsys.readouterr().out
    assert '003033' in said and '2020-12-08' in said, '拒了却不说是哪只、为什么 ⇒ 静默'


def test_a_target_without_any_nav_rows_is_still_bindable(test_db):
    """新档案一条净值都还没有（刚建档、还没同步过）⇒ 不能当成"停更"拦掉。

    这是防止上面那道门建成墙：真正常的第一次关联就走这条路。
    """
    prediction = _seed_verified_prediction(test_db)
    assert FundSyncManager.retag_prediction(
        test_db, prediction, '510300', '沪深300ETF', source='unit-test') is True
    test_db.commit()
    test_db.refresh(prediction)
    assert prediction.fund_code == '510300'


def test_the_stopped_target_ruler_has_exactly_one_implementation():
    """"这段净值会不会再来"这把尺子只许有一处实现（改标、验证器、收口脚本共用）。

    第 45 轮那条老账：**"唯一入口"这句话没有测试钉着，下一轮就会多一个入口** ——
    我这一批就先在自己新写的脚本里抄了一份 `latest >= start`，被这条判据当场点红。
    """
    import ast

    def calls_ruler(path, func_name):
        tree = ast.parse(open(path, encoding='utf-8').read())
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
        assert fn is not None, '%s 里找不到 %s()' % (path, func_name)
        called = {getattr(n.func, 'id', None) or getattr(n.func, 'attr', None)
                  for n in ast.walk(fn) if isinstance(n, ast.Call)}
        compares = [n for n in ast.walk(fn) if isinstance(n, ast.Compare)
                    and isinstance(n.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                    and {'local_latest_nav', 'latest', 'start', 'window_start'} &
                    {getattr(x, 'id', '') for x in ast.walk(n)}]
        return 'nav_cannot_cover_window' in called, compares

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    lifecycle = os.path.join(root, 'src', 'services', 'prediction_lifecycle.py')
    script = os.path.join(root, 'scripts', 'close_unknowable_predictions.py')

    used, own = calls_ruler(lifecycle, 'should_close_as_stale_target')
    assert used and not own, '验证器自己不问尺子、改在自己函数里比日期 ⇒ 第二把尺子'
    used, own = calls_ruler(script, 'plan')
    assert used, '存量收口脚本没走那把尺子 ⇒ 两边的"关不关"会各自漂'
    assert not own, '脚本里又手写了一遍日期比较 ⇒ 一处改了另一处不会跟着改'


def test_the_evidence_gate_is_wired_into_both_the_move_and_the_preview():
    """改标门必须有**两个**调用方，且都不许自己抄一遍验证器的两个阈值。

    为什么单独一条：预览那半是 2026-09-26 补的（先前它只装在 `retag_prediction` 里，
    dry-run 根本不经过 ⇒ 回执说 326、实跑只动 320）。"两处共用一把尺子"这句话
    从今往后由这条钉着，少接一处就红。
    """
    import ast

    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))

    def facts(rel, func_name):
        tree = ast.parse(open(os.path.join(root, rel), encoding='utf-8').read())
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
        assert fn is not None, '%s 里找不到 %s()' % (rel, func_name)
        called = {getattr(n.func, 'id', None) or getattr(n.func, 'attr', None)
                  for n in ast.walk(fn) if isinstance(n, ast.Call)}
        consts = {getattr(n, 'attr', None) for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        return called, consts

    called, consts = facts(os.path.join('src', 'fund', 'fund_sync_manager.py'),
                           'retag_prediction')
    assert 'target_cannot_evidence_window' in called, '动手那一侧没接证据门'
    assert 'VERIFY_MIN_DATA_POINTS' not in consts and 'VERIFY_MAX_END_NAV_AGE_DAYS' not in consts, \
        '改标处自己比阈值 ⇒ 验证器改了门不跟着改'

    called, consts = facts(os.path.join('src', 'services', 'prediction_maintenance_service.py'),
                           'sync_sector_mappings')
    assert 'calendar_gap' in called, '预览那一侧没接同一把尺子 ⇒ "将更新 N 条"会说谎'
    assert 'VERIFY_MIN_DATA_POINTS' not in consts and 'VERIFY_MAX_END_NAV_AGE_DAYS' not in consts, \
        '维护服务自己数点数 ⇒ 第二把尺子'


def _bulk_write_hits(node, column):
    """`query.update(<dict>)` / `.values(...)` 的**参数位**上有没有这一列 —— 三种 key 写法都要认。

    第 40 轮 B 的 M-5：两版收集器都只认字符串 key（`{'is_correct': v}`）与关键字
    （`values(is_correct=v)`），而 SQLAlchemy 的地道批量写法是**把列对象当 key**
    （`update({Prediction.is_correct: True})`）—— 仓库里这种写法有 **7 处**
    （尺子：`python -c "import ast,pathlib;n=0
     for p in pathlib.Path('src').rglob('*.py'):
      t=ast.parse(p.read_text(encoding='utf-8'))
      n+=sum(1 for x in ast.walk(t) if isinstance(x,ast.Call) and
             getattr(x.func,'attr','')=='update' and any(isinstance(a,ast.Dict) and
             any(isinstance(k,ast.Attribute) for k in a.keys) for a in x.args))
     print(n)"`；第 41 轮 A-m2 更正：这里以前手抄的是"3 处"，口径数错了）。

    只认参数位（我第一版扫整棵子树，当场误伤 6 处只读代码：
    `filter(Prediction.fund_code == x).update({'status': ...})` 里的 `fund_code` 是筛选条件，
    不是被写的列）。
    """
    import ast
    if not isinstance(node, ast.Call):
        return False
    names = [getattr(node.func, 'attr', None), getattr(node.func, 'id', None)]
    keyed = [kw.arg for kw in node.keywords if kw.arg is not None]
    if column not in keyed and 'update' not in names and 'values' not in names:
        return False
    for arg in node.args:
        if isinstance(arg, ast.Dict):
            for key in arg.keys:
                is_const = isinstance(key, ast.Constant) and key.value == column
                is_column = isinstance(key, ast.Attribute) and key.attr == column
                if is_const or is_column:
                    return True
        elif isinstance(arg, ast.Attribute) and arg.attr == column:
            return True
    for kw in node.keywords:
        if kw.arg == column:
            return True
    return False


def _assign_write_hits(node, column):
    """`X.<column> = ...` / `X.<column>: T = ...` —— 赋值**目标位**上是不是这一列。

    为什么单独抽出来：第 41 轮 A-m2 抓到样品里那条"读它不算写"（`x = Prediction.is_correct`）
    根本没有 Call 节点，于是判据取到 `None`、`_bulk_write_hits(None, …)` 恒 False ——
    那条样品**结构上不可能红**。赋值这一族走的是另一个形状，得有另一个函数，
    样品才有东西可测（第 2/3 项的 `want=True` 就是它）。
    """
    import ast
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Attribute) and t.attr == column for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Attribute) and node.target.attr == column
    return False


def test_no_new_direct_fund_code_writes_appear():
    """改标的写动作只允许出现在已审的地方（第 18 轮 M-2："唯一入口"必须有测试挡）。

    用 AST 扫 `src/` 里所有 `X.fund_code = ...` 赋值。新增站点会让这里变红，
    必须要么改成走 `FundSyncManager.retag_prediction()`（留痕 + 清结论 + 登记博主），
    要么在这里登记并写明为什么它是例外。
    """
    import ast
    import io as _io
    import os

    root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), 'src')
    allowed = {
        # 唯一入口本体
        ('fund/fund_sync_manager.py', 'retag_prediction'),
        # 人工编辑预测：同一函数里对"已生效结论"先 raise 再改（prediction_service.py:409），
        # 所以它不会静默把结论留在改过的标的上
        ('services/prediction_service.py', 'update_prediction_fields'),
        # 下面两处写的是 SectorFundMapping.fund_code（映射表自己的字段），不是预测
        ('services/sector_fund_agent.py', 'apply_decision'),
        ('services/sector_fund_service.py', 'update_mapping'),
        # 序列化：把 ORM 行拼成响应字典（`{"fund_code": row.fund_code, ...}`），
        # 不写库。采集器把"字典里出现这个键"也算命中，所以在这里登记例外 ——
        # 登记而不是放宽规则：真有代码写进来时它照样会红。
        ('api/routes/viewpoints.py', '_serialize_detail'),
    }
    found = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, '/')

            class _Collector(ast.NodeVisitor):
                """第 26 轮 A 的 MAJOR：`fund_code` 这半边原先只认属性赋值，
                `setattr` / 字典批量 / AnnAssign 都能绕过 —— 而 `is_correct` 那半边已经扩了，
                两条"唯一入口"规则覆盖面不一样，等于又制造"同一判据只修一处"。
                """

                def __init__(self):
                    self.stack = []

                def visit_FunctionDef(self, node):
                    self.stack.append(node.name)
                    self.generic_visit(node)
                    self.stack.pop()

                visit_AsyncFunctionDef = visit_FunctionDef

                def _here(self):
                    return (rel, self.stack[-1] if self.stack else '<module>')

                def _assign(self, target):
                    if isinstance(target, ast.Attribute) and target.attr == 'fund_code':
                        found.add(self._here())

                def visit_Assign(self, node):
                    for target in node.targets:
                        self._assign(target)
                    self.generic_visit(node)

                def visit_AnnAssign(self, node):
                    self._assign(node.target)
                    self.generic_visit(node)

                def visit_Call(self, node):
                    fname = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
                    hit = (fname == 'setattr' and len(node.args) >= 3
                           and isinstance(node.args[1], ast.Constant)
                           and node.args[1].value == 'fund_code')
                    if not hit and fname in ('update', 'values'):
                        hit = _bulk_write_hits(node, 'fund_code')
                    # 裸 SQL 那一腿（第 47 轮 B-3）：以前"唯一入口"的判据只看 AST 里的
                    # 属性赋值 / 批量写 / setattr，一条 `db.execute(text("UPDATE predictions
                    # SET fund_code = …"))` 就绕过去了 —— 而"绕开唯一入口"最常见的形态
                    # 恰恰是"我根本没走 ORM"。判据与样品共用 `_raw_sql_write_hits`。
                    if not hit:
                        hit = _raw_sql_write_hits(node, 'fund_code')
                    if hit:
                        found.add(self._here())
                    self.generic_visit(node)

            _Collector().visit(ast.parse(_io.open(path, encoding='utf-8').read()))

    extra = found - allowed
    assert not extra, (
        '出现了新的 fund_code 直写点 %s：请改走 FundSyncManager.retag_prediction()'
        '（否则改标不留痕、不清结论 ⇒ 又是一批 verdict_under_other_fund）' % sorted(extra))
    assert found == allowed, (
        '已登记的写点少了 %s —— 入口被删掉的话也要同步这里的说明'
        % sorted(allowed - found))


def test_is_correct_is_only_written_by_the_verify_service():
    """下结论这件事只有一个入口（第 24 轮删掉的那条死路留下的永久护栏）。

    背景：`PredictionService.verify()` 自 2026-07-26（`2c227c9` 删 `POST /{id}/verify`）
    起就没有调用方，但它仍然"能"写 `is_correct` —— 而且只写这一个字段：
    不写 `verify_score`、不追加 `verify_history`、不重算 `blogger_stats`，
    两份复评各自复现出"结论 False / 分数 100 / 台账 True / 博主准确率 100% /
    区间 0%"这种五处互相打脸的行。方法已删，这条用例保证它不会被"顺手加回来"。

    措辞边界（第 26 轮 A 又抓到我一句话说过头）：还有一条路能整表带入 `is_correct`
    —— `/api/config/import` 的**合并模式**（`data_portability_service.TABLE_SPECS`
    按整行列插，只剔 owner 两列）。而且那道"总开关 + 确认头"**只管覆盖模式**：
    `config.py` 里是 `if req.replace:` 才检查 `ENABLE_DATABASE_IMPORT` 与
    `X-Danger-Confirm` ⇒ 合并模式既无开关也无确认头（有访问密码就能调）。
    所以本用例断言的范围是"**代码里**对 `is_correct` 的属性/批量写只有验证服务一处"，
    不是"全系统只有这一条路"。合并模式要不要也上确认头属于对外接口变更，等老板拍板。
    """
    import ast
    import io as _io
    import os

    root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), 'src')
    writers = set()

    def _mentions(node):
        return _bulk_write_hits(node, 'is_correct')

    class _C(ast.NodeVisitor):
        """四种写法都要认（第 25 轮 A 的 MINOR-6：只认 `x.is_correct = ...` 太窄）。

        字典批量写（`.update({...})`、query `.values(...)`）与 `setattr` 都能绕过属性赋值，
        而那正是"绕过唯一入口"最常见的两种形态。
        """

        def __init__(self, rel):
            self.rel = rel

        def visit_Assign(self, node):
            if _assign_write_hits(node, 'is_correct'):
                writers.add(self.rel)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if _assign_write_hits(node, 'is_correct'):
                writers.add(self.rel)
            self.generic_visit(node)

        def visit_Call(self, node):
            fname = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
            hit = (fname == 'setattr' and len(node.args) >= 2
                   and isinstance(node.args[1], ast.Constant)
                   and node.args[1].value == 'is_correct') \
                or (fname in ('update', 'values') and _mentions(node)) \
                or _raw_sql_write_hits(node, 'is_correct')
            if hit:
                writers.add(self.rel)
            self.generic_visit(node)

    for dirpath, dirs, files in os.walk(root):
        if '__pycache__' in dirpath:
            continue
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, '/')
            _C(rel).visit(ast.parse(_io.open(path, encoding='utf-8').read()))

    assert writers == {'services/prediction_verify_service.py'}, (
        '出现了新的 `is_correct` 写点 %s：下结论必须走 PredictionVerifyService.verify_prediction'
        '（那里才有数据充分性门、休市证据门、退化终点门，并同步分数/台账/博主统计）' % sorted(writers))

    from src.services.prediction_service import PredictionService
    assert not hasattr(PredictionService, 'verify'), (
        'PredictionService.verify 又回来了：它是一条能写结论却不同步分数与统计的旁路')


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


def test_detail_endpoint_and_drawer_both_carry_the_badge(test_db):
    """抽屉（点开看"这条结论凭什么"）必须和列表一样知道证据失效（第 18 轮 MAJOR-2）。

    两半各钉一颗钉子：接口给得出、页面绑得上。少任一条，⚠ 都会在这一屏安静地消失。
    """
    prediction = _seed_verified_prediction(test_db)
    prediction.fund_code = '512170'        # 512170 那天是 0.3335，结论存的是 0.3788
    test_db.commit()

    detail = PredictionQueryService(test_db).get_detail(prediction.id)
    assert detail['evidence_status'] == 'nav_rewritten', detail['evidence_status']
    assert detail['evidence_note']

    import io as _io
    import os as _os
    html = _io.open(_os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__)))), 'web', 'index.html'),
        encoding='utf-8').read()
    assert 'predictionDetail.evidence_status' in html, \
        '抽屉里没绑 evidence_status：列表有 ⚠、点进去没有，正是老板最容易误读的那一屏'
    assert 'predictionDetail.evidence_note' in html


def test_export_snapshot_carries_the_badge_too(test_db):
    """导出的快照也要带着 ⚠（派生值在导出时现算），否则拿出去看数就丢了这个前提。"""
    from src.services.data_portability_service import DataPortabilityService

    prediction = _seed_verified_prediction(test_db)
    prediction.fund_code = '512170'
    test_db.commit()

    exported = DataPortabilityService(test_db).export_data()
    row = next(r for r in exported['predictions'] if r['id'] == prediction.id)
    assert row['evidence_status'] == 'nav_rewritten'
    assert exported['predictions_evidence']['stale_evidence'] == 1

def _raw_sql_write_hits(node, column, sql_vars=None):
    """委托给 `scripts/sql_write_policy.py` —— 三处棘轮共用**一份**判据（第 48 轮 B-2）。

    上一轮我在这一处写了一份、在授予那一处留了另一份，两份一好一坏：好的那份知道
    "只看 SET 子句"，坏的那份不知道 ⇒ 同一句话在一边是三处授予、在另一边一处都不算。
    "判据只许有一份实现"这条规矩在 `migration_policy` 上立过，这里当时没照做。
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'scripts'))
    import sql_write_policy as sp
    return sp.writes_column(node, column, extra_texts=(sql_vars or {}).get(
        getattr(node.func, 'id', '') or '', ()))



def _shape_hits(src, column):
    """一段源码里有没有"写这一列"的形状（赋值目标位 + 批量写参数位，两条都算）。

    单点真源：仓库级扫描（`_C` 那两个 visitor）与下面的样品共用它，
    免得"样品绿、扫描器瞎"或反过来。
    """
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and _assign_write_hits(node, column):
            return True
        if isinstance(node, ast.Call):
            fname = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
            if fname == 'setattr' and len(node.args) >= 2 \
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value == column:
                return True
            if _bulk_write_hits(node, column):
                return True
            if _raw_sql_write_hits(node, column):
                return True
    return False


def test_the_bulk_write_detector_recognises_every_spelling_we_claim():
    """收集器自己的样品：两种"写"的位置（赋值目标位 / 批量写参数位）都要认，
    三种"看着像但不是"的形状不许误伤 —— 用的就是仓库里真有的那种写法。

    没有这条，"已扩展到批量写"又是一次"加了参数不等于加了护栏"（第 17/27/39 轮同一族）。
    第 41 轮 A-m2 修掉的假样品：原来那条 `x = Prediction.is_correct` 走的是
    `_bulk_write_hits(call, …)`，而那行源码**根本没有 Call 节点** ⇒ 取到 `None`、恒 False，
    结构上不可能红。现在样品按"整段源码"判，正反两侧各有真能红的样本。
    """
    shapes = [
        ('dict 的 key 是列对象，必须认', 'q.update({Prediction.is_correct: True})', 'is_correct', True),
        ('dict 的 key 是字符串，以前就认', 'q.update({"is_correct": True})', 'is_correct', True),
        ('values 的关键字，以前就认', 'q.values(is_correct=True)', 'is_correct', True),
        ('属性赋值就是写（仓库里唯一的真写法）', 'prediction.is_correct = True', 'is_correct', True),
        ('带注解的赋值也算', 'prediction.is_correct: bool = False', 'is_correct', True),
        ('setattr 用字符串列名也算', "setattr(p, 'is_correct', True)", 'is_correct', True),
        ('列名只出现在筛选条件里，不许误伤只读代码',
         'q.filter(Prediction.fund_code == x).update({"status": 1})', 'fund_code', False),
        ('放在**值**的位置上是读它，不是写它',
         'q.update({"status": Prediction.fund_code})', 'fund_code', False),
        ('裸读取：赋值目标是别人', 'x = Prediction.is_correct', 'is_correct', False),
        ('写别的列不算写这一列', 'q.update({Prediction.sector: y})', 'is_correct', False),
        # ↓ 第 47 轮 B-3：`is_correct` / `fund_code` 两条"唯一入口"以前**没有裸 SQL 这一腿**。
        # 绕开唯一入口不需要发明新写法 —— 本仓就有 `result = db.execute(...)`
        # （`src/api/main.py:463`）与把语句拼进变量（`:556`）这两种真写法。
        ('裸 SQL 的 SET 就是写这一列',
         'db.execute(sa.text("UPDATE predictions SET is_correct = true"))', 'is_correct', True),
        ('返回值被接走，语义一个字没变',
         'r = db.execute(text("UPDATE predictions SET fund_code = \'510300\'"))',
         'fund_code', True),
        ('INSERT 的列清单里点名这一列也算写',
         'db.execute("INSERT INTO predictions (is_correct) VALUES (1)")', 'is_correct', True),
        ('列名只在 WHERE 里是**定位行**，不是写它',
         'db.execute("UPDATE predictions SET status = 1 WHERE is_correct = true")',
         'is_correct', False),
        ('SELECT 里出现列名不算写',
         'db.execute("SELECT id FROM predictions WHERE is_correct = true")', 'is_correct', False),
    ]
    for name, src, col, want in shapes:
        assert _shape_hits(src, col) is want, '%s：判成 %s，应为 %s（源码：%s）' % (
            name, not want, want, src)
    # 反向护栏：仓库级的 visitor 与本判据必须是同一把尺（改了 visitor 忘了改样品 ⇒ 红）
    import ast
    tree = ast.parse('prediction.is_correct = True')
    assign = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)][0]
    assert _assign_write_hits(assign, 'is_correct'), '赋值那一族整个失效：样品在骗人'


def test_the_evidence_ruler_answers_the_verifier_s_two_questions():
    """改标门问的就是验证器那两件事（点数、终点年龄），逐格钉住它的边界。

    样品用真日历、真常量：把 `VERIFY_MIN_DATA_POINTS` / `VERIFY_MAX_END_NAV_AGE_DAYS`
    挪一格，这张表就得跟着改 —— 页面/门里再藏一个第二个数字会立刻响。
    """
    from src.core.config import config
    from src.services.prediction_lifecycle import target_cannot_evidence_window

    T = date(2026, 9, 26)                      # 今天（"已到期"那一档都拿它当现算的今天）
    start, end = date(2026, 9, 1), date(2026, 9, 20)
    n = config.VERIFY_MIN_DATA_POINTS
    age = config.VERIFY_MAX_END_NAV_AGE_DAYS
    assert n >= 2 and age >= 3, '样品是按这两个值排的，改小就得重排'

    cases = [
        # 标签，窗口内净值日，库里末笔，窗口起点，目标日，期望（None=放行 / 句子里必须有的词）
        ('净值一条都没有 ⇒ 不敢下结论（新档案还没同步过）', [], None, start, end, None),
        ('末笔早于窗口起点 ⇒ 拒（003033 那一族）',
         [], date(2020, 12, 8), start, end, '不会再来'),
        ('点数刚好够、终点就是目标日 ⇒ 放行',
         [end - timedelta(days=i) for i in range(n)], end, start, end, None),
        ('缺点数（%d 笔 < %d）⇒ 拒' % (n - 1, n),
         [end - timedelta(days=i) for i in range(n - 1)] or [end], end, start, end, '只发过'),
        ('终点差 %d 天（正好在上限内）⇒ 放行' % age,
         [start, end - timedelta(days=age)], end, start, end, None),
        ('终点差 %d 天（越界一天）⇒ 拒' % (age + 1),
         [start, end - timedelta(days=age + 1)], end, start, end, '终点取不到'),
        ('窗口还没到期 ⇒ 点数不够也放行（净值本来就该在后面到）',
         [date(2026, 12, 2)], date(2026, 12, 2),
         date(2026, 12, 1), date(2026, 12, 8), None),
        ('窗口起点说不清 ⇒ 不敢下结论', [end], end, None, end, None),
    ]
    for label, in_window, latest, s_, e_, expect in cases:
        got = target_cannot_evidence_window(in_window, latest, s_, e_, today=T)
        if expect is None:
            assert got is None, '%s ⇒ 被拒了：%s' % (label, got)
        else:
            assert got and expect in got, '%s ⇒ 没拒，或拒了没说清（%r）' % (label, got)


def test_retag_allows_a_target_for_a_window_that_has_not_closed_yet(test_db):
    """未到期的一律放行：新标的还没发净值不是毛病，拦它等于把板块映射永久锁死。

    与上面那条表格用例互为对照：同样一笔都嫌少的窗口，把判的日子挪到目标日之前，
    门的答案就必须从"拒"翻成"放行"。
    """
    prediction = _seed_verified_prediction(test_db)
    prediction.prediction_date = date(2026, 12, 1)
    prediction.target_date = date(2026, 12, 8)
    test_db.add(FundInfo(fund_code='999999', fund_name='还没开张的产品'))
    test_db.commit()

    cleared = FundSyncManager.retag_prediction(
        test_db, prediction, '999999', '还没开张的产品', source='unit-test')
    test_db.commit()
    test_db.refresh(prediction)

    assert prediction.fund_code == '999999', '未到期的窗口被拦 ⇒ 这道门是墙'
    assert cleared is True, '改了标却没清结论 ⇒ 这条放行的路把旧标的的结论留下了'
