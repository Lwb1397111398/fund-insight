# -*- coding: utf-8 -*-
"""两把"同一件事只许有一处回答"的棘轮，加一条"合并去重也走同一只钟"的行为判据。

起因（第 54 轮 A-1 / A-2 / B-2 / B-3）：上一批把两件全仓共用的事各只修了**第一条活路**——
① 「这一行有没有结论」改成了问 `is_correct`，但另外四处还在读遗留列 `predictions.status`；
② 归档那一对时间戳改成了北京钟，而页面「合并相似预测」那条活路还在自己 `date.today()+30`。
两个都是"修一半 + 说满一半"，而它们的复现形式对老板来说是同一种：页面上的数与点进去的列表
打脸，或者回收站里那句"保留到 X 日"莫名少一天。
"""
import ast
import io
import os
from datetime import date, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 会改变"这一行验过没有"这句话含义的那些字面值
CONCLUSION_WORDS = ('pending', 'unverified', 'verified', 'success', 'failed')
ARCHIVE_COLUMNS = ('deleted_at', 'restore_before')


def _functions(base):
    """产出 (相对路径, 函数节点, 源码) —— `src/` 与 `scripts/` 都扫，跳过 __pycache__。"""
    for root_dir, _dirs, files in os.walk(os.path.join(ROOT, base)):
        if '__pycache__' in root_dir:
            continue
        for name in files:
            if not name.endswith('.py'):
                continue
            path = os.path.join(root_dir, name)
            rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
            src = io.open(path, encoding='utf-8').read()
            tree = ast.parse(src)
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                yield rel, fn, src


def _status_judged(node):
    """这一处碰没碰**预测那一行**的 `status` 列（读或写都算一处，必须在名单里说明它是什么）。

    先问"这个函数碰不碰预测"（引用过 `Prediction` 这个名字、或读过 `is_correct`）——
    不碰就跳过，否则会把 `BatchAnalysisTask.status == 'pending'` 那一片**别的模型**的
    状态机全点成违规（这条判据的第二版就这么假红了 6 处）。
    **边界**：行对象那一侧靠变量名认（`prediction` / `pred` / `p` / `Prediction` 列），
    换个名字（`row.status`）它会漏 —— 拦不住所有写法，但 SQL 侧那一腿
    （`Prediction.status`，列对象没法改名）是结实的，而"另起一处判断"最常长的就是那一腿。
    """
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    if 'Prediction' not in names and 'is_correct' not in attrs:
        return False
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == 'status':
            owner = getattr(n.value, 'id', '') or getattr(n.value, 'attr', '')
            if owner == 'Prediction' or owner in ('p', 'pred', 'prediction'):
                return True
    return False


def _status_reads(src_tree):
    """一处读的**写法**：列对象 `Prediction.status`，还是行对象 `pred.status`。"""
    kinds = set()
    for n in ast.walk(src_tree):
        if isinstance(n, ast.Attribute) and n.attr == 'status':
            owner = getattr(n.value, 'id', '') or getattr(n.value, 'attr', '')
            kinds.add('column' if owner == 'Prediction' else 'row:%s' % owner)
    return kinds


