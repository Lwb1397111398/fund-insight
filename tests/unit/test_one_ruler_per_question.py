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


def _status_attrs(node):
    """这个函数里所有"指向预测那一行 `status` 列"的 Attribute 节点。

    行对象那一侧靠变量名认（`prediction` / `pred` / `p` / `Prediction` 列），换个名字
    （`row.status`）它会漏 —— 拦不住所有写法，但 SQL 侧那一腿（`Prediction.status`，
    列对象没法改名）是结实的，而"另起一处判断"最常长的就是那一腿。
    """
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == 'status':
            owner = getattr(n.value, 'id', '') or getattr(n.value, 'attr', '')
            if owner in ('Prediction', 'p', 'pred', 'prediction'):
                out.append(n)
    return out


def _status_judgments(node):
    """这个函数里有**几处**把 `status` 当"验过了吗"来回答（返回行号列表）。

    第 55 轮 M-4：上一版按 (文件, 函数) 收名单 ⇒ 只要那个名字已经在名单里，
    函数体内再造一处判断它一个字都不报（评审的复现：往挂在白名单里的
    `retention_cleanup_service.build_plan` 注一处 `p.status == 'pending'` ⇒ 22 passed 无声）。
    所以这里改数**判断语句**：一个函数里有几处比较/真值判断，名单就得写几。
    取列、回显、赋值都不算（那是另一条账，见 `_status_writes`）。
    """
    attrs = {id(a) for a in _status_attrs(node)}
    if not attrs:
        return []
    parents = {}
    for parent in ast.walk(node):
        for _field, child in ast.iter_fields(parent):
            if isinstance(child, ast.AST):
                parents[id(child)] = parent
            elif isinstance(child, list):
                for c in child:
                    if isinstance(c, ast.AST):
                        parents[id(c)] = parent
    judged = []
    for a in _status_attrs(node):
        cur, decision = a, None
        while cur is not None and decision is None:
            parent = parents.get(id(cur))
            if parent is None:
                break
            if isinstance(parent, ast.Compare):
                decision = 'judge'          # 比较：`Prediction.status == 'pending'`
            elif isinstance(parent, ast.Assign):
                if any(cur is t for t in parent.targets):
                    decision = 'write'      # 写列：留给同步那三处，不是"回答"
                else:
                    decision = 'read'
            elif isinstance(parent, ast.AnnAssign):
                decision = 'write'
            elif isinstance(parent, (ast.If, ast.While, ast.Assert)):
                decision = 'judge'          # 直接当布尔用
            elif isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not):
                decision = 'judge'
            elif isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decision = 'read'           # 到函数头还没遇上上面那些 ⇒ 取列/回显
            cur = parent
            if decision is not None:
                break
        if decision == 'judge':
            judged.append(a.lineno)
    return sorted(set(judged))


def _status_writes(node):
    """这个函数里有没有**写**那一列（`clear_verification_fields` 那三处的正当用途）。"""
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == 'status':
            owner = getattr(n.value, 'id', '') or getattr(n.value, 'attr', '')
            if owner not in ('Prediction', 'p', 'pred', 'prediction'):
                continue
            parent = None
            for cand in ast.walk(node):
                if isinstance(cand, ast.Assign) and any(t is n for t in cand.targets):
                    parent = cand
                    break
            if parent is not None:
                return True
    return False


def _judgments_by_name(src):
    """源码 → `{函数名: 判断处数}`（碰了预测 `status` 列的函数都要出现，0 处也出现）。"""
    tree = ast.parse(src)
    out = {}
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        if _status_judged(fn):
            out.setdefault(fn.name, 0)
            out[fn.name] += len(_status_judgments(fn))
    return out


def _status_judged(node):
    """这个函数碰没碰**预测那一行**的 `status` 列（读、写、判断都算，处置另数）。"""
    return bool(_status_attrs(node)) or _status_writes(node)


def test_only_the_ruler_decides_whether_a_prediction_has_a_conclusion():
    """`status` 那一列不许再被任何一处当成"验过了吗"的答案（第 54 轮 A-2 / B-3）。

    第 55 轮 M-4：名单从"哪些函数碰过"改成"每个函数里有**几处判断**" ⇒
    在一条已经登记的活路里再造一处判断，当场红。
    **边界要说清**：这条闸按"这个函数碰不碰预测"收 —— 只从字典里把 `p.status`
    复制出去的序列化、以及不引用 `Prediction` / `is_correct` 的写法它看不见；
    它拦的是"再写一处判断"，不是"所有出现过的读法"。
    """
    registered = {
        # —— 判断：只有这一处，而且它的第三档问的是"按字面值筛"，不是"有没有结论"
        ('src/services/prediction_query_service.py', '_conclusion_conditions'): 1,
        # —— 写侧：把遗留列与 `is_correct` 保持同步（正因为不可信才要一直回填，不许拿它当判断）
        ('src/services/prediction_verify_service.py', 'clear_verification_fields'): 0,
        ('src/services/prediction_verify_service.py', 'verify_prediction'): 0,
        ('src/services/prediction_verify_service.py', 'rollback_invalid_verifications'): 0,
        # —— 回显 / 取列：payload、导出、清理计划里那一格给人看的话
        ('src/services/prediction_query_service.py', '_serialize'): 0,
        ('src/services/prediction_service.py', 'get_prediction_detail'): 0,
        ('src/services/prediction_service.py', 'get_predictions_for_export'): 0,
        ('src/services/advice_evidence.py', '_build_predictions'): 0,
        ('src/services/post_service.py', 'get_post_detail'): 0,
        ('src/services/retention_cleanup_service.py', 'build_plan'): 0,
        ('src/services/retention_three_buckets.py', '_unverifiable_prediction_ids'): 0,
    }
    found = {}
    for rel, fn, _s in _functions('src'):
        if not _status_judged(fn):
            continue
        found[(rel, fn.name)] = found.get((rel, fn.name), 0) + len(_status_judgments(fn))
    assert set(found) == set(registered), (
            '「有没有结论」又多了一处回答（读 `status` 列）：新增 %s / 已消失 %s'
            % (sorted(set(found) - set(registered)), sorted(set(registered) - set(found))))
    wrong = {k: (found[k], registered[k]) for k in found if found[k] != registered[k]}
    assert not wrong, (
            '这些函数里"拿 status 答一次"的处数与登记不符（实测, 登记）：%s ⇒ '
            '在一条已登记的活路里再加一处判断，以前这条闸一个字都不报（第 55 轮 M-4）' % wrong)

    # 控制一：现造一处违规必须被量到（没有控制断言的判据等于没有判据）
    column_read = ('def f(db):\n'
                   "    return db.query(Prediction).filter(Prediction.status == 'pending')\n")
    assert _status_judged(ast.parse(column_read).body[0]), (
        '列对象那种读法量不到 ⇒ 上面那条永远绿')
    assert _status_judgments(ast.parse(column_read).body[0]) == [2]
    from_var = ('def f(status):\n'
                '    return Prediction.status == status\n')
    assert _status_judgments(ast.parse(from_var).body[0]), (
        '右边是变量就放过 ⇒ 下一轮有人会专门这样写来绕过这条闸')
    row_read = ('def f(pred):\n'
                "    return pred.status == 'success' and pred.is_correct is None\n")
    assert _status_judgments(ast.parse(row_read).body[0]), '行对象那种读法量不到'
    benign = ('def f(task):\n'
              "    return task.status == 'running'\n")
    assert not _status_judged(ast.parse(benign).body[0]), (
        '过宽：**别的模型**的状态机不许被点名（第一版就把 6 处任务状态机点成违规，'
        '那种假红会让下一轮直接把整条闸关掉）')

    # 控制二（这一条才是 M-4 的本体）：**同一个函数里两处判断必须数成 2**，
    # 否则"按函数登记"与"按语句登记"没有区别，上面那条 wrong 检查是装饰。
    twice = ('def f(p):\n'
             "    if p.status == 'pending':\n"
             '        return True\n'
             "    return p.status == 'failed'\n")
    assert len(_status_judgments(ast.parse(twice).body[0])) == 2, (
        '同一函数里第二处判断被并成了 1 ⇒ 名单按函数收，'
        '在已登记的活路里加判断仍然隐身（这就是第 55 轮 M-4 那个洞）')
    # 取列与回显不许被数成判断（不然名单里那 9 处"写/读"会变成违规）
    echo = ('def f(p):\n'
            "    return {'status': p.status}\n")
    assert _status_judgments(ast.parse(echo).body[0]) == [], '回显被数成判断 ⇒ 过宽'
    write = ('def f(p):\n'
             "    p.status = 'success'\n")
    assert _status_judgments(ast.parse(write).body[0]) == [], '写列被数成判断 ⇒ 过宽'

    # 控制三：往**真实登记在册**的函数里注入一处判断，按文件的计数必须 +1
    rel, name = 'src/services/retention_three_buckets.py', '_unverifiable_prediction_ids'
    src = io.open(os.path.join(ROOT, rel.replace('/', os.sep)), encoding='utf-8').read()
    before = _judgments_by_name(src)[name]
    tree = ast.parse(src)
    target = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name][0]
    target.body.insert(0, ast.parse(
        "if p.status == 'failed':\n    pass\n").body[0])
    after = _judgments_by_name(ast.unparse(tree))[name]
    assert after == before + 1, (
        '往已登记的函数里注入一处判断，计数没动（%s → %s）⇒ 这把尺子量的还是"函数存不存在"'
        % (before, after))


