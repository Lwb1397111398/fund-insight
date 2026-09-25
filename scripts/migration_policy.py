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

# 明确会删结构/删数据的 alembic 方法名。**按类别取**（第 47 轮 B-1：上一版逐个点名，
# `op.rename_table` 不在名单里 ⇒ "把 `predictions` 改名成 `predictions_old`"这一支
# 审计回的是"干净"，而应用侧那张表就此消失 —— 改名与删除在"数据还找不找得回来"这件事上
# 是同一类，alembic 的删除方法一律是 `drop_*` 前缀，所以这里只点前缀 + 三个不规则名字）
DESTRUCTIVE_METHODS = {'truncate', 'rename_table', 'rename_column'}
DESTRUCTIVE_PREFIX = 'drop'


def _is_destructive_method(name):
    return bool(name) and (name in DESTRUCTIVE_METHODS or name.startswith(DESTRUCTIVE_PREFIX))

# 裸 SQL 里的危险动词（`ALTER` 也算：改列类型/去默认值会重写或清空既有值）
DANGEROUS_SQL = re.compile(r'\b(DROP|TRUNCATE|DELETE|UPDATE|ALTER|RENAME)\b', re.I)
# **执行** SQL 的入口：跑起来就把语句发出去，不管调用方有没有用返回值。
EXEC_CARRIERS = {'execute', 'exec_driver_sql', 'run_sql', 'sql'}
# 只是**构造** SQL 对象的写法（`sa.text(...)`）：它本身不执行，所以既不算危险动作、
# 也不该因为"参数是变量"就被记成看不清 —— 第 45 轮的误报就是把 `server_default=sa.text(d)`
# 当成"执行了一条看不见的 SQL"，那会让 Render 每次启动退 4（闸门建成墙）。
SQL_BUILDERS = {'text'}
# 动态取方法名：`getattr(op, "drop_" + "table")(...)`
_DYNAMIC_PREFIX = re.compile(r'drop_|truncate', re.I)
# **"把要做的事交给一段我看不见的代码"**：子进程、动态执行、现读现 eval。
# 这些不是"危险动词"，而是"我到此为止看不见"⇒ 一律按可疑报（第 46 轮 A-M5 的第三条样品
# `def upgrade():\n    def inner():\n        import os\n        os.system("alembic downgrade -1")`
# 以前既不进 drops 也不进 unclear，审计回的是"干净"）。
# 分两档是因为 `run` / `call` 这类名字太普通：只有挂在进程相关模块上才算，
# 否则 `df.apply(...)`、`json.loads(...)` 这类正常写法会被判成"看不清"⇒ 每次启动退 4
# （第 45 轮 `server_default=sa.text(d)` 那次误报就是同一课）。
PROCESS_MODULES = {'os', 'subprocess', 'pty', 'commands', 'popen2', 'asyncio'}
PROCESS_SINKS = {'system', 'popen', 'run', 'call', 'check_call', 'check_output', 'execv',
                 'execve', 'execvp', 'spawnl', 'spawnv', 'spawn', 'kill', 'send_signal'}
BUILTIN_SINKS = {'eval', 'exec', 'compile', '__import__'}
# "把控制权交给看不见的内容"：子进程 / 动态执行 / 现读现拼的代码。
# 迁移里出现任何一条，我都无法回答"这一支往上走删了什么" ⇒ 一律按可疑登记（第 46 轮 A-M5：
# 探针样品 `def upgrade(): def inner(): import os; os.system("alembic downgrade -1")`
# 以前既不进 drops 也不进 unclear，审计回的是"干净"）。
OPAQUE_EXECUTORS = {'system', 'popen', 'spawn', 'spawnl', 'execv', 'execve', 'run', 'Popen',
                    'check_call', 'check_output', 'call', 'exec', 'eval', '__import__'}

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
VERSIONS = os.path.join(REPO, 'alembic', 'versions')
ALLOWLIST = os.path.join(REPO, 'alembic', 'destructive-upgrades.json')
# 报错消息里印的是仓库相对路径（绝对路径带着用户名目录，既难看又没法照着敲）
REL_ALLOWLIST = 'alembic/destructive-upgrades.json'


def _strings(node):
    """表达式里出现的**所有**字符串常量（含 f-string 片段、拼接片段、关键字参数）。"""
    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant)
            and isinstance(n.value, str)]


def _call_args(call):
    return list(call.args) + [k.value for k in call.keywords]


def _is_opaque_arg(node):
    """参数是变量 / 属性 / 下标 ⇒ 语句内容我看不见（字面量、f-string 拼的都不算这一类）。"""
    return isinstance(node, (ast.Name, ast.Attribute, ast.Subscript))