def test_only_the_ruler_decides_whether_a_prediction_has_a_conclusion():
    """`status` 那一列不许再被任何一处当成"验过了吗"的答案（第 54 轮 A-2 / B-3）。

    登记名单里只有一处是**判断**（`_conclusion_conditions` 的第三档：按字面值筛），
    其余都是回显或取列。**边界要说清**：这条闸按"这个函数碰不碰预测"收 ——
    只从字典里把 `p.status` 复制出去的序列化、以及不引用 `Prediction` / `is_correct`
    的写法它看不见；它拦的是"再写一处判断"，不是"所有出现过的读法"。
    """
    registered = {
        # —— 判断：只有这一处，而且它的第三档问的是"按字面值筛"，不是"有没有结论"
        ('src/services/prediction_query_service.py', '_conclusion_conditions'),
        # —— 写侧：把遗留列与 `is_correct` 保持同步（正因为不可信才要一直回填，不许拿它当判断）
        ('src/services/prediction_verify_service.py', 'clear_verification_fields'),
        ('src/services/prediction_verify_service.py', 'verify_prediction'),
        ('src/services/prediction_verify_service.py', 'rollback_invalid_verifications'),
        # —— 回显 / 取列：payload、导出、清理计划里那一格给人看的话
        ('src/services/prediction_query_service.py', '_serialize'),
        ('src/services/prediction_service.py', 'get_prediction_detail'),
        ('src/services/prediction_service.py', 'get_predictions_for_export'),
        ('src/services/advice_evidence.py', '_build_predictions'),
        ('src/services/post_service.py', 'get_post_detail'),
        ('src/services/retention_cleanup_service.py', 'build_plan'),
        ('src/services/retention_three_buckets.py', '_unverifiable_prediction_ids'),
    }
    found = {(rel, fn.name) for rel, fn, _s in _functions('src') if _status_judged(fn)}
    assert found == registered, (
            '「有没有结论」又多了一处回答（读 `status` 列）：新增 %s / 已消失 %s'
            % (sorted(found - registered), sorted(registered - found)))

    # 控制：现造一处违规必须被量到（没有控制断言的判据等于没有判据）
    column_read = ('def f(db):\n'
                   "    return db.query(Prediction).filter(Prediction.status == 'pending')\n")
    assert _status_judged(ast.parse(column_read).body[0]), (
        '列对象那种读法量不到 ⇒ 上面那条永远绿')
    from_var = ('def f(status):\n'
                '    return Prediction.status == status\n')
    assert _status_judged(ast.parse(from_var).body[0]), (
        '右边是变量就放过 ⇒ 下一轮有人会专门这样写来绕过这条闸')
    row_read = ('def f(pred):\n'
                "    return pred.status == 'success' and pred.is_correct is None\n")
    assert _status_judged(ast.parse(row_read).body[0]), '行对象那种读法量不到'
    benign = ('def f(task):\n'
              "    return task.status == 'running'\n")
    assert not _status_judged(ast.parse(benign).body[0]), (
        '过宽：**别的模型**的状态机不许被点名（第一版就把 6 处任务状态机点成违规，'
        '那种假红会让下一轮直接把整条闸关掉）')


def _targets(n):
    """赋值左侧的属性目标，含 `a.x, a.y = …` 这种**解包**写法。

    第一版只认单个 `ast.Attribute`，于是解包赋值整个漏掉 —— 而共用那只钟的正确写法
    恰好就是 `pred.deleted_at, pred.restore_before = archive_stamp()`。
    """
    if isinstance(n, (ast.Assign, ast.AugAssign)):
        raw = n.targets if isinstance(n, ast.Assign) else [n.target]
    else:
        raw = []
    out, stack = [], list(raw)
    while stack:
        t = stack.pop()
        if isinstance(t, (ast.Tuple, ast.List)):
            stack.extend(t.elts)
        elif isinstance(t, ast.Starred):
            stack.append(t.value)
        elif isinstance(t, ast.Attribute):
            out.append(t)
    return out


def _archive_writes(node):
    """这一处写不写归档那一对列？（属性赋值 / 解包赋值 / setattr / 关键字参数都算）"""
    hits = set()
    for n in ast.walk(node):
        for t in _targets(n):
            if t.attr in ARCHIVE_COLUMNS:
                hits.add(t.attr)
        if isinstance(n, ast.Call):
            fn = getattr(n.func, 'id', '') or getattr(n.func, 'attr', '')
            if fn == 'setattr' and len(n.args) > 1 and isinstance(n.args[1], ast.Constant) \
                    and n.args[1].value in ARCHIVE_COLUMNS:
                hits.add(n.args[1].value)
        if isinstance(n, ast.keyword) and n.arg in ARCHIVE_COLUMNS:
            hits.add(n.arg)
    return hits


