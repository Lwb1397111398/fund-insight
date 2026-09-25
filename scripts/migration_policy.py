# -*- coding: utf-8 -*-
"""迁移文件的"这一支会不会丢数据"策略：判据**只有一份实现**，测试与运行期共用。

为什么要有这个文件（第 45 轮两份评审各自指出，B-M-2 / A-M-8 的最后一环）：
上一轮我把 `alembic/versions/*.py` 纳进扫描，但那套判据住在
`tests/unit/test_script_db_guards.py` 里 —— 而**每次 Render 启动都会 `upgrade head` 的是
`scripts/run_migrations.py`**（`render.yaml:11` 的 startCommand），它从不跑 pytest，
也就从不读那张名单。结果是："apply 即丢数据的迁移要登记"这句话只在有人记得跑测试时成立。
现在判据搬到这里，`run_migrations.py` 在连线之前自己核一遍，没登记的就不发 DDL。

判"丢数据"按**类别**而不是按名字匹配（AGENTS 里那条老规矩）：
`op.drop_*` 只是其中一种写法，alembic 文档里同样正规的
`op.execute(sa.text("DROP TABLE …"))`、`op.get_bind().execute("TRUNCATE …")`、
f-string 拼出来的 SQL、两跳以上的 helper、以及**根级别就执行**的语句都得算。
看不懂的（参数是个变量）不猜"没事"，按可疑报出来 —— 猜错的代价是删掉生产表。
"""
import ast
import json
import os
import re

# 明确会删结构/删数据的 alembic 方法名
DESTRUCTIVE_METHODS = {'drop_table', 'drop_column', 'drop_index', 'drop_constraint',
                       'drop_schema', 'drop_view', 'drop_sequence', 'truncate'}
# 裸 SQL 里的危险动词（`ALTER` 也算：改列类型/去默认值会重写或清空既有值）
DANGEROUS_SQL = re.compile(r'\b(DROP|TRUNCATE|DELETE|UPDATE|ALTER|RENAME)\b', re.I)
# 这些调用**可能**携带裸 SQL，参数看不清就当可疑
SQL_CARRIERS = {'execute', 'exec_driver_sql', 'text', 'run_sql', 'sql'}
# 动态取方法名：`getattr(op, "drop_" + "table")(...)`
_DYNAMIC_PREFIX = re.compile(r'drop_|truncate', re.I)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VERSIONS = os.path.join(REPO, 'alembic', 'versions')
ALLOWLIST = os.path.join(REPO, 'alembic', 'destructive-upgrades.json')


def _strings(node):
    """表达式里出现的**所有**字符串常量（含 f-string 片段、拼接片段、关键字参数）。"""
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
            and isinstance(n.value, str)]