def _classify_calls(nodes):
    """一批 AST 节点里的危险动作。返回 (证据列表, 看不清的处数)。

    第 46 轮 B-M4 / A-M5 把这一层从"只看结果被丢掉的语句"改成"**看所有调用**"：
    `result = op.execute(SQL)`、`if op.execute(SQL).scalar():`、`[op.execute(S) for S in …]`、
    `with op.get_bind() as c: c.execute(VAR)` 四种写法都在**执行**那条语句，
    只是把返回值接走了 —— 而旧写法要求"这条调用必须是一整句"，于是它们全部隐身，
    连"看不清"都不标（审计回"干净"）。反过来，`sa.text(d)` 只是**构造**对象，
    它落在 `server_default=` 里时既没执行也不该被记成可疑（那是第 45 轮的误报）。
    """
    hits, unclear = [], 0
    for holder in nodes:
        for node in ast.walk(holder):
            if not isinstance(node, ast.Call):
                continue
            attr = getattr(node.func, 'attr', None)
            fname = getattr(node.func, 'id', None)
            strs = _strings(node)
            if attr in ('alter_column', 'modify_column'):
                # 改列类型/收窄长度会**重写或清空**既有值（`DANGEROUS_SQL` 里早就有 `ALTER`，
                # 但方法形式以前既不算命中也不算看不清，审计回的是"干净"——第 47 轮 B-1 的
                # 第四种形状）。为什么不直接算"删除"：多数 `alter_column` 只是加默认值，
                # 硬算删除会把 9 支正常迁移全逼进登记表；按"看不清"报出来让人看一眼，
                # 判据与代价都对得上。
                unclear += 1
                continue
            if _is_destructive_method(attr) or (isinstance(node.func, ast.Name)
                                                and _is_destructive_method(fname)):
                # 裸名调用也算（第 47 轮 B-1：`from alembic.op import drop_table` 之后
                # 直接写 `drop_table("predictions")` —— 语义一个字没改，只因为不是
                # `op.` 前缀就隐身）
                hits.append('%s（删除类方法）' % (attr or fname))
                continue
            if (attr or fname) == 'getattr' and any(_DYNAMIC_PREFIX.search(s) for s in strs):
                # `getattr(op, "drop_" + "table")("x")`：方法名是拼出来的，但拼完还是删。
                hits.append('getattr(op, 动态取删除方法)')
                continue
            executes = (attr in EXEC_CARRIERS or fname in EXEC_CARRIERS
                        or isinstance(node.func, ast.Call))    # getattr(...)() 这种"取来就跑"
            if not executes:
                # 把活儿交给一段我看不见的代码：子进程 / 动态执行 ⇒ 看不见就等于"可疑"，
                # 不猜"没事"（猜错的代价是删掉生产表，第 45/46 轮两份评审同一句话）。
                recv = getattr(node.func, 'value', None)
                if fname in BUILTIN_SINKS \
                        or (attr in PROCESS_SINKS
                            and getattr(recv, 'id', None) in PROCESS_MODULES) \
                        or (attr == 'Popen' and getattr(recv, 'id', None) in PROCESS_MODULES):
                    unclear += 1
                continue
            dangerous = [s for s in strs if DANGEROUS_SQL.search(s)]
            if dangerous:
                hits.append('裸 SQL：%s' % dangerous[0][:40])
            elif any(_is_opaque_arg(a) for a in _call_args(node)) or not strs:
                # 语句内容在变量里 / 拼不出来 ⇒ 看不见它删什么，按可疑报。
                # 反过来，看得见是 `SELECT`/`COMMENT ON` 这类无害字面量的就不记 ——
                # 第 45 轮的误报（`server_default=sa.text(d)`）教过：**把闸门建成墙，
                # Render 每次启动都会退 4**，那比漏报更快被感觉到、也更容易被人为绕过。
                unclear += 1
    return hits, unclear


def _top_funcs(tree):
    """模块级的函数（`upgrade` / `downgrade` 只认这一层 —— 入口点是它，别把嵌套 def 当入口）。"""
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _all_funcs(tree):
    """本文件里定义的**每一个**函数体，含嵌套 def（第 46 轮 A-M5）。

    旧实现只收模块级，于是 `def upgrade(): def inner(): op.drop_table(…)` 这一支隐身：
    第一条循环用 `ast.walk` 还能撞见，第二条按"语句位置"筛的那一半就漏了。
    现在归属交给 `reachable()` 的调用图 + `ast.walk` 覆盖嵌套体，两类都不放过。
    """
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node)
    return out