def test_archiving_a_prediction_always_stamps_with_the_shared_clock():
    """写"归档时刻 / 可恢复到哪天"的站点必须闭合，且**放进回收站**那一支必须共用那只钟。

    第 54 轮 A-1 / B-2：`_soft_archive` 改成北京钟时，页面「合并相似预测」那条活路没跟上，
    而它的 docstring 就写着"唯一实现"。登记名单按 (文件, 函数) 数站点：
    加一处不登记就红；登记了却不再写那一列也红。
    """
    registered = {
        # 归档那一支：三处都必须调用 `archive_stamp()`（行为判据在下面两条）
        ('src/services/prediction_service.py', '_soft_archive'),
        ('src/services/prediction_maintenance_service.py', 'deduplicate_predictions'),
        ('src/tasks/cleanup_enhanced.py', 'soft_delete'),
        # 还原那一支：写的是 None（把这一对清空），不涉及时区
        ('src/services/prediction_service.py', 'restore_prediction'),
        ('src/tasks/cleanup_enhanced.py', 'restore'),
        # 只读审计：把列名放进清单里打印，不写值
        ('src/services/retention_cleanup_service.py', '_audit_item'),
        # 别的模型（观点）自己的软删，与预测的保留期不是一件事
        ('src/services/viewpoint_service.py', 'delete_viewpoint'),
    }
    found = {(rel, fn.name) for rel, fn, _s in _functions('src') if _archive_writes(fn)}
    found |= {(rel, fn.name) for rel, fn, _s in _functions('scripts') if _archive_writes(fn)}
    assert found == registered, (
            '归档时间戳的写站集合与登记名单不一致：新增 %s / 已消失 %s'
            % (sorted(found - registered), sorted(registered - found)))

    # 三条"放进回收站"的活路都必须**真的调用**那只共用的钟
    for rel, name in (('src/services/prediction_service.py', '_soft_archive'),
                      ('src/services/prediction_maintenance_service.py',
                       'deduplicate_predictions'),
                      ('src/tasks/cleanup_enhanced.py', 'soft_delete')):
        src = io.open(os.path.join(ROOT, rel.replace('/', os.sep)), encoding='utf-8').read()
        tree = ast.parse(src)
        fn = [n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name][0]
        called = {getattr(c.func, 'id', '') or getattr(c.func, 'attr', '')
                  for c in ast.walk(fn) if isinstance(c, ast.Call)}
        assert 'archive_stamp' in called, (
                '%s:%s 自己算归档时间戳 ⇒ 它就是第二条旁路（用 archive_stamp()）' % (rel, name))

    # 控制：现造一处"绕过 archive_stamp 直接写"必须被量到，**两种写法都要**
    plain = ('def f(row):\n'
             '    row.is_deleted = True\n'
             '    row.deleted_at = datetime.now()\n')
    assert _archive_writes(ast.parse(plain).body[0]) == {'deleted_at'}
    unpacked = ('def f(row, stamp):\n'
                '    row.deleted_at, row.restore_before = stamp\n')
    assert _archive_writes(ast.parse(unpacked).body[0]) == {'deleted_at', 'restore_before'}, (
        '解包赋值漏了 ⇒ 这条棘轮对"正确写法"是瞎的，也就能放过任何一处绕开它的写法')