def _targets(n):
    """赋值左侧的属性目标，含 `a.x, a.y = …` 这种**解包**写法。

    第一版只认单个 `ast.Attribute`，于是解包赋值整个漏掉 —— 而共用那只钟的正确写法
    恰好就是 `pred.deleted_at, pred.restore_before = archive_stamp()`。
    ⚠ `row.deleted_at: datetime = …`（带标注的赋值）也算一处写（第 59 轮探针 D1：同一份判据里的
    `_query_named` 认 `AnnAssign`、这一腿不认 ⇒ 同一把尺子两种待遇）；
    **纯声明**（`row.deleted_at: datetime` 没有值）不写任何东西，不许数。
    """
    if isinstance(n, (ast.Assign, ast.AugAssign)):
        raw = n.targets if isinstance(n, ast.Assign) else [n.target]
    elif isinstance(n, ast.AnnAssign):
        raw = [] if n.value is None else [n.target]
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


def _instance_dict(call_or_subscript):
    """这个表达式是不是"某个对象的 `__dict__`"：`row.__dict__` 或 `vars(row)`（第 60 轮 MINOR）。

    两条拼法写的是同一个东西 —— 实例自己的属性字典，所以等价于属性赋值，**不受**
    "收件人得长得像查询"那条边界管；上一版只认下标赋值那一半（`row.__dict__[列] = v`），
    `row.__dict__.update({列: v})` 与 `vars(row)[列] = v` 两种拼法实测一格都不数。
    """
    import ast as _ast

    if isinstance(call_or_subscript, _ast.Attribute):
        return call_or_subscript.attr == '__dict__'
    if isinstance(call_or_subscript, _ast.Call):
        return (getattr(call_or_subscript.func, 'id', '') or
                getattr(call_or_subscript.func, 'attr', '')) == 'vars'
    return False


def _dunder_dict_key(assign):
    """`row.__dict__['deleted_at'] = …` / `vars(row)['deleted_at'] = …` → `(列名, 值的表达式)`。

    这一格与"组个字典再递进库"那一族**不是一件事**：直接改实例的 `__dict__` 等价于属性赋值，
    所以**不要求**收件人长得像查询（`_dict_target_key` 那一腿要，因为它组的是普通字典）。
    第 46 轮授予那把尺子认下标赋值，归档这一把上一版不认（探针 B3 实测回 `[]`）。
    """
    if not isinstance(assign, ast.Assign) or len(assign.targets) != 1:
        return None
    t = assign.targets[0]
    if isinstance(t, ast.Subscript) and _instance_dict(t.value) \
            and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str):
        return t.slice.value, assign.value
    return None


def _stamp_names(node):
    """这个函数里哪些名字**就是**那只钟交出来的那一对（`stamp, deadline = archive_stamp()`）。"""
    names = set()
    for n in ast.walk(node):
        if not isinstance(n, ast.Assign):
            continue
        value = n.value
        if isinstance(value, ast.Call) and \
                (getattr(value.func, 'id', '') or getattr(value.func, 'attr', '')) == 'archive_stamp':
            target = n.targets[0]
            for elt in (target.elts if isinstance(target, ast.Tuple) else [target]):
                if isinstance(elt, ast.Name):
                    names.add(elt.id)
    return names


# `db.query(...).update({Prediction.deleted_at: …})` / `.values(**{…})` 这种"批量写"的入口名。
# 只认这三个动词，因为**别的**字典字面量是回显（`_serialize` 返回 `{'deleted_at': …}`），
# 算成写就把 9 处正当的读全变成违规。
_BULK_WRITE_CALLS = ('update', 'values', 'set')
# 判断"这个 `.update({...})` 是查询还是字典"只有一把尺子：收件人那条链上有没有查询动词。
# 光看动词名会过宽 —— `get_detail` 里那句 `detail.update({...})` 是**合并返回给前端的字典**，
# 数成写就是把回显算进账（第 57 轮 m-4 的控制样品）。
_QUERY_VERBS = ('query', 'filter', 'filter_by', 'where', 'select', 'scalars',
                'options', 'join', 'session', 'execute', 'update', 'delete')


def _chain_call_names(expr):
    """`self.db.query(X).filter(Y)` → {'query', 'filter'}（收件人那条链上的调用叶子名）。"""
    out = set()
    cur = expr
    while isinstance(cur, ast.Call):
        leaf = getattr(cur.func, 'attr', '') or getattr(cur.func, 'id', '')
        if leaf:
            out.add(leaf)
        cur = cur.func.value if isinstance(cur.func, ast.Attribute) else None
    return out


def _query_named(node):
    """这个函数里哪些局部变量**是从查询表达式赋来的**（`q = db.query(P).filter(…)`）。
    一跳就停：再深就不是"读代码看得出它在写哪张表"，而是我在猜数据流。"""
    names = set()
    for n in ast.walk(node):
        if not isinstance(n, (ast.Assign, ast.AnnAssign)):
            continue
        targets = n.targets if isinstance(n, ast.Assign) else [n.target]
        value = n.value
        if value is None:
            continue
        for t in targets:
            if isinstance(t, ast.Name) and any(
                    _chain_call_names(c) & set(_QUERY_VERBS)
                    for c in [value] + [x for x in ast.walk(value) if isinstance(x, ast.Call)]):
                names.add(t.id)
    return names


def _bulk_dicts(node, call, query_vars):
    """一次批量写调用里"那些列 = 那些值"的字典，从**三种**来路挖（第 58 轮 M-3）。

    上一版只看**位置参数字典字面量** ⇒ 两格日常拼法一格都不数（探针实测都回 `[]`）：
    ① **关键字** —— SQLAlchemy 2.0 的 `.update(values={col: …})` / `.update(mappings=…)`；
    ② **字典先交给变量** —— `payload = {col: …}` 然后 `q.update(payload)`（本仓 common 写法：
       先组一份 detail 再整份递进去）；
    ③ 位置参数字典字面量（原有那一格，保留）。
    ②只回溯一跳、且**只在函数里能找到唯一一次字典赋值**时才认 —— 来路看不清就当一个没有，
    这条闸拦的是"把归档列整批写掉却没人盯"，不是"我看不穿你的数据流"。
    """
    out = []
    for arg in list(call.args) + [k.value for k in (call.keywords or [])]:
        if isinstance(arg, ast.Dict):
            out.append(arg)
        elif isinstance(arg, ast.Name):
            # 变量那一腿：这个函数里"唯一一次把字典赋给这个名字"
            cands = [a.value for a in ast.walk(node)
                     if isinstance(a, ast.Assign) and len(a.targets) == 1
                     and isinstance(a.targets[0], ast.Name) and a.targets[0].id == arg.id
                     and isinstance(a.value, ast.Dict)]
            if len(cands) == 1:
                out.append(cands[0])
    return out