def _expr_statements(fn):
    """函数体里"结果被丢掉"的那些调用 —— 它们才是**执行一条语句**。

    为什么要分这一层：`op.add_column("bloggers", sa.Column(name, server_default=sa.text(d)))`
    里的 `sa.text(...)` 是一个**默认值表达式**，不是"执行 SQL"。上一版见着 `text(` 就把
    "参数是变量"记成可疑 ⇒ 两支老老实实加列的迁移被报"看不见内容"（第 45 轮我自己
    跑 `--dry-run` 时当场撞出来的误报）。误报的代价不是难看一点，是 Render 每次启动
    都会因为这个脚本退 4 —— 那是把闸门建成墙。
    """
    out = []

    def walk(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(st, ast.Expr):
                out.append(st)
            for field in ('body', 'orelse', 'finalbody'):
                walk(getattr(st, field, None) or [])
            if isinstance(st, ast.Try):
                for handler in st.handlers:
                    walk(handler.body)

    walk(fn.body)
    return out


def _call_args(call):
    return list(call.args) + [k.value for k in call.keywords]


def _directives(fn):
    """一个函数体里的危险动作。返回 (证据列表, 看不清的处数)。"""
    hits, unclear = [], 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        attr = getattr(node.func, 'attr', None)
        fname = getattr(node.func, 'id', None)
        if attr in DESTRUCTIVE_METHODS:
            hits.append('op.%s' % attr)
        elif (attr or fname) == 'getattr' and any(_DYNAMIC_PREFIX.search(s)
                                                  for s in _strings(node)):
            # `getattr(op, "drop_" + "table")("x")`：方法名是拼出来的，但拼完还是删。
            # 注意 `getattr` 是**函数**（`node.func` 是 Name），不是属性调用 —— 上一版
            # 只看了 `.attr`，这条样品当场没被点名。
            hits.append('getattr(op, 动态取删除方法)')
    for stmt in _expr_statements(fn):
        call = stmt.value
        if not isinstance(call, ast.Call):
            continue
        if getattr(call.func, 'attr', None) not in SQL_CARRIERS:
            continue
        strs = _strings(call)
        dangerous = [s for s in strs if DANGEROUS_SQL.search(s)]
        if dangerous:
            hits.append('裸 SQL：%s' % dangerous[0][:40])
        elif any(isinstance(a, (ast.Name, ast.Attribute, ast.Subscript)) for a in _call_args(call)):
            unclear += 1                     # 语句内容在一个变量里：我看见不了它删什么
    return hits, unclear


def _funcs(tree):
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def scan_migration(path):
    """一支迁移：有没有 upgrade/downgrade，**各自**会做什么危险动作。

    helper 的归属走到**不动点**（不限一跳）：`upgrade() → _a() → _b() → op.drop_table`
    这种两跳写法以前不算（A-M8/B-M2 各复现一次）。
    """
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        tree = ast.parse(fh.read(), filename=str(path))
    funcs = _funcs(tree)
    per_func, unclear = {}, {}
    for name, fn in funcs.items():
        hits, bad = _directives(fn)
        per_func[name], unclear[name] = hits, bad

    def reachable(entry):
        """entry 直接或间接调用了哪些本文件 helper（含自己）。"""
        found, stack = {entry}, [entry]
        while stack:
            cur = stack.pop()
            fn = funcs.get(cur)
            if fn is None:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, 'id', None) in funcs \
                        and node.func.id not in found:
                    found.add(node.func.id)
                    stack.append(node.func.id)
        return found

    def collect(entry):
        hits, bad = [], 0
        for name in reachable(entry):
            hits += per_func.get(name, [])
            bad += unclear.get(name, 0)
        return hits, bad

    up_hits, up_unclear = collect('upgrade') if 'upgrade' in funcs else ([], 0)
    down_hits, down_unclear = collect('downgrade') if 'downgrade' in funcs else ([], 0)
    # 根级别（模块顶层）直接执行的动作：`import` 这支迁移就跑了它，不等 upgrade()
    root_hits, root_unclear = [], 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                attr = getattr(sub.func, 'attr', None)
                if attr in DESTRUCTIVE_METHODS:
                    root_hits.append('顶层 op.%s' % attr)
                elif attr in SQL_CARRIERS and any(DANGEROUS_SQL.search(s)
                                                  for s in _strings(sub)):
                    root_hits.append('顶层裸 SQL')

    return {
        'file': os.path.basename(str(path)),
        'has_upgrade': 'upgrade' in funcs,
        'has_downgrade': 'downgrade' in funcs,
        'upgrade_drops': sorted(set(up_hits + root_hits)),
        'upgrade_unclear': up_unclear,
        'downgrade_drops': sorted(set(down_hits)),
    }


def load_allowlist(path=None):
    """已登记的"往上走会丢数据"的迁移：`{文件名: 原因}`。空文件 = 一支都没有。"""
    target = path or ALLOWLIST
    if not os.path.exists(target):
        return {}
    with open(target, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        return {k: (v if isinstance(v, str) else str(v)) for k, v in data.items()}
    if isinstance(data, list):          # 只给了名字没给原因 ⇒ 交给 audit 去判"没写原因"
        return {k: '' for k in data}
    return {}


def audit(versions_dir=None, allowlist=None):
    """返回 `(问题列表, 扫过的支数)`。问题 = 未登记的丢数据迁移 / 缺 downgrade / 参数看不清。"""
    versions_dir = versions_dir or VERSIONS
    registered = allowlist if allowlist is not None else load_allowlist()
    problems, seen = [], 0
    for name in sorted(os.listdir(versions_dir)):
        if not name.endswith('.py') or name.startswith('__'):
            continue
        seen += 1
        try:
            facts = scan_migration(os.path.join(versions_dir, name))
        except SyntaxError:
            problems.append('%s：解析不了，看不清它会不会删东西 ⇒ 按可疑处理' % name)
            continue
        if not facts['has_upgrade']:
            problems.append('%s：没有 upgrade()（这支迁移 apply 时什么都不做？）' % name)
        if not facts['has_downgrade']:
            problems.append('%s：没有 downgrade() ⇒ 回滚无路可走' % name)
        if facts['upgrade_drops'] and name not in registered:
            problems.append('%s：往上走这一支会 %s ⇒ apply 即丢数据，未登记'
                            % (name, '、'.join(facts['upgrade_drops'])))
        elif facts['upgrade_drops'] and not (registered.get(name) or '').strip():
            problems.append('%s：已登记但没写原因（登记不是盖章，要写得清为什么非丢不可）' % name)
        if facts['upgrade_unclear'] and name not in registered:
            problems.append('%s：upgrade 里有 %d 处把 SQL 交给变量，我看不见内容 ⇒ '
                            '要么改成字面量让我看得见，要么登记并写明它删什么'
                            % (name, facts['upgrade_unclear']))
    return problems, seen