def scan_migration(path):
    """一支迁移：有没有 upgrade/downgrade，**各自**会做什么危险动作。

    helper 的归属走到**不动点**（不限一跳）：`upgrade() → _a() → _b() → op.drop_table`
    这种两跳写法以前不算（A-M8/B-M2 各复现一次）。
    """
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        tree = ast.parse(fh.read(), filename=str(path))
    top, funcs = _top_funcs(tree), _all_funcs(tree)
    per_func, unclear = {}, {}
    for name, nodes in funcs.items():
        hits, bad = _classify_calls(nodes)
        per_func[name], unclear[name] = hits, bad

    def reachable(entry):
        """entry 直接或间接调用了哪些本文件 helper（含自己）。

        第 47 轮 B-1：以前只跟 `func.id`（裸名字），于是
        `class Legacy: def wipe(self): op.drop_table(...)` + `upgrade(): Legacy().wipe()`
        整条链隐身 —— 方法调用的 callee 是 `ast.Attribute`，`getattr(node.func,'id',None)`
        压根不是名字。**按调用图跟，不认写法**：`_a()`、`obj.wipe()`、`Legacy().wipe()`
        只要那个名字在本文件定义过，就算这一支会走到。
        """
        found, stack = {entry}, [entry]
        while stack:
            for fn in funcs.get(stack.pop(), []):
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    for called in (getattr(node.func, 'id', None),
                                   getattr(node.func, 'attr', None)):
                        if called in funcs and called not in found:
                            found.add(called)
                            stack.append(called)
        return found

    def unresolved(entry):
        """这一支往上走时，有多少处调用**我看不出它会做什么**（第 47 轮 B-1 / A6）。

        `from ._util import wipe_everything` 之后 `upgrade(): wipe_everything()` ——
        名字既不在本文件的 def 里、也不是内置函数，alembic 的 `op`/`sa`/`insp` 这些
        模块前缀更沾不上边。上一版这种调用**既不算命中也不算看不清**，审计回的是"干净"，
        而它自己的 docstring 写着"看不懂的按可疑报出来"。
        只对**裸名字**调用发难（属性调用挂在 `op.` / `insp.` 这类对象上，对象可能是
        本地变量或连接，硬判"看不清"会把 `insp.get_indexes()` 这种正常写法打死 ——
        那就是"把闸门建成墙"，与第 45 轮 `sa.text(d)` 那次误报同一课）。
        两条豁免要分开看：本文件 `def` 出来的 helper 看得见（`reachable()` 已经跟进去了）、
        内置函数看得见；**从别的模块 import 进来的裸名字看不见**（第 47 轮 B-1 的
        `from ._util import wipe_everything` ⇒ 上一版把它算成"认得的名字"而放过，
        等于跨模块就能把删除藏起来）。
        """
        local = set(funcs) | {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        import builtins as _py_builtins        # 模块级拿 `__builtins__` 时它是 dict 还是 module
        builtins_ = set(dir(_py_builtins))     # 取决于谁 import 了我，两种都有过
        imported, assigned = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {(a.asname or a.name).split('.')[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported |= {(a.asname or a.name) for a in node.names}
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        assigned.add(t.id)
        bad = 0
        for name in reachable(entry):
            for fn in funcs.get(name, []):
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        callee = node.func.id
                        if callee in local or callee in builtins_ or callee in assigned:
                            continue
                        if callee in imported:
                            bad += 1        # 看得见名字、看不见函数体 ⇒ 这一支会做什么我不知道
                            continue
                        bad += 1
        return bad

    def collect(entry):
        hits, bad = [], 0
        for name in reachable(entry):
            hits += per_func.get(name, [])
            bad += unclear.get(name, 0)
        return hits, bad

    up_hits, up_unclear = collect('upgrade') if 'upgrade' in top else ([], 0)
    down_hits, down_unclear = collect('downgrade') if 'downgrade' in top else ([], 0)
    up_unclear += unresolved('upgrade') if 'upgrade' in top else 0
    down_unclear += unresolved('downgrade') if 'downgrade' in top else 0
    # 根级别（模块顶层）直接执行的动作：`import` 这支迁移就跑了它，不等 upgrade()
    root_hits, root_unclear = _classify_calls(
        [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                    ast.ClassDef))])
    root_hits = ['顶层 %s' % h for h in root_hits]

    return {
        'file': os.path.basename(str(path)),
        'has_upgrade': 'upgrade' in top,
        'has_downgrade': 'downgrade' in top,
        'upgrade_drops': sorted(set(up_hits + root_hits)),
        'upgrade_unclear': up_unclear + root_unclear,      # 顶层那句 import 时就执行
        'downgrade_drops': sorted(set(down_hits)),
    }


def load_allowlist(path=None):
    """已登记的"往上走会丢数据"的迁移：`{文件名: 原因}`。空文件 = 一支都没有。

    原因必须是一句**读得通的话**：`null` / `{}` / `0` / `False` 这些 JSON 里能写的东西
    以前被 `str(v)` 变成 "None"/"{}"/"0" 就当"写了原因"（第 46 轮 B-minor-1）——
    登记不是盖章，空登记的净效果是给危险迁移发了通行证。
    """
    target = path or ALLOWLIST
    if not os.path.exists(target):
        return {}
    with open(target, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        return {str(k): (v.strip() if isinstance(v, str) else '') for k, v in data.items()}
    if isinstance(data, list):          # 只给了名字没给原因 ⇒ 交给 audit 去判"没写原因"
        return {str(k): '' for k in data}
    return {}


def _version_files(versions_dir):
    """alembic 眼里"存在的迁移文件"有哪些 —— **递归**（第 46 轮 B-M5）。

    旧写法用 `os.listdir` 只看顶层，而 alembic 自己收版本目录用的是
    `path_walk`（`os.walk`，递归）⇒ 子目录里放一支 `op.drop_table` 会被 apply，
    却从来没被这道闸看过，屏幕上照样印"已核对 N 支、0 处未登记"。
    两边看见的集合必须一致，所以这里按 alembic 的方式走。
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(versions_dir):
        dirnames[:] = [d for d in dirnames if d != '__pycache__']
        for fn in filenames:
            if not fn.endswith('.py') or fn.startswith('__'):
                continue
            full = os.path.realpath(os.path.join(dirpath, fn))
            rel = os.path.relpath(full, versions_dir).replace('\\', '/')
            found.append((rel, full))
    return sorted(set(found))


def audit(versions_dir=None, allowlist=None):
    """返回 `(问题列表, 扫过的支数)`。问题 = 未登记的丢数据迁移 / 缺 downgrade / 参数看不清。

    `allowlist` 传 `None` 时读的就是运行期那一份 JSON —— 测试与 `run_migrations.py` 因此
    共用同一张表（第 46 轮 B-M1：上一版测试传的是**测试文件自己的本地空集合**，
    于是"把一支迁移登记进 JSON"会让运行期放行、测试变红，错误消息还指挥人回去登记）。
    传 set/list 也认（那是"登记了名字没写原因"），不再 `AttributeError`（A-m8）。
    """
    versions_dir = versions_dir or VERSIONS
    if allowlist is None:
        registered = load_allowlist()
    elif isinstance(allowlist, dict):
        registered = allowlist
    else:                                        # set / list / tuple ⇒ 只登记了名字
        registered = {str(k): '' for k in allowlist}
    problems, seen = [], 0
    files = _version_files(versions_dir)
    if not files:
        # 一支都没看见 ≠ 干净。目录不存在 / 路径打错 / 递归没走对，都会让这道闸"满分通过"。
        return ['%s：这个目录里一支迁移都没看到 ⇒ 闸门没跑成，不能当成"没有危险迁移"'
                % versions_dir], 0
    for rel, full in files:
        seen += 1
        try:
            facts = scan_migration(full)
        except SyntaxError:
            problems.append('%s：解析不了，看不清它会不会删东西 ⇒ 按可疑处理' % rel)
            continue
        name = rel
        if not facts['has_upgrade']:
            problems.append('%s：没有 upgrade()（这支迁移 apply 时什么都不做？）' % name)
        if not facts['has_downgrade']:
            problems.append('%s：没有 downgrade() ⇒ 回滚无路可走' % name)
        if facts['upgrade_drops'] and name not in registered:
            problems.append('%s：往上走这一支会 %s ⇒ apply 即丢数据，未登记（登记处：`%s`，'
                            '值必须写清为什么非丢不可）'
                            % (name, '、'.join(facts['upgrade_drops']), REL_ALLOWLIST))
        elif facts['upgrade_drops'] and not (registered.get(name) or '').strip():
            problems.append('%s：已登记但没写原因（登记不是盖章，要写得清为什么非丢不可）' % name)
        if facts['upgrade_unclear'] and name not in registered:
            problems.append('%s：upgrade 里有 %d 处把 SQL 交给变量，我看不见内容 ⇒ '
                            '要么改成字面量让我看得见，要么登记并写明它删什么'
                            % (name, facts['upgrade_unclear']))
    return problems, seen