def _sql_policy():
    """`scripts/sql_write_policy.py` —— "这句 SQL 在写哪一列"的**唯一**那份判据（第 48 轮 B-2）。

    三处棘轮（免疫授予、`is_correct`、`fund_code`）从第 48 轮起共用它；归档这一把以前是**第四份**
    没接上它的（第 58 轮 m-2：`db.execute(text("UPDATE predictions SET deleted_at = now()"))`
    探针实测一格都不数 ⇒ 有人把归档改成裸 SQL，"每处都必须出自那只钟"当场失明）。
    """
    import sys
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))
    import sql_write_policy as sp
    return sp


def _splat_dicts(node, call):
    """`Model(**{列: 值})` 与 `Model(**payload)` 里那些字典（第 59 轮 M-2）。

    属性赋值 / 解包 / setattr / 关键字 / 批量 `.update()` 之外还有这一族：**整包摊进构造函数**。
    `Prediction(**{'deleted_at': datetime.now()})` 与 `Prediction(deleted_at=datetime.now())`
    是同一件事，而上一版只认后者 ⇒ 这一格实测回 `[]`。
    `**payload` 那一腿只回溯一跳（函数里**唯一一次**字典赋值才认），与批量那一腿同一个尺度。
    **边界要说清**：`spec.model(**cleaned)`（合并导入整库那条泛型建行）里**列名静态看不见**，
    这把尺子对它结构性失明 —— 与第 56 轮 NAV 那把遇到的完全同族，那一洞不在这里数，
    由任务 **#144** 记着（`_clean_row` 只剔免疫两列 ⇒ 自带 `deleted_at=60 天前` 的 JSON
    能给任意行伪造"回收站年龄"，而 `/api/config/import` 合并模式既无总开关也无确认头）。
    """
    out = []
    for kw in call.keywords or []:
        if kw.arg is not None:                # `**{…}` 的 `kw.arg` 是 None
            continue
        if isinstance(kw.value, ast.Dict):
            out.append(kw.value)
        elif isinstance(kw.value, ast.Name):
            cands = [a.value for a in ast.walk(node)
                     if isinstance(a, ast.Assign) and len(a.targets) == 1
                     and isinstance(a.targets[0], ast.Name)
                     and a.targets[0].id == kw.value.id and isinstance(a.value, ast.Dict)]
            if len(cands) == 1:
                out.append(cands[0])
    return out


def _dict_sink_names(node):
    """这个函数里"最终落到库上"的那些字典变量的名字（第 59 轮 M-2 的第二半）。

    `payload['deleted_at'] = 墙钟` / `payload.setdefault('deleted_at', 墙钟)` 这种**组字典**的写法，
    单看一句话分不清它是要 `.update(payload)` 进库、还是 `return` 给前端 ——
    上一版正因为分不清，只对"收件人长得像查询"的批量写计分（那是防过宽的边界）。
    这里补的是另一半：**同一个函数里这个字典真的被递进批量写 / 摊进构造函数**，
    那它组装的每一格就是写库的一部分，必须数。没被递出去（`get_detail` 那种整份 return）→ 不数。
    """
    sinks = set()
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        fn = getattr(n.func, 'id', '') or getattr(n.func, 'attr', '')
        if fn in _BULK_WRITE_CALLS and isinstance(n.func, ast.Attribute):
            for a in list(n.args) + [k.value for k in (n.keywords or [])]:
                if isinstance(a, ast.Name):
                    sinks.add(a.id)
        for k in n.keywords or []:
            if k.arg is None and isinstance(k.value, ast.Name):   # `Model(**payload)`
                sinks.add(k.value.id)
    return sinks


def _dict_target_key(assign):
    """`payload['deleted_at'] = …` 这种下标赋值 → `(收件人名, 键名, 值的表达式)`，不是就交回 None。"""
    if not (isinstance(assign, ast.Assign) and len(assign.targets) == 1):
        return None
    t = assign.targets[0]
    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
            and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str):
        return t.value.id, t.slice.value, assign.value
    return None