def test_deduplicating_predictions_stamps_the_archive_with_the_beijing_clock(test_db,
                                                                             monkeypatch):
    """页面「合并相似预测」真的走这一条：两把钟给出不同日期时，必须按北京那把写。

    夹具把 `date.today()` 与 `current_as_of()` 故意摆到**不同的一天**（本机在 +8，
    永远看不到这个冲突；Render 容器在 UTC，北京 00:00~08:00 就会差一天）。
    """
    from src.models.database import Blogger, Post, Prediction
    from src.services import prediction_lifecycle as pl
    from src.services.prediction_maintenance_service import PredictionMaintenanceService

    blogger = Blogger(name='去重博主', platform='wechat')
    test_db.add(blogger)
    test_db.flush()
    post = Post(blogger_id=blogger.id, title='t', content='c', post_date=date.today())
    test_db.add(post)
    test_db.flush()
    target = date.today() - timedelta(days=3)
    kept, dropped = [], []
    for i in range(2):
        p = Prediction(post_id=post.id, blogger_id=blogger.id, fund_code='510300',
                       fund_name='沪深300ETF', sector='宽基指数', prediction_type='up',
                       prediction_content='涨%d' % i, confidence=80,
                       prediction_date=target - timedelta(days=10),
                       prediction_period='2周', target_date=target,
                       status='pending', is_expired=False)
        test_db.add(p)
        (kept if i == 0 else dropped).append(p)
    test_db.commit()

    beijing = date.today() + timedelta(days=1)      # 与 date.today() 冲突的那把钟
    monkeypatch.setattr(pl, 'current_as_of', lambda: beijing)
    monkeypatch.setattr('src.services.prediction_maintenance_service.date',
                        _FakeDate(date.today()))

    result = PredictionMaintenanceService(test_db).deduplicate_predictions()
    assert [p.id for p in dropped if p.is_deleted], result
    test_db.refresh(dropped[0])
    assert dropped[0].restore_before == beijing + timedelta(days=30), (
        '恢复下界没按北京那把钟算 ⇒ 回收站那句"保留到 X 日"比页面上少一天')
    assert dropped[0].deleted_at.date() == beijing
    assert dropped[0].deleted_by == 'maintenance'


class _FakeDate:
    """只替掉 `date.today()`，`date.fromisoformat` 等类方法必须原样能用。"""

    def __init__(self, today):
        self._today = today

    def today(self):
        return self._today

    def __getattr__(self, item):
        return getattr(date, item)

    def __call__(self, *a, **k):
        return date(*a, **k)


@pytest.mark.parametrize('latest, start, first, end, expected', [
    # 末条净值早于窗口起点 ⇒ 停更
    (date(2020, 1, 1), date(2026, 1, 1), date(2019, 1, 1), date(2026, 2, 1), 'stopped'),
    # 窗口整段早于库里第一笔净值 ⇒ 那几天它还没开始发
    (date(2026, 2, 1), date(2026, 1, 1), date(2026, 3, 1), date(2026, 2, 1), 'pre_inception'),
    # 两边都不满足 ⇒ 还能判，不关
    (date(2026, 9, 18), date(2026, 1, 1), date(2025, 1, 1), date(2026, 2, 1), None),
    # 库里一行净值都没有（两个日期都说不清）⇒ 不许关，也不许算成任何一种"永久"
    (None, date(2026, 1, 1), None, date(2026, 2, 1), None),
    # 第 54 轮 A-6：这两格以前没有样品 —— 把 `nav_started_after_window` 改成
    # "库里查不到首笔就当它还没开始"也能全绿 ⇒ "任一日期说不清就不算永久"只钉了一半
    (date(2026, 9, 18), date(2026, 1, 1), None, date(2026, 2, 1), None),
    (None, date(2026, 1, 1), date(2026, 3, 1), None, None),
])
def test_a_permanent_shape_needs_both_of_its_dates(latest, start, first, end, expected):
    """`stale_close_evidence` 的输入一格一个答案：**说不清**不等于**永久**。"""
    from src.services.prediction_lifecycle import stale_close_evidence

    assert stale_close_evidence(local_latest_nav=latest, window_start=start,
                                local_first_nav=first, window_end=end) == expected, (
        '这一格的答案变了：拿"日期缺失"当证据 ⇒ 一只只是还没同步过的基金'
        '会被判成永久判不出来')


def test_the_shared_clock_returns_a_pair_from_one_ruler():
    """`archive_stamp()` 自己：日期出自 `current_as_of()`，保留期是它 + 天数。"""
    from datetime import datetime as _dt

    from src.services import prediction_lifecycle as pl

    stamp, deadline = pl.archive_stamp(retention_days=7)
    assert isinstance(stamp, _dt) and deadline == pl.current_as_of() + timedelta(days=7)
    assert stamp.date() == pl.current_as_of()
