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


def _bulk_write_hits(node, column):
    """`query.update(<dict>)` / `.values(...)` 的**参数位**上有没有这一列 —— 三种 key 写法都要认。

    第 40 轮 B 的 M-5：两版收集器都只认字符串 key（`{'is_correct': v}`）与关键字
    （`values(is_correct=v)`），而 SQLAlchemy 的地道批量写法是**把列对象当 key**
    （`update({Prediction.is_correct: True})`）—— 仓库里今天就有 3 处这么写
    （`routes/viewpoints.py:473`、`retention_cleanup_service.py:907`、
    `retention_three_buckets.py:1108`），说明这条路团队真的会走。

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
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == 'is_correct':
                    writers.add(self.rel)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            if isinstance(node.target, ast.Attribute) and node.target.attr == 'is_correct':
                writers.add(self.rel)
            self.generic_visit(node)

        def visit_Call(self, node):
            fname = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
            hit = (fname == 'setattr' and len(node.args) >= 2
                   and isinstance(node.args[1], ast.Constant)
                   and node.args[1].value == 'is_correct') \
                or (fname in ('update', 'values') and _mentions(node))
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

def test_the_bulk_write_detector_recognises_all_five_spellings():
    """收集器自己的样品：参数位上的三种写法都要认，非参数位的两处不许误伤，用的就是仓库里真有的那种形状。

    没有这条，'已扩展到批量写'又是一次"加了参数不等于加了护栏"（第 17/27/39 轮同一族）。
    """
    import ast
    shapes = [
        ('dict 的 key 是列对象，必须认', 'q.update({Prediction.is_correct: True})', 'is_correct', True),
        ('dict 的 key 是字符串，以前就认', 'q.update({"is_correct": True})', 'is_correct', True),
        ('values 的关键字，以前就认', 'q.values(is_correct=True)', 'is_correct', True),
        ('列名只出现在筛选条件里，不许误伤只读代码', 'q.filter(Prediction.fund_code == x).update({"status": 1})', 'fund_code', False),
        ('读它不算写', 'x = Prediction.is_correct', 'is_correct', False),
        ('写别的列不算写这一列', 'q.update({Prediction.sector: y})', 'is_correct', False),
    ]
    for name, src, col, want in shapes:
        tree = ast.parse(src)
        call = next((n for n in ast.walk(tree) if isinstance(n, ast.Call)), None)
        got = _bulk_write_hits(call, col)
        assert got == want, '%s：判成 %s，应为 %s（源码：%s）' % (name, got, want, src)