def _archive_writes(node):
    """这个函数往"归档那一对列"写了几次、每次的值出自哪里。

    返回 `[(行号, 列名, 值的来路)]`，来路三档：
    `stamp`＝共用那只钟（直接调、下标取它第 0/1 格、或调完 unpack 进的那两个名字）、
    `none`＝清空（还原那一支）、
    `other`＝**别处算出来的时间**（墙钟、`date.today()+…`、外面传进来的参数……）。
    第 56 轮 M-4：上一版只交回"写过哪几列"的**集合** ⇒ 一个函数里两处写与一处写在名单上
    长得一模一样，而"回收站那几处必须共用那只钟"那半句只核到"函数里调用过 archive_stamp"
    —— 在已登记的 `_soft_archive` 里再插一行 `row.deleted_at = datetime.now()`，
    集合不变、那次调用也还在 ⇒ 两条断言都不红（实测）。
    第 57 轮 m-4 补三种边：
    ① **下标** —— `row.deleted_at = archive_stamp()[0]` 与 `stamp[0]`（先交给变量再取格）
       都是正确写法，判成 `other` 就是过宽，而过宽的下场是整条闸被人关掉；
    ② **批量写** —— `.update({col: …})` / `.values({col: …})` 里字典的**键**在列名上，
       这一路以前一格都不数 ⇒ 有人把归档改成批量 UPDATE，那条"每处都必须出自那只钟"当场失明；
    ③ **同一行同名的两处** 不能再被 `set` 并成一处，去重按 `(行, 列, 来路, **那次出现的位置**)`。
    认到哪几种拼法为止**以那份判据里的控制样品为准**（属性赋值 / 解包 / 带标注的赋值 / `setattr` /
    `object.__setattr__` / 关键字 / 改实例的 `__dict__` / 下标组字典 / `setdefault` /
    整包 `**` 摊进构造函数 / 裸 SQL …… 第 57~59 轮各补了一批），别在下面抄个数 —— 抄的那个数每次加拼法都会过期。
    ⚠ **仍然要说清它看不见哪三族**（第 59 轮探针 B1/B4 + 第 60 轮 Q1-B，与上面那些"补了就数得到"的不是同一档）：
    ① `setattr(row, 变量, …)` / `getattr` 式写 —— 列名是变量 ⇒ 这一把**没有**登记通道
      （免疫那把为这一档开了 `IMMUNITY_OPAQUE_SITES`，两把尺子对同一件事两种待遇，写在这儿防下轮误当已封）。
      ⚠ **这条边界今天站得住，但依据不是"这种拼法不存在"**（第 60 轮 Q1-B 点的正是这一层区别）：
      `src/services/base.py:99-101` 的 `for key, value in obj_in.items(): setattr(db_obj, key, value)`
      就是**载荷驱动**地写列名，而 `PredictionService` / `ViewpointService` 都继承它、模型上都有那两列。
      今天数不到归档列的真实依据是**调用方名单**：`grep -rn "self.update(" src/services/` 里走这条 helper 的
      只有 `blogger_service.py:162/174`（`{"is_active": …}`）与 `post_service.py:115/132`（字面量键），
      两个归档模型的服务**零调用方**；`config.py:1454` 那个 `setattr(row, field, value)` 循环写的是
      `SectorFundMapping`，而那张表**没有**归档列（`python -c` 现数：`deleted_at`/`restore_before` 均 False）。
      ⇒ 谁哪天给 `base.update` 接上"载荷能递任意列"的路由，这把尺子与那道登记名单**同时失明**；
      那一族的闸得开在载荷层（白名单剔列，与 #144 同一条），不是在这里给正则加形状。
    ② `spec.model(**cleaned)` 那种**泛型建行** —— 列名运行时从元数据拼出来，任何按 AST 数的尺子
      都看不见 ⇒ 那一族的闸在 `_clean_row` 剔列 + 行为判据，不在这里（任务 #144）；
    ③ **查询从函数参数递进来**（`def f(q): q.update({列: 值})`）—— 收件人是不是查询要到调用方才知，
      而"按变量名猜它是 q 还是 query"本仓一贯拒绝（第 45 轮"方向要来自值"同一族）⇒ 不猜、也不数。
    第 60 轮 Q1-B 另外那几格**已经补上**（不是边界，是当时"只补了一半"的拼法）：
    `row.__dict__.update({列: 值})`、`vars(row)[列] = 值` / `vars(row).update({…})`、
    `db.session.set(row, {列: 值})`、以及"先 `p.update({列: 值})` 组字典、再 `q.update(p)` 整份递进库"。
    仍然没补的是**别名**（`s = object.__setattr__` 再 `s(row, 'deleted_at', v)`）—— ⚠ **这句到第 60 轮
    提交信息里被写成了"补了五格"之一，实测回 `[]`，那句话是说过头**（同一批的这段 docstring 当时写的
    才是对的）。第 61 轮才真补：`alias_names` 只认"这个函数里唯一一次把名字绑到 `setattr` /
    `object.__setattr__`"，绑到别处的不算 ⇒ 与第 43 轮 `create_engine as ce` 同一判法（认**值**不认名字）；
    同一批补上 `row.__dict__ |= {列: 值}`（运行时真换字典，上一版只认 `.update({...})` 与下标赋值）。
    """
    stamp = _stamp_names(node)
    query_vars = _query_named(node)
    sink_names = _dict_sink_names(node)
    # `s = setattr` / `s = object.__setattr__` 这种**别名**（第 61 轮 MINOR：上一批把它写成"补了"，
    # 实测回 `[]` ⇒ 那句话是说过头；这一批才真补）。只认"这个函数的名字里唯一一次绑到那两个"，
    # 绑到别处的（`s = row.setter`）不算 ⇒ 不按名字猜 callee，与第 43 轮 `create_engine as ce` 同一判法。
    alias_names = {t.id for a in ast.walk(node) if isinstance(a, ast.Assign)
                   for t in a.targets if isinstance(t, ast.Name)
                   and (getattr(a.value, 'id', '') == 'setattr'
                        or (isinstance(a.value, ast.Attribute)
                            and a.value.attr == '__setattr__'))}
    policy = _sql_policy()
    sql_vars = policy.resolve_assigned_sql(node)
    hits = []

    def _clock_call(value):
        """这个表达式叫的是不是那只钟（`archive_stamp()` / `pl.archive_stamp(7)`）。"""
        fn = getattr(value, 'func', None)
        return fn is not None and \
            (getattr(fn, 'id', '') or getattr(fn, 'attr', '')) == 'archive_stamp'

    def _source_of(value):
        if value is None:
            return 'other'
        if isinstance(value, ast.Constant) and value.value is None:
            return 'none'
        if isinstance(value, ast.Name) and value.id in stamp:
            return 'stamp'
        if _clock_call(value):
            return 'stamp'
        if isinstance(value, ast.Subscript):
            # `archive_stamp()[0]`、`stamp[0]`、`pair[1]` —— 从那一格里取，仍是那只钟
            base = value.value
            if _clock_call(base) or (isinstance(base, ast.Name) and base.id in stamp):
                return 'stamp'
        if isinstance(value, ast.Starred):
            return _source_of(value.value)
        return 'other'

    def _add(lineno, col, attr, kind):
        hits.append((lineno, col, attr, kind))

    for n in ast.walk(node):
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            for t in _targets(n):
                if t.attr in ARCHIVE_COLUMNS:
                    _add(n.lineno, t.col_offset, t.attr, _source_of(n.value))
            # `row.__dict__['deleted_at'] = …`：改的就是那个实例，等价于属性赋值 ⇒ 不受"收件人
            # 得像查询"那条边界管（探针 B3：免疫那把尺子认这一族，归档这一把上一版不认）
            dd = _dunder_dict_key(n)
            if dd and dd[0] in ARCHIVE_COLUMNS:
                _add(n.lineno, n.col_offset, dd[0], _source_of(dd[1]))
            # 组字典那一族：`payload['deleted_at'] = 墙钟` —— 只有这个字典**在本函数里真的递进了库**
            # （批量写 / 摊进构造函数）才算写库，否则它是回显给前端的字典（`get_detail` 的形状）。
            if isinstance(n, ast.Assign) and isinstance(_dict_target_key(n), tuple):
                name, key, value = _dict_target_key(n)
                if key in ARCHIVE_COLUMNS and name in sink_names:
                    _add(n.lineno, n.col_offset, key, _source_of(value))
            # `row.__dict__ |= {'deleted_at': …}`（第 61 轮 MINOR）：运行时**真的**换掉了实例字典
            # （`python -c` 实测 `row.__dict__` 里那一列进去了），而上一版只认 `.update({...})`
            # 与下标赋值两种 ⇒ 同一件事第三种拼法隐身。合并进上面那份"改实例字典"的扫描。
            if isinstance(n, ast.AugAssign) and _instance_dict(n.target) \
                    and isinstance(n.value, ast.Dict):
                for k, v in zip(n.value.keys, n.value.values):
                    if isinstance(k, ast.Constant) and k.value in ARCHIVE_COLUMNS:
                        _add(n.lineno, k.col_offset, k.value, _source_of(v))
        if isinstance(n, ast.Call):
            fn = getattr(n.func, 'id', '') or getattr(n.func, 'attr', '')
            if fn == '__setattr__' and len(n.args) > 2 and isinstance(n.args[1], ast.Constant) \
                    and n.args[1].value in ARCHIVE_COLUMNS:
                # `object.__setattr__(row, 'deleted_at', …)`：第 46 轮授予那把尺子认这一族，
                # 归档这一把上一版不认（探针实测回 `[]`）⇒ 同一件事两种判法就是两把尺子
                _add(n.lineno, n.args[1].col_offset, n.args[1].value, _source_of(n.args[2]))
            if fn == 'setdefault' and len(n.args) > 1 \
                    and isinstance(n.args[0], ast.Constant) and n.args[0].value in ARCHIVE_COLUMNS \
                    and isinstance(n.func, ast.Attribute) \
                    and getattr(n.func.value, 'id', '') in sink_names:
                _add(n.lineno, n.args[0].col_offset, n.args[0].value, _source_of(n.args[1]))
            if fn == 'setattr' and len(n.args) > 2 and isinstance(n.args[1], ast.Constant) \
                    and n.args[1].value in ARCHIVE_COLUMNS:
                _add(n.lineno, n.args[1].col_offset, n.args[1].value, _source_of(n.args[2]))
            # 别名那一腿：`s = object.__setattr__` 再 `s(row, 'deleted_at', …)`（第 61 轮 MINOR）
            if isinstance(n.func, ast.Name) and n.func.id in alias_names \
                    and len(n.args) > 2 and isinstance(n.args[1], ast.Constant) \
                    and n.args[1].value in ARCHIVE_COLUMNS:
                _add(n.lineno, n.args[1].col_offset, n.args[1].value, _source_of(n.args[2]))
            # `row.__dict__.update({'deleted_at': …})` / `vars(row).update({…})`（第 60 轮 MINOR）：
            # 与上面那格下标赋值写的是**同一个实例字典**，所以同样**不要求**收件人长得像查询。
            # 上一批补了 `row.__dict__[列] = v` 却没补 `.update({...})` ⇒ 同一实例写法两种拼法，
            # 只补一半等于没补（探针实测这一格回 `[]`）。
            if isinstance(n.func, ast.Attribute) and n.func.attr in ('update', 'setdefault') \
                    and _instance_dict(n.func.value):
                dicts = [a for a in n.args if isinstance(a, ast.Dict)]
                if n.func.attr == 'setdefault' and n.args and isinstance(n.args[0], ast.Constant):
                    key = n.args[0].value
                    if isinstance(key, str) and key in ARCHIVE_COLUMNS:
                        _add(n.lineno, n.args[0].col_offset, key, _source_of(n.args[1]))
                for arg in dicts:
                    for k, v in zip(arg.keys, arg.values):
                        if isinstance(k, ast.Constant) and k.value in ARCHIVE_COLUMNS:
                            _add(k.lineno, k.col_offset, k.value, _source_of(v))
            # 批量写：字典字面量的**键**是列名才算，值是时间；并且收件人得是查询而不是返回给
            # 前端的字典（`detail.update({...})` 那一格是回显）。
            if fn in _BULK_WRITE_CALLS and isinstance(n.func, ast.Attribute):
                recv = n.func.value
                looks_like_query = bool(_chain_call_names(recv) & set(_QUERY_VERBS)) or \
                    (isinstance(recv, ast.Name) and recv.id in query_vars) or \
                    (isinstance(recv, ast.Attribute) and recv.attr == 'session')
                # 最后那一臂不是"按名字猜"：`Session.set(obj, {...})` 是 SQLAlchemy 2.0 的
                # 身份图写入 API，收件人写的就是 `session`（探针实测 `db.session.set(row, {列: 值})`
                # 上一版一格都不数 —— 那条链上没有查询动词、也不是 `Name`）。
                if looks_like_query:
                    for arg in _bulk_dicts(node, n, query_vars):
                        for k, v in zip(arg.keys, arg.values):
                            if isinstance(k, ast.Constant) and k.value in ARCHIVE_COLUMNS:
                                _add(k.lineno, k.col_offset, k.value, _source_of(v))
                            elif isinstance(k, ast.Attribute) and k.attr in ARCHIVE_COLUMNS:
                                _add(k.lineno, k.col_offset, k.attr, _source_of(v))
            # `p.update({'deleted_at': …})` 而 `p` 之后**整份递进库**（`q.filter(…).update(p)`）：
            # 与上面 `p['deleted_at'] = …` 那一格是同一族，只差一个动词（第 60 轮 MINOR 探针实测：
            # 下标赋值那一半上一批补了，`.update({...})` 这一半没补 ⇒ 同一件事两种待遇）。
            # 收件人必须是"这个函数里真被递进库的那个字典名"，否则它就是回显给前端的字典。
            if fn in _BULK_WRITE_CALLS and isinstance(n.func, ast.Attribute) \
                    and isinstance(n.func.value, ast.Name) and n.func.value.id in sink_names:
                for arg in [a for a in n.args if isinstance(a, ast.Dict)] + \
                        [k.value for k in (n.keywords or []) if isinstance(k.value, ast.Dict)]:
                    for k, v in zip(arg.keys, arg.values):
                        if isinstance(k, ast.Constant) and k.value in ARCHIVE_COLUMNS:
                            _add(k.lineno, k.col_offset, k.value, _source_of(v))
            # 整包摊进构造函数那一族（`Model(**{列: 值})` / `Model(**payload)`，第 59 轮 M-2）：
            # 建行本身就是写，所以**不要求**收件人长得像查询（与上面批量那一腿的边界不同）。
            for payload in _splat_dicts(node, n):
                for k, v in zip(payload.keys, payload.values):
                    if isinstance(k, ast.Constant) and k.value in ARCHIVE_COLUMNS:
                        _add(k.lineno, k.col_offset, k.value, _source_of(v))
                    elif isinstance(k, ast.Attribute) and k.attr in ARCHIVE_COLUMNS:
                        _add(k.lineno, k.col_offset, k.attr, _source_of(v))
            # 裸 SQL 那一腿（第 58 轮 m-2）："这句 SQL 在写哪一列"问共用那把尺子，
            # 值出自哪只钟它答不出（SQL 文本里的 `now()` / `:ts` 都不是 `archive_stamp()`）
            # ⇒ 一律记 `other`：**任何**用裸 SQL 写归档列的站点都必须登记并写明依据。
            if fn in ('execute', 'exec_driver_sql'):
                seen, unclear = policy.classify_sql(
                    n, set(c.lower() for c in ARCHIVE_COLUMNS),
                    extra_texts=policy.variable_sqls(n, sql_vars))
                # **两档都要数**：日期列的值永远不会是 `true`/`1` 那种"看得见即授予"的字面量，
                # 共用那把尺子把它们一律归进 `unclear` —— 只取 `granted` 就等于这条腿恒空
                # （第 58 轮探针实测 ⑨ 那格回 `[]`，就是这个错）。
                for col in sorted(seen | unclear):
                    _add(n.lineno, n.col_offset, col, 'other')
        if isinstance(n, ast.keyword) and n.arg in ARCHIVE_COLUMNS:
            _add(n.lineno, n.col_offset, n.arg, _source_of(n.value))
    unique = {(line, col, attr, kind): (line, attr, kind) for line, col, attr, kind in hits}
    return sorted(unique[k] for k in sorted(unique))


def test_archiving_a_prediction_always_stamps_with_the_shared_clock():
    """写"归档时刻 / 可恢复到哪天"的站点必须闭合，且**放进回收站**那一支必须共用那只钟。

    第 54 轮 A-1 / B-2：`_soft_archive` 改成北京钟时，页面「合并相似预测」那条活路没跟上，
    而它的 docstring 就写着"唯一实现"。登记名单按 (文件, 函数) 数**语句条数**：
    加一处不登记就红；登记了却不再写那一列也红。
    第 57 轮 m-4 把 `_archive_writes` 从"只认属性赋值"补到能认批量写那一族，并把去重键
    换成"每次出现的位置"（下面那一大串控制样品就是它现在认的全集，第 58~59 轮又各补一批）——
    补上批量这一腿**当场量到**的站点里有 `delete_viewpoints_by_ids`，
    而它**是零调用方的死路**（第 58 轮 M-1 抓到：上一版我把死路写成"页面「批量删除观点」一直在用"，
    那是假话 —— 登记它的理由是"它会写那一列"，与有没有人调无关，见下面那条注释）。
    """
    registered = {
        # —— 归档那一支：**每一处**写都必须出自那只钟（下面第二条逐个核）
        ('src/services/prediction_service.py', '_soft_archive'): 2,
        ('src/services/prediction_maintenance_service.py', 'deduplicate_predictions'): 2,
        ('src/tasks/cleanup_enhanced.py', 'soft_delete'): 2,
        # —— 还原那一支：写的是 None（把这一对清空），不涉及时区
        ('src/services/prediction_service.py', 'restore_prediction'): 2,
        ('src/tasks/cleanup_enhanced.py', 'restore'): 2,
        # —— 只读审计：把列名放进清单里打印（`CleanupItemLog` 自己那行日志的时间戳）
        ('src/services/retention_cleanup_service.py', '_audit_item'): 1,
        # —— 别的模型（观点）自己的软删。**⚠ 这条注释连着两版都写过假话，第 59 轮全部按代码重做**：
        # ① 第 57 轮我写"`Viewpoint` 模型压根没有 `restore_before` 这一列" —— 错。AST 按类数
        #    `src/models/database.py`：`Prediction:252` / **`Viewpoint:360`** / `CleanupItemLog:888` 三张表都有。
        #    真的那半句是**前端脚本里没有这几个字**：`grep -c restore_before web/index.html web/*-manager.js` ⇒ 0。
        #    ⚠ **但别说成"这一列没人读"**（第 60 轮 BLOCKER）：后端 `retention_cleanup_service.py:443/447`
        #    就在读它，而它读出来的数走 `build_plan()` → `GET /api/config/cleanup/preview`
        #    （`config.py:333`）→ 页面 `web/index.html:2549` ⇒ 补那一列**会**把行从候选挪进 `protected_counts`。
        # ② 第 58 轮我写"页面「删除观点」那条按钮走 `viewpoint_service.delete_viewpoint`，
        #    所以它拿不到恢复窗口" —— **又错，而且错的正是第 58 轮 M-1 刚罚过的那个错**（把死路当产品事实）：
        #    `grep -rn "delete_viewpoint\b" src/ scripts/ web/ tests/` ⇒ 这个方法在 src/scripts/web
        #    **零调用方**（同名命中的是 `src/api/routes/viewpoints.py:463` 那个**路由**，两条测试叫的也是路由）；
        #    前端 `web/viewpoint-manager.js:303` 打的是
        #    `axios.delete('/api/viewpoints/${id}', {headers:{'X-Danger-Confirm':'delete-viewpoint'}}`)
        #    ⇒ 页面那条是**带确认头的硬删**（`db.delete(viewpoint)`，回执"观点已永久删除"）。
        #    我当时当药方引用的 `cleanup_enhanced.SoftDeleteManager` 同样**全仓零 import**。
        #    ⇒ 这两条登记项今天都是死路，登记理由只剩"它会写那一列"（与有没有人调无关）。
        # ③ 现在按代码写清**活的**那一站在哪、以及这把尺子为什么看不见它：
        #    活的观点软删只有 AI 拒绝那一处 `viewpoint_workflow_service.py:328`（`viewpoint.is_deleted = True`），
        #    而它**这两个时间戳一个都不写** ⇒ 这把按"写归档列"收站点的尺子对它**结构性失明**，
        #    另一面是 `retention_three_buckets._deleted_viewpoint_ids`（:582 起，条件在 :590-592）
        #    要求 `deleted_at.isnot(None)` ⇒ 那些行永远进不了清理桶（镜像实测 18 行 = 418 软删 − 400 带戳）。
        #    ⚠ **任务 #142 的立论到第 60 轮要改口径**：上一版在这里写"补 `restore_before` 也没人读、是空转"
        #    —— 前半句是假的（见上面 ① 的更正，`_viewpoint_candidates:447` 读它、数就在清理预览上）。
        #    站得住的那半是"**预览保护得到、真删保护不到**"：认那一列的只有 `build_plan` 这一把，
        #    而它的 `execute()` 起手就 raise（`HARD_DELETE_DISABLED = True` :46 / raise :268，路由回 403），
        #    线上真在删行的是三桶那一把，它一个字都不看那一列 ⇒ 补列改的是**页面上那个数**，改不了会不会被删。
        # ④ 还有一处两把钟：`retention_cleanup_service.py:157` 是 `self.today = today or date.today()`
        #    而 `grep -rn "RetentionCleanupService(" src/` 数出的 **6** 处构造（`config.py:173/235/300/333`
        #    ＋ `cleanup_tasks.py:769/783`）一个 `today=` 都不递 ⇒ 它处处跑**墙钟**；同做清理的
        #    `retention_three_buckets.py:185` 用北京 `current_as_of()`。上一版我在 AGENTS 里把 :447 那句
        #    （`restore_before >= self.today`，是**窗口检查**、不是"today 减 N 天"）说成"北京那把钟" —— 也是错的。
        #    ⚠ 而"页面三个清理预览都在用墙钟那把"同样说过头了（第 60 轮 MAJOR）：页面上打得到的
        #    是**两个**清理预览、各用一把钟（`/cleanup/preview` 墙钟、`/cleanup/three-buckets/preview` 北京），
        #    `/cleanup/orphan-funds/preview` 那第三个端点**页面不打**（`grep -rn orphan web/` ⇒ 0），
        #    第三个按钮 `/api/test-data/find` 不看日历。⇒ 北京 00:00~08:00 里"预览"与"真删"不是同一个今天。
        ('src/services/viewpoint_service.py', 'delete_viewpoint'): 1,
        # ⚠ **这一条是死路**（第 58 轮 M-1 抓到我把死路说成产品事实）：
        # `grep -rn "delete_viewpoints_by_ids" src/ scripts/ web/` ⇒ **只命中定义那一行**
        # （`viewpoint_service.py:504`）；`/api/viewpoints` 的路由只有 `DELETE /{viewpoint_id}` 单条，
        # 前端只有 `viewpoint-manager.js` 那一条 `axios.delete('/api/viewpoints/${id}')` ⇒ **零调用方**。
        # 上一版三处（AGENTS / 模块总览 / 这里）都写成"页面「批量删除观点」一直在走整条批量 UPDATE"，
        # 那是假话：这条路今天走不通。登记**保留**的理由与调用方无关 ——
        # 它确实往那一列写值，所以"谁以后把它接上活路，必须先改用那只钟"这句要有抓手；
        # 按本仓规矩（第 49 轮 `_save_fund_mapping`、第 54 轮 `sync_predictions_by_sector_mapping`），
        # 死路**不配绿灯判据**，只配登记 + 写明它是死路。
        ('src/services/viewpoint_service.py', 'delete_viewpoints_by_ids'): 1,
    }
    found = {}
    for base in ('src', 'scripts'):
        for rel, fn, _s in _functions(base):
            writes = _archive_writes(fn)
            if writes:
                key = (rel, fn.name)
                found[key] = found.get(key, 0) + len(writes)
    assert set(found) == set(registered), (
            '归档时间戳的写站集合与登记名单不一致：新增 %s / 已消失 %s'
            % (sorted(set(found) - set(registered)), sorted(set(registered) - set(found))))
    wrong = {k: (found[k], registered[k]) for k in found if found[k] != registered[k]}
    assert not wrong, (
            '这些函数里"写归档那一对列"的**处数**与登记不符（实测, 登记）：%s ⇒ '
            '在一条已登记的活路里再加一处写，以前这条闸一个字都不报（第 56 轮 M-4）' % wrong)

    # 逐处核"来路"：这三处都登记在名单里、都往那一对列写，但**只有前两处今天活着**。
    # ⚠ `src/tasks/cleanup_enhanced.py` 整模块**零 import**（`grep -rn cleanup_enhanced
    # --include=*.py src/ scripts/ tests/ | grep -i import` ⇒ 只剩我这行注释），
    # 所以第三格不是"页面在走的一条活路"，而是"谁把它接回去时不许换成墙钟"（第 59 轮 m-3：
    # 上一版把这三处一起叫"回收站那三条活路"，与刚被驳回的 M-1 同一族）。
    for rel, name in (('src/services/prediction_service.py', '_soft_archive'),
                      ('src/services/prediction_maintenance_service.py',
                       'deduplicate_predictions'),
                      ('src/tasks/cleanup_enhanced.py', 'soft_delete')):
        src = io.open(os.path.join(ROOT, rel.replace('/', os.sep)), encoding='utf-8').read()
        tree = ast.parse(src)
        fn = [n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name][0]
        writes = _archive_writes(fn)
        assert writes, '%s:%s 现在一处归档列都不写 ⇒ 上面那条处数账是空的' % (rel, name)
        stray = [(line, attr, kind) for line, attr, kind in writes if kind != 'stamp']
        assert not stray, (
                '%s:%s 里有 %d 处归档时间戳不是出自 `archive_stamp()`：%s ⇒ '
                '同一座回收站里两种"保留到 X 日"（第 54 轮 A-1 那一族复活）'
                % (rel, name, len(stray), stray))

    # 控制一：现造"绕过 archive_stamp 直接写"必须被量到，**四种拼法都要**，且来路分得开
    plain = ('def f(row):\n'
             '    row.is_deleted = True\n'
             '    row.deleted_at = datetime.now()\n')
    assert _archive_writes(ast.parse(plain).body[0]) == [(3, 'deleted_at', 'other')], \
        '绕过那只钟的直接写量不到、或来路判不出 ⇒ 上面那条 stray 检查是装饰'
    unpacked = ('def f(row, stamp):\n'
                '    row.deleted_at, row.restore_before = stamp\n')
    assert [a for _l, a, _k in _archive_writes(ast.parse(unpacked).body[0])] == \
        ['deleted_at', 'restore_before'], (
        '解包赋值漏了 ⇒ 这条棘轮对"正确写法"是瞎的，也就能放过任何一处绕开它的写法')
    from_stamp = ('def f(row):\n'
                  '    row.deleted_at, row.restore_before = archive_stamp()\n')
    assert {k for _l, _a, k in _archive_writes(ast.parse(from_stamp).body[0])} == {'stamp'}, \
        '正确写法被判成"出自别处" ⇒ 那条 stray 检查会天天红，整条闸会被关掉'
    via_names = ('def f(row):\n'
                 '    stamp, deadline = archive_stamp(7)\n'
                 '    row.deleted_at = stamp\n'
                 '    row.restore_before = deadline\n')
    assert [k for _l, _a, k in _archive_writes(ast.parse(via_names).body[0])] == \
        ['stamp', 'stamp'], '钟先交给变量、再逐列赋值这一路认不出 ⇒ 过宽'
    cleared = ('def f(row):\n'
               '    row.deleted_at = None\n'
               '    row.restore_before = None\n')
    assert {k for _l, _a, k in _archive_writes(ast.parse(cleared).body[0])} == {'none'}, \
        '还原那一支（清空）被算成"自己算时间" ⇒ 过宽'

    # 控制二：**同一个函数里两处写必须数成 2**，否则"按处数登记"与"按函数登记"没有区别
    assert len(_archive_writes(ast.parse(via_names).body[0])) == 2

    # 控制二b（第 57 轮 m-4 的三种边）
    subscripted = ('def f(row):\n'
                   '    row.deleted_at = archive_stamp()[0]\n'
                   '    row.restore_before = archive_stamp()[1]\n')
    assert {k for _l, _a, k in _archive_writes(ast.parse(subscripted).body[0])} == {'stamp'}, (
        '从那一格里取下标认不出是那只钟 ⇒ 正确写法被判成"自己算时间"，那条 stray 检查会天天红')
    subscript_via_name = ('def f(row):\n'
                          '    pair = archive_stamp()\n'
                          '    row.deleted_at = pair[0]\n')
    assert [k for _l, _a, k in _archive_writes(ast.parse(subscript_via_name).body[0])] == ['stamp'], (
        '钟先交给变量、再按下标取格这一路认不出 ⇒ 过宽')
    bulk = ('def f(db):\n'
            '    return db.query(Prediction).update('
            '{Prediction.deleted_at: datetime.now(), "restore_before": archive_stamp()[1]})\n')
    got = _archive_writes(ast.parse(bulk).body[0])
    assert sorted(got) == [(2, 'deleted_at', 'other'), (2, 'restore_before', 'stamp')], (
            '批量写（`.update({列: 值})`）一格都不数 ⇒ 把归档改成批量 UPDATE 之后，'
            '"每处都必须出自那只钟"当场失明（实测第 57 轮 m-4）。数到的：%s' % (got,))
    echo_dict = ('def f(p):\n'
                 "    return {'deleted_at': p.deleted_at.isoformat()}\n")
    assert _archive_writes(ast.parse(echo_dict).body[0]) == [], (
        '回显用的字典被数成写 ⇒ 过宽：序列化/_serialize 那一族会变违规')
    payload_merge = ('def f(self, p):\n'
                     '    detail = self._serialize(p)\n'
                     '    detail.update({"deleted_at": x, "restore_before": y})\n'
                     '    return detail\n')
    assert _archive_writes(ast.parse(payload_merge).body[0]) == [], (
            '把**返回给前端的字典**当成批量写 ⇒ 过宽。收件人那条链上没有查询动词就不许数'
            '（真仓库里 `prediction_query_service.get_detail` 就是这个形状，第一版把它点成了违规）')
    via_query_var = ('def f(self, ids):\n'
                     '    q = self.db.query(Viewpoint).filter(Viewpoint.id.in_(ids))\n'
                     '    return q.update({"deleted_at": datetime.now()}, synchronize_session=False)\n')
    assert _archive_writes(ast.parse(via_query_var).body[0]) == [(3, 'deleted_at', 'other')], (
            '查询先交给变量、再 `.update({...})` 这一路认不出 ⇒ 那条批量写的路仍然隐身，'
            '和没补这一腿只差一个变量名')
    same_line_twice = ('def f(row):\n'
                       '    row.deleted_at, row.deleted_at = archive_stamp()\n')
    assert len(_archive_writes(ast.parse(same_line_twice).body[0])) == 2, (
        '同一行、同一列、同一来路的两处写被去重并成 1 ⇒ 处数账还能被"写在同一行"骗过去')

    # 控制二c（第 58 轮 M-3 / m-2）：批量写还有两条隐身拼法 + 裸 SQL 那一整族
    bulk_kw = ('def f(db):\n'
               '    return db.query(Prediction).update('
               'values={"deleted_at": datetime.now()})\n')
    assert _archive_writes(ast.parse(bulk_kw).body[0]) == [(2, 'deleted_at', 'other')], (
            'SQLAlchemy 2.0 的 `.update(values={列: 值})`（**关键字**递字典）一格都不数 ⇒ '
            '把归档改成这一种拼法就脱开这把尺子（探针实测第 58 轮 m-4 只认位置参数）')
    bulk_via_payload = ('def f(db):\n'
                        '    payload = {"deleted_at": datetime.now()}\n'
                        '    return db.query(Prediction).update(payload)\n')
    assert _archive_writes(ast.parse(bulk_via_payload).body[0]) == [(2, 'deleted_at', 'other')], (
            '字典先交给变量、再整份递进 `.update(payload)` 认不出 ⇒ 换个变量名就隐身。'
            '行号锚在**那一格字典**上（第 2 行），不是调用那一行 —— 值在哪一行算出来，'
            '就该在哪一行追责；锚在调用行会把"组 payload 的函数"和"发 UPDATE 的函数"算成两处。')
    raw_sql = ('def f(db):\n'
               '    db.execute(text("UPDATE predictions SET deleted_at = now()"))\n')
    assert _archive_writes(ast.parse(raw_sql).body[0]) == [(2, 'deleted_at', 'other')], (
            '裸 SQL 写归档列一格都不数 ⇒ 这一族完全在这把尺子外面（第 58 轮 m-2：'
            '"这句 SQL 在写哪一列"从第 48 轮起有共用那把尺子 `scripts/sql_write_policy.py`，'
            '归档这一把当时没接上，等于第四份没写）')
    raw_sql_where_only = ('def f(db):\n'
                          '    db.execute(text("SELECT 1 FROM predictions '
                          'WHERE deleted_at IS NOT NULL"))\n')
    assert _archive_writes(ast.parse(raw_sql_where_only).body[0]) == [], (
            '只在 WHERE 里出现那一列被数成写 ⇒ 过宽：那把共用尺子的第一条反向对照就是它')
    # 第 59 轮 M-2 剩下的三格：整包摊进构造函数 / `object.__setattr__` / **组字典**
    splat = ('def f():\n'
             "    return Prediction(**{'deleted_at': datetime.now()})\n")
    assert _archive_writes(ast.parse(splat).body[0]) == [(2, 'deleted_at', 'other')], (
            '`Model(**{列: 值})` 认不出 ⇒ 建行时把归档年龄一起摊进去就脱开这把尺子'
            '（合并导入 `data_portability_service.py:231` 那条泛型建行正是这族）')
    splat_from_stamp = ('def f():\n'
                        "    return Prediction(**{'deleted_at': archive_stamp()[0]})\n")
    assert _archive_writes(ast.parse(splat_from_stamp).body[0]) == [(2, 'deleted_at', 'stamp')], (
            '同一个位置、值出自那只钟却被判成"别处" ⇒ 过宽')
    obj_setattr = ('def f(row):\n'
                   "    object.__setattr__(row, 'deleted_at', datetime.now())\n")
    assert _archive_writes(ast.parse(obj_setattr).body[0]) == [(2, 'deleted_at', 'other')], (
            '`object.__setattr__` 这一族第 46 轮授予那把尺子就认，归档这一把不认 ⇒ '
            '同一件事两种判法（探针实测上一版回 []）')
    dict_payload_into_bulk = ('def f(db):\n'
                              '    payload = {}\n'
                              "    payload['deleted_at'] = datetime.now()\n"
                              '    return db.query(Prediction).update(payload)\n')
    assert _archive_writes(ast.parse(dict_payload_into_bulk).body[0]) == \
        [(3, 'deleted_at', 'other')], ('组完字典再整份递进 UPDATE 认不出 ⇒ 又一条隐身拼法')
    dict_setdefault = ('def f(db):\n'
                       '    payload = {}\n'
                       "    payload.setdefault('restore_before', datetime.now())\n"
                       '    return db.query(Prediction).update(payload)\n')
    assert _archive_writes(ast.parse(dict_setdefault).body[0]) == \
        [(3, 'restore_before', 'other')], ('`setdefault` 组字典那一格认不出 ⇒ 同上一格同族')
    # 上面两格的**反面**：字典没递进库（只是回显给前端）就一处都不许数 —— 少了这两格，
    # "收件人得长得像查询"那条边界会被我为了数全拼法而悄悄放弃（第 57 轮 m-4 的过宽教训）
    echo_subscript = ('def f(p):\n'
                      '    d = {}\n'
                      '    d[\'deleted_at\'] = p.deleted_at\n'
                      '    return d\n')
    assert _archive_writes(ast.parse(echo_subscript).body[0]) == [], (
            '把回显字典的下标赋值数成写 ⇒ 过宽：`_serialize` 那一族会天天红')
    echo_setdefault = ('def f(opts):\n'
                       "    opts.setdefault('deleted_at', None)\n"
                       '    return opts\n')
    assert _archive_writes(ast.parse(echo_setdefault).body[0]) == [], (
            '没递进库的 setdefault 被数成写 ⇒ 过宽（同上一条同族）')
    # 第 59 轮探针剩下的两格：**带标注的赋值** 与 **改实例的 `__dict__`**
    annotated = ('def f(row):\n'
                 '    row.deleted_at: datetime = datetime.now()\n')
    assert _archive_writes(ast.parse(annotated).body[0]) == [(2, 'deleted_at', 'other')], (
            '`row.deleted_at: datetime = 墙钟` 数不到 ⇒ 同一把尺子内部两种待遇'
            '（同文件的 `_query_named` 认 AnnAssign，探针 D1 实测上一版回 []）')
    annotated_decl_only = ('def f(row):\n'
                           '    row.deleted_at: datetime\n')
    assert _archive_writes(ast.parse(annotated_decl_only).body[0]) == [], (
            '纯声明（没有值）被数成写 ⇒ 过宽：那种一行什么都没写')
    dunder_dict = ('def f(row):\n'
                   "    row.__dict__['deleted_at'] = datetime.now()\n")
    assert _archive_writes(ast.parse(dunder_dict).body[0]) == [(2, 'deleted_at', 'other')], (
            '直接改实例的 `__dict__` 等价于属性赋值，上一版一格都不数 ⇒ '
            '免疫那把尺子（第 46 轮）认下标赋值，这把不认就是两把尺子')
    dunder_dict_from_stamp = ('def f(row):\n'
                              "    row.__dict__['restore_before'] = archive_stamp()[1]\n")
    assert _archive_writes(ast.parse(dunder_dict_from_stamp).body[0]) == \
        [(2, 'restore_before', 'stamp')], (
            '同一个位置、值出自那只钟却判成"别处" ⇒ 过宽（那条 stray 检查会天天红）')
    # 第 60 轮 Q1-B 的四格：上一批给 `__dict__` 那一族**只补了下标赋值的一半**。
    # 同一件事的另一种拼法（`.update({...})`）与同一个对象的另一种拿法（`vars(row)`）
    # 都实测回 `[]` ⇒ "补了这一族"必须补到整族，否则下一轮换拼法就是又一次"隐身多年"。
    dunder_update = ('def f(row):\n'
                     "    row.__dict__.update({'deleted_at': datetime.now()})\n")
    assert _archive_writes(ast.parse(dunder_update).body[0]) == [(2, 'deleted_at', 'other')], (
            '`row.__dict__.update({列: 值})` 数不到 ⇒ 与上面那格写的是同一个实例字典，'
            '只差一个动词（探针 Q1-B 实测上一版回 []）')
    vars_subscript = ('def f(row):\n'
                      "    vars(row)['deleted_at'] = datetime.now()\n")
    assert _archive_writes(ast.parse(vars_subscript).body[0]) == [(2, 'deleted_at', 'other')], (
            '`vars(row)[列] = 值` 是 `row.__dict__[列] = 值` 的等价写法 ⇒ 只认一种就是半族')
    vars_update_from_stamp = ('def f(row):\n'
                               "    vars(row).update({'restore_before': archive_stamp()[1]})\n")
    assert _archive_writes(ast.parse(vars_update_from_stamp).body[0]) == \
        [(2, 'restore_before', 'stamp')], (
            '同一格、值出自那只钟却判成"别处" ⇒ 过宽（诚实写法被打红的结局是整条闸被关掉）')
    session_set = ('def f(db, row):\n'
                   "    db.session.set(row, {'deleted_at': datetime.now()})\n")
    assert _archive_writes(ast.parse(session_set).body[0]) == [(2, 'deleted_at', 'other')], (
            'SQLAlchemy 2.0 的 `Session.set(对象, {列: 值})` 数不到 ⇒ 那条链上没有查询动词、'
            '收件人也不是 `Name`，上一版的 `looks_like_query` 恒假（探针 Q1-B 实测回 []）')
    dict_update_then_bulk = ('def f(db):\n'
                             '    p = {}\n'
                             "    p.update({'deleted_at': datetime.now()})\n"
                             '    return db.query(Prediction).filter(Prediction.id == 1).update(p)\n')
    assert _archive_writes(ast.parse(dict_update_then_bulk).body[0]) == \
        [(3, 'deleted_at', 'other')], (
            '"先 `p.update({列: 值})` 组字典、再 `q.update(p)` 整份递进库"数不到 ⇒ '
            '与本批刚补的"下标组字典"那一族**只差一个动词**（同一件事两种待遇）')
    # 上面那格的**反面**：字典没递进库 ⇒ 一处都不许数（`_serialize` 那一族天天红就是这条闸过宽）
    echo_dict_update = ('def f(detail):\n'
                        "    detail.update({'deleted_at': None})\n"
                        '    return detail\n')
    assert _archive_writes(ast.parse(echo_dict_update).body[0]) == [], (
            '把回显字典的 `.update({…})` 数成写 ⇒ 过宽：与 `get_detail` 那一格同族')
    # ↓ 第 61 轮 MINOR 的两格：上一批的**提交信息**把"别名"算进"补了五格"，实测回 `[]`
    #   （同一批的 docstring 当时诚实写着"仍然没补"）⇒ 这一批才真补，每格各配反面对照。
    alias_setattr = ('def f(row, s):\n'
                     '    s = object.__setattr__\n'
                     "    s(row, 'deleted_at', datetime.now())\n")
    assert _archive_writes(ast.parse(alias_setattr).body[0]) == [(3, 'deleted_at', 'other')], (
            '`s = object.__setattr__` 再 `s(row, 列, 值)` 数不到 ⇒ 别名那一族第 43 轮就给'
            '`create_engine as ce` 补过，这把不认就是两把尺子')
    alias_plain = ('def f(row):\n'
                   '    s = setattr\n'
                   "    s(row, 'restore_before', archive_stamp()[1])\n")
    assert _archive_writes(ast.parse(alias_plain).body[0]) == [(3, 'restore_before', 'stamp')], (
            '值明明出自那只钟，只因为写的是别名调用就判"别处" ⇒ 归因错，那条 stray 检查会天天红')
    alias_other = ('def f(row, s):\n'
                   '    s = row.get\n'
                   "    s('deleted_at')\n")
    assert _archive_writes(ast.parse(alias_other).body[0]) == [], (
            '绑到**别的东西**上的名字（`s = row.get`）也被当成 setattr ⇒ 按名字猜 callee，这道闸过宽')
    dunder_or_update = ('def f(row):\n'
                        "    row.__dict__ |= {'deleted_at': datetime.now()}\n")
    assert _archive_writes(ast.parse(dunder_or_update).body[0]) == [(2, 'deleted_at', 'other')], (
            '`row.__dict__ |= {列: 值}` 运行时真换掉了实例字典（`python -c` 实测那一列进去了），'
            '而尺子只认 `.update({...})` 与下标赋值 ⇒ 同一件事第三种拼法隐身')

    # 控制三（M-4 的本体）：往真实登记在册的 `_soft_archive` 注入一处墙钟写 ⇒ 处数 +1 且是 other
    rel, name = 'src/services/prediction_service.py', '_soft_archive'
    src = io.open(os.path.join(ROOT, rel.replace('/', os.sep)), encoding='utf-8').read()
    tree = ast.parse(src)
    target = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name][0]
    before = _archive_writes(target)
    target.body.insert(0, ast.parse('prediction.deleted_at = datetime.now()').body[0])
    injected = ast.parse(ast.unparse(tree))
    after = _archive_writes(next(n for n in ast.walk(injected)
                                 if isinstance(n, ast.FunctionDef) and n.name == name))
    assert len(after) == len(before) + 1 and any(k == 'other' for _l, _a, k in after), (
            '往已登记的函数里注入一处绕开那只钟的写，处数没动或来路没判成 other（%d→%d）'
            '⇒ 这把尺子量的还是"函数存不存在"（第 56 轮 M-4 那个洞）' % (len(before), len(after)))


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
