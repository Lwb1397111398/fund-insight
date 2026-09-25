# -*- coding: utf-8 -*-
"""能改数据的脚本必须**在代码里**说清"我连的是哪个库"（第 28 轮 F-MINOR-6 起）。

`scripts/run_three_bucket_retention.py` 以前直接 `from src.models.database import SessionLocal`
且不调守卫 —— 而 `.env` 里的 `DATABASE_URL` 指向**生产 Supabase**。它是那批无守卫脚本里
唯一带硬删的：跑起来默认就在生产上算删除候选，还能 `--execute --confirm` 真删，
而回执里连"哪个库"都不印。

第 28 轮 H-MAJOR-1/2 抓到的正是本用例的上一版：它拿**整份文件正文**做正则，
于是把 `pin_local_sqlite` 写进 docstring、或只加一个 `add_argument("--against-production")`
就算"有守卫"—— 把真正的 pin 两行删掉，3 条用例照样全绿。
现在改成走 AST：只看代码里真实发生的调用/赋值，注释与文档字符串一律不算。
"""
import ast
import re
import sys
from pathlib import Path

import pytest    # 第 43 轮 A-MINOR-3：`test_the_scanned_set…` 走 `pytest.skip`，
                  # 而这个文件以前只在某个函数里 `import pytest as _pt` ⇒
                  # 真到"拿不到 git 名单"那一步时，先炸的是 `NameError` 不是 skip。

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'

WRITE_SWITCH = re.compile(r'--(apply|execute|hard-delete|import)\b')
HARD_DELETE = re.compile(r'CONFIRM_TOKEN|ThreeBucketRetentionService|three-buckets-hard-delete')
PROD_FLAG = re.compile(r'--against-production')
HTTP_WRITE = {'post', 'put', 'patch', 'delete'}   # 第 44 轮 A-M2：`delete` 以前不在表里，
                                        # 而 `requests.delete(url)` / `s.delete(url)` 就是 HTTP 侧最狠的动词
HTTP_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}   # urllib 那一族：方法藏在 method='POST' 里
OS_SHELL = {'system', 'popen', 'execv', 'execve', 'spawn', 'spawnl'}


def _target_is_hardcoded_local(call_node):
    """`create_engine(...)` / `sessionmaker(...)` 的目标**是不是代码里写死的本地文件**。

    第 42 轮 B-MAJOR-3：上一版认的是"文件里出现过 `DATABASE_URL` 这个字面量"，
    于是 `create_engine(settings.database_url)`（值仍来自那个环境变量）一次间接就隐身。
    写死在本地的两种形状放过：字面量 `sqlite:…`，以及 `'sqlite:///' + 副本路径` 这种拼接
    （`replay_verifications_on_copy.py` 用，目标不可能是生产库）。除此之外一律算"来自代码之外"。
    """
    first = call_node.args[0] if call_node.args else None
    if first is None:
        for kw in call_node.keywords:
            if kw.arg in ('bind', 'url', 'database'):
                first = kw.value
    if isinstance(first, ast.Constant):
        return isinstance(first.value, str) and first.value.startswith('sqlite')
    if isinstance(first, ast.BinOp):
        return any(isinstance(c, ast.Constant) and isinstance(c.value, str)
                   and c.value.startswith('sqlite') for c in ast.walk(first))
    return False


def _docstring_consts(tree):
    """模块/类/函数体第一句字符串常量 —— 那是**说明文**，不是代码在做的事。

    第 43 轮 A-MAJOR-1 复现出来的洞：`_facts` 以前用 `ast.walk` 把所有 `ast.Constant`
    收进 `consts`，而 docstring **就是**一个 `Expr(Constant(str))` 节点。于是
    "在 docstring 里写一句 `postgres…` + 在 docstring 里写一句 `[目标] …`"
    就能让三个识别器（`_refuses_remote_without_a_flag` / `_declares_http_target` /
    读侧第④档）一起点头。而 `_refuses_remote_without_a_flag` 自己的 docstring 写着
    "写在注释或 docstring 里不算" —— 那句话当场是假的。
    """
    nodes = set()
    holders = [tree] + [n for n in ast.walk(tree)
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    for h in holders:
        body = getattr(h, 'body', [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            nodes.add(id(body[0].value))
    return nodes


DML_WORDS = re.compile(r'^\s*(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|MERGE)\b',
                       re.I)
DBAPI_MODULES = ('psycopg2', 'psycopg', 'sqlite3', 'pymysql', 'MySQLdb', 'cx_Oracle', 'oracledb')
# 第 44 轮 A-M2/B-M2 把三张名字表补宽：`os.replace` 是最省事的"一句话换掉整个镜像"，
# `engine_from_config` 是 alembic 自己的官方入口（`alembic/env.py:101` 就用它），
# `create_async_engine` / `Session(bind=…)` 是同一件事的另一种写法。
FILE_WRITERS = {'copyfile', 'copy', 'copy2', 'copytree', 'move', 'rename', 'remove', 'unlink',
                'rmtree', 'write_text', 'write_bytes', 'truncate', 'replace'}
ENGINE_FACTORIES = {'create_engine', 'sessionmaker', 'engine_from_config', 'create_async_engine',
                    'create_engine_from_config'}
PROC_WRAPPERS = {'run', 'Popen', 'call', 'check_call', 'check_output', 'system', 'popen',
                 'execv', 'execve', 'spawn', 'spawnl'}
GENERIC_HTTP = {'request'}      # `requests.request("DELETE", url)` / `httpx.request(...)`
DYNAMIC_IMPORTERS = {'import_module', '__import__', 'run_module', 'run_path', 'exec', 'eval'}
PRINTERS = {'print', 'log', 'warning', 'warn', 'info', 'error', 'debug', 'exception', 'write',
            'writeln', 'echo'}
# "借道子进程"的判据有**两条**（第 45 轮 B-M-5：以前只有一条按名字的，而注释承诺的那条对齐
# 判据压根不存在 —— 实测真扫描判为"能改数据"的 25 个脚本里有 10 个不在这张名单上）：
#   ① 指名道姓：命令里出现 alembic / `-m src` / `scripts/<名单里的写脚本>.py`；
#   ② 不指名：命令指着 `scripts/任何一个.py` 并且带着写开关（`--apply`/`--confirm`/…）。
# ②不依赖名单，所以新加一个写脚本不必改这里也不会隐身；①留着是因为有些写口只靠
# `--production` 这类开关区分动静，名字仍是线索。名单本身由
# `test_the_subprocess_borrow_list_is_not_a_stale_gate` 与真扫描结果对齐（只许变短或持平）。
KNOWN_WRITERS = (r'run_migrations|sync_db_columns|purge_junk_funds|purge_test_rows_from_prod|'
                 r'push_sector_mappings_to_prod|import_export|seed_sector_mappings|'
                 r'seed_owner_proxies|sync_sector_map_funds|repair_replay_side_effects|'
                 r'restore_prediction_batch|resync_verdict_scalars|revert_degenerate_verdicts|'
                 r'run_three_bucket_retention|drop_probe_residue')
# ⚠ 上面这张名单**一定会过期**（第 45 轮 B-M-5 实测：真扫描判为"能改数据"的 25 个脚本里
# 有 10 个不在名单上，而注释里承诺的那条对齐判据压根没被写出来）。所以借道检测不依赖名单：
# **叫不出是谁也行**，只要那一条命令指着 `scripts/某个脚本.py` 并且带着写开关。
# 名单留着当第二条证据（`--production` 这类没写开关却会动的调用），两条任一命中即算能写。
BORROWED_WRITER = re.compile(r'scripts[/\\][\w.]+\.py')
BORROW_IS_WRITING = re.compile(r'--(apply|execute|hard-delete|import|write|production)\b')
# `[目标]` 这一类自报要**真被印出来**才算（第 44 轮 B-M5：`LABEL = '[目标] 线上生产库'`
# 是一个从不被打印的死赋值，操作者一个字都看不见，守卫却判"声明了目标"）。
TARGET_WORDS = ('[目标]', '[target]', '[TARGET]', '[Target]')
# 所有"能把状态落到某个库/文件上"的能力类别（第 43 轮 B 的盲区清单逐类补）。
# `engine_from_env` / `orm_session` / `own_engine` **不在这里** —— 它们是"读侧那道闸"的触发条件
# （见 `test_read_side_scripts_also_say_which_database_they_read`），把它们算成"能写"会
# 把三个纯读脚本推进写侧的受管集合，而 `_guarded` 认的信号里没有 `read_only_connect`。
WRITE_CAPABILITIES = {'raw_sql_write', 'schema_ddl_call', 'ddl_via_subprocess', 'bulk_replace',
                      'dbapi_direct', 'file_overwrite', 'http_write', 'opaque_exec'}
ALL_CAPABILITIES = WRITE_CAPABILITIES | {'engine_from_env', 'orm_session', 'own_engine',
                                         'alembic_import'}


def _strings_of(node):
    """表达式里出现过的所有字符串常量（含 list/tuple/f-string 片段/关键字参数）。"""
    return [c.value for c in ast.walk(node)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)]


def _strings_in_call(node):
    """一个 `Call` 的参数（位置 + 关键字）里的字符串常量。"""
    out = []
    for a in getattr(node, 'args', []):
        out += _strings_of(a)
    for k in getattr(node, 'keywords', []):
        out += _strings_of(k.value)
    return out


def _scheme_words(strings):
    """一批字符串里的**方向**信息：`postgres`（含 supabase）/ `sqlite` / `mysql`。

    护栏要看的就是这个：第 44 轮两席各自复现出"三件事在同一文件里共存"的旧判据能被
    装饰化满足 —— 现在要求方向字样出现在**那条分支的判断条件里**。
    """
    out = set()
    for s in strings or []:
        low = (s or '').lower()
        if 'postgres' in low or 'supabase' in low:
            out.add('postgres')
        if 'sqlite' in low:
            out.add('sqlite')
        if 'mysql' in low:
            out.add('mysql')
    return out


def _own_returns(fn):
    """这个函数**自己**的出口（不钻嵌套函数 —— 那是别人的 return）。

    `try` 的 `handlers` 与 `orelse` **算出口**（第 46 轮 A-M2）：
    `try: return "sqlite:///a.db" / except Exception: return os.environ["DATABASE_URL"]`
    兜底那条也是这条 helper 会交出去的值 —— 出事才走的那条路恰恰最可能给出生产串。
    注意这与 `_guard_stmts`（"护栏不许只挂在 except 里"）不矛盾：那边问的是
    "正常路径会不会停下来"，这边问的是"这个 helper 可能返回什么"。两个问题
    共用一个"走不走 except"的开关才是 bug，所以各自写清自己判的是什么。
    """
    out = []

    def walk(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(st, ast.Return):
                out.append(st)
            for field in ('body', 'orelse', 'finalbody'):
                walk(getattr(st, field, None) or [])
            if isinstance(st, ast.Try):
                for handler in st.handlers:
                    walk(handler.body)

    walk(fn.body)
    return out


def _literal_sqlite(node):
    """这个表达式**本身**是不是一个 sqlite 串（或以它开头的拼接片段）。

    判的是"值长什么样"，不是"值里出没出现过 `sqlite` 这六个字"（第 46 轮 A-M1 / B-M3）：
    旧写法 `'sqlite' in ' '.join(_strings_of(v)).lower()` 会被
    `"sqlite:///data/copy.db" if a.local else os.environ["PROD_DB_URL"]`
    这种"本地/生产开关"白送 —— 屏幕上跑出来的可以是生产串，判据却说方向已证明。
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.lower().startswith('sqlite')
    if isinstance(node, ast.JoinedStr):                       # f"sqlite:///{path}"
        first = next((v for v in node.values if isinstance(v, ast.Constant)
                      and isinstance(v.value, str)), None)
        return first is not None and first.value.lower().startswith('sqlite')
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_sqlite(node.left)                     # "sqlite:///" + 路径
    if isinstance(node, ast.Call):                            # os.path.join('sqlite:///', …)
        first = node.args[0] if node.args else None
        return first is not None and _literal_sqlite(first)
    return False


def _reads_runtime_state(node):
    """表达式里有没有"到运行时才决定"的子式：环境、配置对象、函数参数、外部调用。

    有 ⇒ 这条赋值指向哪儿我说了不算，一律按"方向不明"处理（不再因为字面里带 sqlite 就放行）。
    `os.environ[...]`、`os.getenv(...)`、`.get(k, 默认)`、`settings.X`、`request.args[...]` 都算。
    """
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in ('environ', 'env', 'config', 'settings'):
            return True
        if isinstance(n, ast.Call):
            fn = getattr(n.func, 'attr', None) or getattr(n.func, 'id', None)
            if fn in ('getenv', 'get', 'read_env', 'popdefault'):
                return True
    return False


def _returns_sqlite(fn, tree=None):
    """这个 helper 的**每一条出口**都返回 sqlite 串吗？

    第 45 轮两席同点（A-M2 / B-M1⑨）：上一版问的是"函数体里任何地方出现过 `sqlite` 字样"，
    于是**函数 docstring 写一句**"这只库是 sqlite 镜像"就能买到"方向能证明"。
    第 46 轮 A-M1/B-M3 再收一格：以前"这条 return 的表达式里含 sqlite 字样"就算，
    一条 return 里写两臂（三目）也能过 ⇒ 现在**每一臂都得单独证明**（`_proves_sqlite`）。
    """
    rets = _own_returns(fn)
    if not rets:
        return False
    for r in rets:
        if r.value is None or not _proves_sqlite(r.value, tree if tree is not None else fn):
            return False
    return True


def _guard_stmts(body):
    """分支体里**正常路径会走到**的语句：不钻嵌套函数，也不钻 `except`。

    第 45 轮 B-M1 的两个复现：① 分支里放一个从不调用的 `def _unused(): return 4`
    就替整条分支满足了"停下来"；② `[abort]` 只挂在分支内的 `except` 里
    （正常路径什么都不拦）也算护栏。两条都被这一层挡掉。
    """
    out = []

    def walk(stmts):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue            # 定义了 ≠ 会跑
            out.append(st)
            if isinstance(st, ast.Try):
                walk(getattr(st, 'body', None) or [])
                walk(getattr(st, 'finalbody', None) or [])
                continue            # handlers / orelse 是"出事了才走"的路径，不算
            for field in ('body', 'orelse', 'finalbody'):
                walk(getattr(st, field, None) or [])

    walk(list(body or []))
    return out


def _try_const(node):
    """能整体求成常量就返回值，否则 None（**不 eval**，只认字面量与简单组合）。"""
    try:
        return ast.literal_eval(node)
    except Exception:                                       # noqa: BLE001 求不动就当看不见
        return None


def _is_dead_test(test):
    """这条分支会不会被走到？—— 条件恒假就是**死路**，死路里的 `[abort]` 不挡任何事。

    第 45 轮 B-M1① 立的是"`and False` 也算死"，第 46 轮 A-M3 指出实现退化成两种拼写：
    现在按**求值**判（见 `_const_false`），`while 0:` / `if ():` / `if 1 == 0:` 一起收口。
    """
    if test is None:
        return True
    if isinstance(test, ast.Name):
        return test.id in ('False', 'None')
    folded = _try_const(test)
    if folded is not None:
        return not folded
    if isinstance(test, ast.Compare):
        left, right = _try_const(test.left), [_try_const(c) for c in test.comparators]
        if len(right) == 1 and right[0] is not None and left is not None:
            op = type(test.ops[0])
            try:
                same = left == right[0]
            except Exception:                               # noqa: BLE001
                return False
            if op is ast.Eq:
                return not same
            if op is ast.NotEq:
                return same
            if op is ast.Is:
                return left is not right[0]
            if op is ast.IsNot:
                return left is right[0]
            try:
                if op is ast.Lt:
                    return not (left < right[0])
                if op is ast.Gt:
                    return not (left > right[0])
                if op is ast.LtE:
                    return not (left <= right[0])
                if op is ast.GtE:
                    return not (left >= right[0])
            except TypeError:
                return False
        return False
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        return any(_is_dead_test(v) for v in test.values)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _try_const(test.operand)
        return inner is not None and bool(inner)
    return False


def _proves_sqlite(value_node, tree, _seen=None):
    """`os.environ['DATABASE_URL'] = <这个表达式>` 的结果**能不能证明是 sqlite**。

    第 44 轮 B-M4 把"赋过值就算守卫"打掉了；第 45 轮 A-M1 又打掉"按变量名猜方向"；
    第 46 轮 A-M1 / B-M3 打掉最后一条宽松："表达式里**出现过** sqlite 字样"。
    现在只认三种形状，且**每一臂都要单独证明**：
      ① 字面量本身以 `sqlite` 开头（含 `"sqlite:///" + 路径` 这种拼接、f-string）；
      ② 名字：本文件里**每一次**赋值都能证明（有一次赋成别的东西 ⇒ 取决于跑到哪一行）；
      ③ 一跳之内的本文件 helper：它的**每条 return** 都能证明（含 except 里那条）。
    三目 / `or` / `and` 这类多臂表达式 ⇒ 要求**所有臂**都能证明；
    只要表达式里出现"到运行时才决定"的子式（环境、配置、`.get(k, 默认)`）就整体不算证明。
    两跳以上、跨文件也不认 —— 那种情况让脚本自己去印 `[库]` 或调 `pin_local_sqlite()`。

    顺序也是判据的一部分（第 46 轮我自己第一版搞反过）：**先解析 helper 的出口，再看有没有
    运行时子式**。反过来先问"这坨里有没有 `os.environ`"，会把
    `url(bool(os.environ.get("L")))` 这种"参数来自环境、但每一条出口都返回 sqlite"的诚实写法
    一起打死 —— 那等于把闸门建成墙，下一步就是有人去把它拆了。
    """
    seen = _seen or frozenset()
    if value_node is None:
        return False
    if isinstance(value_node, ast.Call):
        fn_name = getattr(value_node.func, 'id', None) or getattr(value_node.func, 'attr', None)
        if fn_name and fn_name not in seen:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name == fn_name \
                        and _returns_sqlite(node, tree):
                    return True
    if _literal_sqlite(value_node):
        return True
    if isinstance(value_node, ast.IfExp):
        return _proves_sqlite(value_node.body, tree, seen) and \
            _proves_sqlite(value_node.orelse, tree, seen)
    if isinstance(value_node, ast.BoolOp):
        return all(_proves_sqlite(v, tree, seen) for v in value_node.values)
    if _reads_runtime_state(value_node):
        return False                                  # 环境/配置说了算 ⇒ 我不猜
    if isinstance(value_node, ast.Name):
        if value_node.id in seen:
            return False                              # 自引用（`u = u or …`）⇒ 不证明
        assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == value_node.id for t in n.targets)]
        if not assigns:
            return False                              # 名字来自参数/外层 ⇒ 值我不知道
        return all(_proves_sqlite(a.value, tree, seen | {value_node.id}) for a in assigns)
    return False


def _scope_map(tree):
    """每个 AST 节点属于哪个函数（**最内层**优先），用来判"护栏与危险动作谁在前面"。"""
    scopes = {}
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for fn in sorted(funcs, key=lambda f: (f.end_lineno or f.lineno) - f.lineno, reverse=True):
        label = fn.name
        for sub in ast.walk(fn):
            scopes.setdefault(id(sub), label)
    return scopes


# 一句调用"是不是会把状态写出去"。这张表**故意窄**：它只用来判顺序（护栏必须排在它前面），
# 判"能不能改数据"另有 `capabilities` 那张按类别枚举的表。窄一点的代价是漏（某些写动词
# 不进这张表 ⇒ 那条顺序判据对它不起作用），而不是冤枉真护栏 —— 这个方向上我选保守。
DANGEROUS_CALLS = {'upgrade', 'downgrade', 'stamp', 'create_all', 'drop_all', 'commit', 'flush',
                   'execute', 'exec_driver_sql', 'to_sql', 'bulk_insert', 'bulk_save_objects',
                   'run', 'Popen', 'popen', 'system', 'check_call', 'check_output', 'urlopen',
                   'post', 'put', 'patch', 'request', 'truncate', 'add_all', 'delete_all',
                   'copyfile', 'copy2', 'unlink', 'remove'}


def _dangerous_lines(tree, scopes, alias):
    """`(作用域, 行号)` 列表：这个文件里每一处"会把状态写出去"的调用点。"""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
        resolved = alias.get(name or '', name or '')
        strs = _strings_in_call(node)
        hit = resolved in DANGEROUS_CALLS
        if not hit and resolved in PROC_WRAPPERS:
            hay = ' '.join(strs)      # 路径与写开关是**两个**参数，逐个匹配配不上
            if re.search(r'\balembic\b|-m\s+alembic|-m\s+src\b|--init-db|' + KNOWN_WRITERS,
                         hay) or (BORROWED_WRITER.search(hay) and BORROW_IS_WRITING.search(hay)):
                hit = True                   # 子进程借道 alembic / 别的写脚本
        if not hit and resolved in FILE_WRITERS and any('.db' in s or '.env' in s for s in strs):
            hit = True                       # 覆盖 db 文件 / 重写 .env
        if not hit and resolved == 'open' and any(
                '.db' in s or '.env' in s for s in strs):
            holder = node.args[1] if len(node.args) > 1 else \
                next((k.value for k in node.keywords if k.arg == 'mode'), None)
            hit = bool(holder is not None
                       and any(v[:1] in ('w', 'a', 'x') for v in _strings_of(holder)))
        if hit:
            out.append((scopes.get(id(node), '<module>'), node.lineno))
    return out


def _stmt_abort_evidence(stmt):
    """这条**直接语句**里：有没有把拒绝的原因说出来（`[abort]` / `[拒`），有没有停下来。

    不钻嵌套函数、也不钻 `except` 体（第 45 轮 B-M1 的④⑤两个复现：
    定义了从不调用的 `def _unused(): return 4` 替分支满足"停下来"；
    `[abort]` 只挂在 except 里，正常路径什么都不拦）。
    """
    says = stop = False
    stack = [stmt]
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                            ast.ExceptHandler, ast.comprehension)):
            continue
        name = None
        if isinstance(cur, ast.Call):
            name = getattr(cur.func, 'id', None) or getattr(cur.func, 'attr', None)
            said = any(isinstance(c, ast.Constant) and isinstance(c.value, str)
                       and c.value.startswith(('[abort]', '[拒')) for c in ast.walk(cur))
            if name in PRINTERS and said:
                says = True
            if name in ('exit', '_exit') and any(
                    isinstance(a, ast.Constant) and isinstance(a.value, int) and a.value
                    for a in cur.args):
                stop = True
        if isinstance(cur, ast.Raise) and isinstance(cur.exc, ast.Call):
            stop = True
            if any(isinstance(c, ast.Constant) and isinstance(c.value, str)
                   and c.value.startswith(('[abort]', '[拒')) for c in ast.walk(cur)):
                says = True
        if isinstance(cur, ast.Return) and isinstance(cur.value, ast.Constant) \
                and isinstance(cur.value.value, int) and cur.value.value != 0:
            stop = True
        for _field, value in ast.iter_fields(cur):
            if isinstance(value, list):
                stack.extend(v for v in value if isinstance(v, ast.AST))
            elif isinstance(value, ast.AST):
                stack.append(value)
    return says, stop


def _facts(py):
    """从 AST 里取"代码真正做了什么"，不是"文件里出现过哪些字"。

    第 43 轮把两件东西补进来了：
    ① **docstring 不算**（见 `_docstring_consts`），且"拒跑"必须是**条件分支里**的 raise
      —— `raise SystemExit(main())` 那句入口样板以前被当成"这脚本会主动拒跑"；
    ② **能力按类别枚举**（`capabilities`）：第 42/43 轮两份评审各自独立指出，
      判据只认"SQLAlchemy + 顶层 import + 字面量旗子"这一种形状，而落笔的路还有
      `create_all` / 裸 SQL DML / DB-API 直连 / `to_sql(if_exists='replace')` /
      覆盖 db 文件或 `.env` 的文件操作 / 子进程借道 alembic / `requests.request('DELETE')`。
      漏一类 ＝ 那一类永远免检（第 37 轮 B 就这么教过我一次）。
    """
    text = py.read_text(encoding='utf-8', errors='replace')
    tree = ast.parse(text, filename=str(py))
    prose = _docstring_consts(tree)
    called, flags, raised, consts, direct_db = set(), set(), set(), set(), False
    env_written = env_written_unknown = False
    alembic = False      # `from alembic import command` / `import alembic...`
    reads_url_env = False    # 读 `DATABASE_URL` / `ALEMBIC_DATABASE_URL` 这两个名字
    builds_engine = False    # 自己建 engine / session 工厂
    builds_external_engine = False    # …且目标不是代码里写死的本地 sqlite 路径
    capabilities = set()
    printed_targets = set()
    called_printed = set()
    src_imports = set()
    alias = {}          # `from _db_guard import pin_local_sqlite as _pin_x` 也要认得出来

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ('_db_guard', 'sqlalchemy'):
            # sqlalchemy 那一支是第 43 轮 B-MAJOR-1：`from sqlalchemy import create_engine as ce`
            # 一个别名就让 `engine_from_env` 变 False（上一版只对 `_db_guard` 的名字做还原）。
            for a in node.names:
                alias[a.asname or a.name] = a.name
    # `scheme = url.split('://')[0]` 这一类"一步前驱赋值"：护栏条件里只写 `scheme.startswith(...)`
    # 时，方向信息在上一行。认一层变量赋值，别把真护栏误判成没护栏（第 44 轮 A-m1 的误报面）。
    scheme_from_name = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            words = {w for w in _scheme_words(_strings_of(node.value))}
            if words:
                scheme_from_name[node.targets[0].id] = words

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, 'id', None) or getattr(func, 'attr', None)
            resolved = alias.get(name or '', name or '')
            if resolved:
                called.add(resolved)
            strs = []
            for a in node.args:
                strs += _strings_of(a)
            for k in node.keywords:
                # 关键字参数也要看（第 44 轮 A-M2）：`requests.request(method="DELETE", url=u)`、
                # `c.execute(statement="TRUNCATE …")`、`shutil.copyfile(dst="data/fund_insight.db")`
                # 都只是换了写法，语义一个字没变。
                strs += _strings_of(k.value)
            if resolved in ENGINE_FACTORIES:
                builds_engine = True
                if not _target_is_hardcoded_local(node):
                    builds_external_engine = True       # 含 `create_engine(settings.database_url)`
                    capabilities.add('engine_from_env')
            if resolved == 'Session' and (node.args or any(k.arg == 'bind' for k in node.keywords)):
                builds_engine = True                    # `Session(bind=<远程>)` 同样是"连上了"
                capabilities.add('own_engine')
            if resolved in DYNAMIC_IMPORTERS:
                # 动态 import / exec 不是"另一种语言"，它就是同一条 import 语句换了写法
                # （第 45 轮 B-m4：`importlib.import_module("src.models.database")`
                #  + `SessionLocal().query(...)` 的纯读脚本，上一版碰库触发一条都不响）。
                joined = ' '.join(strs)
                for token in re.findall(r'src(?:\.[A-Za-z0-9_]+)+', joined):
                    src_imports.add(token)
                    if token.startswith('src.models'):
                        direct_db = True
                        capabilities.add('orm_session')
                if resolved in ('exec', 'eval') and any(
                        w in joined for w in ('commit', 'SessionLocal', 'create_engine',
                                              'CREATE ', 'DROP ', 'INSERT ', 'UPDATE ',
                                              'DELETE ', 'TRUNCATE')):
                    capabilities.add('opaque_exec')   # 字符串里写着落笔动作：按能写处理
            if name == 'add_argument':
                flags.update(s for s in strs if s.startswith('-'))
            if resolved in ('create_all', 'drop_all'):
                # `drop_all` 比 `create_all` 更危险，以前表里只写了 create_all（A44 M2）
                capabilities.add('schema_ddl_call')
            if resolved in ('execute', 'exec_driver_sql', 'text') \
                    and any(DML_WORDS.search(s) for s in strs):
                capabilities.add('raw_sql_write')            # 裸 SQL 写：串里有 DML/DDL 动词
            if resolved == 'to_sql':
                capabilities.add('bulk_replace')             # df.to_sql(if_exists='replace')
            if resolved in DBAPI_MODULES and name == 'connect':
                capabilities.add('dbapi_direct')             # sqlite3.connect / psycopg2.connect
            if resolved in FILE_WRITERS and any('.db' in s or '.env' in s for s in strs):
                capabilities.add('file_overwrite')       # 覆盖 db 文件 / 重写 .env
            if resolved == 'open':
                # 判"写模式"要看**模式参数**：上一版拿 'w'/'a' 去子串匹配所有参数，
                # 于是 `'a' in 'data/fund_insight.db'` 恒真 —— 任何纯读都算写（第 44 轮 A-M9）。
                holder = node.args[1] if len(node.args) > 1 else \
                    next((k.value for k in node.keywords if k.arg == 'mode'), None)
                if holder is not None and any(v[:1] in ('w', 'a', 'x') for v in _strings_of(holder)) \
                        and any('.db' in s or '.env' in s for s in strs):
                    capabilities.add('file_overwrite')
            if resolved in PROC_WRAPPERS:
                # **同一个调用**的参数拼起来看：`subprocess.run([sys.executable,
                # "scripts/x.py", "--apply", "--confirm", T])` 里路径与开关是**两个**常量，
                # 逐个匹配永远配不上（第 45 轮我自己第一条控制断言当场抓出来）。
                hay = ' '.join(strs)
                if re.search(r'\balembic\b|-m\s+alembic|-m\s+src\b|--init-db|' + KNOWN_WRITERS,
                             hay) or (BORROWED_WRITER.search(hay) and BORROW_IS_WRITING.search(hay)):
                    # 两条任一命中即算"借道写库"：第二条不看名字，只看"指着 scripts/ 里的脚本
                    # 并让它去写"（名单会过期，这条不会 —— 见 KNOWN_WRITERS 上面那段）
                    capabilities.add('ddl_via_subprocess')
            if resolved in GENERIC_HTTP and any(s.upper() in HTTP_METHODS for s in strs):
                capabilities.add('http_write')               # requests.request('DELETE', …)
            if (name in PRINTERS) or (getattr(func, 'value', None) is not None
                                      and getattr(getattr(func, 'value', None), 'attr', None)
                                      in ('stdout', 'stderr')):
                # "声明了目标"必须是**真会被印出来**的一行（第 44 轮 B-M5 / A-M1）：
                # 死赋值 `LABEL = '[目标] 线上生产库'` 买不到守卫。
                printed_targets.update(s for s in _strings_in_call(node) if s.startswith(TARGET_WORDS))
                # `print(_target_line(base))` 也算：常量在 helper 的 Return 上，
                # 但这个 helper 确实被印了出来（`push_sector_mappings_to_prod.py` 就是这个形状）。
                for c in ast.walk(node):
                    if isinstance(c, ast.Call):
                        called_printed.add(getattr(c.func, 'id', None) or '')
                    elif isinstance(c, ast.Name):
                        called_printed.add(c.id)
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            exc = node.exc.func
            raised.add(getattr(exc, 'id', None) or getattr(exc, 'name', '') or '')
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in prose:
                continue        # docstring 里的字**不算**证据
            consts.add(node.value)
            if node.value in ('DATABASE_URL', 'ALEMBIC_DATABASE_URL'):
                reads_url_env = True
        elif isinstance(node, (ast.ImportFrom, ast.Import)):
            mod = getattr(node, 'module', None) or ''
            if mod.startswith('src.models'):
                direct_db = True
            if mod.startswith('src'):
                src_imports.add(mod)
            for al in node.names:
                full = (mod + '.' + al.name) if mod else al.name
                if full.startswith('src.models'):
                    direct_db = True
                if full.startswith('src'):
                    src_imports.add(full)
                if (al.name or '').split('.')[0] == 'alembic' or mod.split('.')[0] == 'alembic':
                    alembic = True
                if al.name == 'main' and mod == 'alembic.config':
                    capabilities.add('schema_ddl_call')      # alembic 官方 CLI 入口
        elif isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Subscript):
            t = node.targets[0]
            if isinstance(t.value, ast.Attribute) and t.value.attr == 'environ':
                key = t.slice
                if isinstance(key, ast.Constant) and key.value in ('DATABASE_URL', 'LOCAL_DB_URL'):
                    # "自设 DATABASE_URL"只有**方向能证明**才算守卫（第 44 轮 B-M4）：
                    # `os.environ['DATABASE_URL'] = os.environ['MAINT_DB_URL']` 赋的可以是生产串，
                    # 旧判据把"赋过值"当"有守卫"。方向不明的赋值仍然记录，但不计分。
                    # 第 46 轮 A-M1 / B-M3 收的最后一格：以前这里先问一句
                    # `'sqlite' in 表达式里的字符串`，于是
                    # `= "sqlite:///data/copy.db" if a.local else os.environ["PROD_DB_URL"]`
                    # 与 `= os.environ.get("X", "sqlite:///x.db")` 都能白买到"方向已证明"——
                    # 屏幕上跑出来的完全可以是生产串。现在**只走 `_proves_sqlite`**：
                    # 表达式里凡有"到运行时才决定"的子式（环境 / 配置 / `.get(k, 默认)`），
                    # 多臂表达式里有一臂证明不了，就一律算方向不明。
                    # `pin_local_sqlite()` 仍然算，但那是"它自己调了钉库函数"这件事实，
                    # 不是"字符串里出现过这几个字"。
                    calls = {getattr(n.func, 'id', None) or getattr(n.func, 'attr', None)
                             for n in ast.walk(node.value) if isinstance(n, ast.Call)}
                    if 'pin_local_sqlite' in calls or _proves_sqlite(node.value, tree):
                        env_written = True
                    else:
                        env_written_unknown = True
    # 护栏 = **同一条分支**里做到四件事，而且这条分支**真的挡在危险动作前面**：
    #   ① 判断条件看的是库的方向（不是 help 文案、不是说明文、不是恒假条件）；
    #   ② 分支体的正常路径上印了 `[abort]`；③ 同一条正常路径上停下来；
    #   ④ 这条分支**可达**（从模块顶层真能走到它，不是待在一个没人调用的函数里）；
    #   ⑤ 它的执行时刻**早于**任何一处危险动作 —— 而且这条比较要走**调用图**，
    #     不能只比"同一个最内层函数里的行号"（第 46 轮 A-M4：把 `command.upgrade(cfg)`
    #     抽成一个 helper、主流程先调用它、再判方向，当时仍判"有守卫"；
    #     另一半：护栏只被一个从不被调用的函数调用，也算"可达"）。
    refusals = []
    scopes = _scope_map(tree)
    danger = _dangerous_lines(tree, scopes, alias)
    sites = {}                                    # 函数名 → [(调用发生处的作用域, 行号)]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = getattr(node.func, 'id', None)
            if fn:
                sites.setdefault(fn, []).append((scopes.get(id(node), '<module>'),
                                                 getattr(node, 'lineno', 0)))

    def exec_paths(scope, line, seen=()):
        """这一行**什么时候被执行**：一条从模块顶层走到它的调用路径，写成行号元组。
        模块顶层就是 `(line,)`；函数里的第 L 行 = 调用它的那条路径再接上 `L`。
        比较用字典序 —— 不能用"累加小数"，那样 `main:165` 会被算得比
        `main:133 → prod_conn:37` 更早（第 46 轮我自己第一版就是这么把一条真护栏弄红的）。
        空集 ＝ 这条路从模块顶层走不到。"""
        if scope == '<module>':
            return {(float(line),)}
        if scope in seen:
            return set()
        out = set()
        for caller_scope, caller_line in sites.get(scope, ()):
            for prefix in exec_paths(caller_scope, caller_line, seen + (scope,)):
                out.add(prefix + (float(line),))
        return out

    def first(a, b):
        """a 是否**保证**排在 b 前面（两条路径的字典序比较；取最早的那条）。"""
        pa, pb = a and min(a), b and min(b)
        return pa is not None and pb is not None and pa < pb

    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.ExceptHandler, ast.For, ast.While)):
            continue
        test = getattr(node, 'test', None)
        if isinstance(node, ast.ExceptHandler):
            test = None          # `except SomeError:` 判的是异常，不是库的方向
        if _is_dead_test(test):
            continue             # 永不成立的条件里写一百句 `[abort]` 也不挡任何事
        body = _guard_stmts(getattr(node, 'body', None))
        says_abort, stops = False, False
        for stmt in body:        # 只看**正常路径上的语句**：嵌套函数里那句 `return 4` 不算
            says, stop = _stmt_abort_evidence(stmt)
            says_abort = says_abort or says
            stops = stops or stop
        if not (says_abort and stops):
            continue
        scope = scopes.get(id(node), '<module>')
        line = getattr(node, 'lineno', 0)
        at = exec_paths(scope, line)
        if not at:
            continue             # 走不到这里的"护栏"＝没有护栏（含"只被死函数调用"那一族）
        if any(first(exec_paths(s, dl), at) for s, dl in danger):
            continue             # 危险动作排在护栏前面 ⇒ 这句话protect不了任何东西
        words = set(_scheme_words(_strings_of(test) if test is not None else []))
        if isinstance(test, ast.Name):
            words |= scheme_from_name.get(test.id, set())
        refusals.append({'words': words, 'line': line, 'scope': scope})
    # helper 返回值里的 `[目标]`：只有"这个 helper 被印过"才算自报
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in called_printed:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return):
                    printed_targets.update(s for s in _strings_of(sub) if s.startswith(TARGET_WORDS))
    if direct_db:
        capabilities.add('orm_session')
    if builds_engine or builds_external_engine:
        capabilities.add('own_engine')
    if alembic:
        capabilities.add('alembic_import')
    return {'text': text, 'called': called, 'flags': flags, 'raised': raised,
            'refusals': refusals, 'consts': consts, 'printed_targets': printed_targets,
            'env_written': env_written, 'env_written_unknown': env_written_unknown,
            'direct_db': direct_db, 'src_imports': src_imports,
            'capabilities': capabilities,
            'alembic': alembic,
            # 第 41 轮 B-MAJOR-1 立了"读侧也要问连哪儿"，但上一版的触发条件是
            # "文件里出现过 `DATABASE_URL` 这个字面量" —— 第 42 轮 B-MAJOR-3 指出那**一次间接就隐身**：
            # `create_engine(settings.database_url)` 里压根没出现过那个字面量，值却仍是同一个环境变量。
            # 所以现在两路都算命中：① 读了那个环境变量并且建了 engine；② 直接看参数形状 ——
            # `create_engine` / `sessionmaker` 的目标**不是代码里写死的本地 sqlite 路径**。
            'engine_from_env': bool(builds_external_engine or (reads_url_env and builds_engine))}


def _tracked_scripts():
    """**受检集合必须由仓库定义，不由我这台机器的磁盘定义**（第 41 轮 A 席 M2 的续账）。

    上一版把这条修成"临时目录现造文件"验形状，但 `_scripts()` 仍然 `SCRIPTS.glob('*.py')`
    ⇒ 本机 53 个文件里有 7 个是被 `.gitignore` 掉的 `_tmp_*.py` 草稿，干净克隆只有 46 个
    —— 受管集合跟着磁盘变，"我这台机器全绿"与"这个仓库全绿"就不是同一句话（第 42 轮 A-MINOR-3）。
    现在以 `git ls-files` 为准；git 不可用时退回磁盘扫描并**明说**（宁可退让，不要静默换范围）。
    """
    import subprocess
    try:
        out = subprocess.run(['git', 'ls-files', '--', 'scripts'], cwd=str(SCRIPTS.parent),
                             capture_output=True, text=True, timeout=60)
        names = {Path(ln).name for ln in (out.stdout or '').splitlines()
                 if ln.endswith('.py') and Path(ln).parent.name == 'scripts'}
        if names:
            return names, 'git'
    except Exception:                                  # noqa: BLE001  没 git / 超时 / 不是仓库
        pass
    return {p.name for p in SCRIPTS.glob('*.py')}, '磁盘（git 不可用）'


def _scripts():
    tracked, source = _tracked_scripts()
    # 只有扫**真**目录时才按 git 过滤；`_scan_into` 会把 SCRIPTS 指到临时目录（那里的文件
    # 当然不在 git 里），那一类"现造文件验形状"的用例不能被这道过滤掉。
    real_dir = SCRIPTS.resolve() == (Path(__file__).resolve().parents[2] / 'scripts').resolve()
    _scripts.scope = (len(tracked), source, real_dir)
    out = {}
    for py in sorted(SCRIPTS.glob('*.py')):
        if real_dir and source == 'git' and py.name not in tracked:
            continue        # 未入库的 `_tmp_*.py` 草稿：本机有、干净克隆没有
        # 只放过 `_db_guard.py` 本身（它就是被大家调用的那把钉库守卫）。
        # 第 39 轮 B 说得对：以前 `startswith('_')` 是**整族豁免**，新写一个 `_x.py`
        # 的生产写脚本可以永远不进扫描 —— 豁免名单必须是"一个文件"，不是"一个前缀"。
        if py.name == '_db_guard.py':
            continue
        try:
            out[py.name] = _facts(py)
        except SyntaxError:
            # 解析不了的文件不能悄悄放过：标成"未受管"，让下面三条用例为它变红
            # 兜底 dict 必须与 `_facts` 返回的键**完全一致**，否则"标成未受管让下面变红"
            # 这句承诺是假的：扫描器会先 KeyError 崩掉（第 39 轮 A-MINOR-7 实测）。
            out[py.name] = {'text': py.read_text(encoding='utf-8', errors='replace'),
                            'called': set(), 'flags': set(), 'raised': set(),
                            'refusals': [{'words': {'postgres', 'sqlite'}, 'line': 0,
                                          'scope': '<module>'}], 'consts': set(),
                            'printed_targets': set(),
                            'env_written': False, 'env_written_unknown': True,
                            'direct_db': True, 'src_imports': set(), 'alembic': False,
                            'engine_from_env': True,     # 解析不了＝无法证明它不读环境
                            # 解析不了＝无法证明它不能写 ⇒ 把所有落笔能力**全点一遍**。
                            # 键集合与 `_facts` 的返回值必须一致，由
                            # `test_the_fallback_dict_cannot_lag_behind_the_facts_it_stands_in_for`
                            # 钉住（这一族漂过一次：兜底 dict 少一个键，判据 KeyError 崩掉扫描器）。
                            'capabilities': set(ALL_CAPABILITIES),
                            'broken': True}
    return out


_SCHEMA_ddL_VERBS = {'upgrade', 'downgrade', 'stamp'}


def _issues_schema_ddl(f):
    """alembic 的**任何**改结构动作都算，不只 `upgrade`。

    第 38 轮 B 的 BLOCKER 附带项：上一版我只认 `command.upgrade`，于是
    `command.downgrade(cfg, "base")`（`20260722_0002` 的 downgrade 是 `drop_table("prediction_change_logs")`，
    即审计台账本体）与 `command.stamp(...)` 都判"不能改结构"、不进守卫集合；
    拿 `subprocess` 起裸 `alembic` CLI 的那一路同样隐身。
    """
    via_api = bool(f.get('alembic')) and (f['called'] & _SCHEMA_ddL_VERBS)
    via_cli = any('alembic' in c for c in f['consts']) and bool(
        f['called'] & ({'run', 'Popen', 'call', 'check_call', 'check_output'} | OS_SHELL))
    # 第 43 轮 B 的盲区清单里两条最贵的：`Base.metadata.create_all(engine)`（不走 alembic 的建表）
    # 与"子进程借道 `run_migrations.py`/裸 alembic"（`via_cli` 只认字面量里有 `alembic` 字样）。
    via_caps = bool(f.get('capabilities', set()) & {'schema_ddl_call', 'ddl_via_subprocess'})
    return bool(via_api or via_cli or via_caps)


def _write_capable(f):
    """CLI 上有写开关 / 确认口令 / 直接执行删除的服务 / 跑迁移 —— 都算"能改数据"。"""
    if f.get('broken'):
        # 解析不了＝**无法证明它不能写** ⇒ fail-closed 按能写处理。
        # 以前只写"标成未受管，让下面三条用例为它变红"，实际是：兜底 dict 里没有写开关、
        # 也没有 commit/add/delete 调用 ⇒ 它压根进不了受管集合，解析失败被静默放过
        # （这条是我自己新写的判据当场抓出来的，第 39 轮 A-MINOR-7 的第二半）。
        return True
    if _issues_schema_ddl(f):
        return True
    if f.get('capabilities', set()) & WRITE_CAPABILITIES:
        # 第 43 轮 B 的"能力×判据"表：裸 SQL DML、DB-API 直连（psycopg2/sqlite3）、
        # `df.to_sql(if_exists='replace')`、覆盖 `.db`/`.env` 的文件操作、
        # `requests.request('DELETE', …)` 这种通用入口 —— 每一类都曾经"三道判据一条不响"。
        return True
    if f['direct_db'] and f['called'] & {'commit', 'add', 'delete'}:
        # 第 36 轮 B-MINOR-3：**什么写开关都没有、上来就 commit** 的脚本以前落在扫描集合外
        # （`scripts/seed_sector_mappings.py` 当时就是这个形状）。
        # "有没有开关"不该是"受不受管"的前提：会写库就得说清连的是哪个库。
        # 这条必须排在 `--confirm` 那个分支**前面**：加完它才发现旧顺序会短路
        # （`import_export.py` 带 `--confirm` 却没有硬删 ⇒ 老早退直接判"不受管"）。
        return True
    if f['called'] & HTTP_WRITE or ('urlopen' in f['called'] and f['consts'] & HTTP_METHODS):
        # 第 40 轮 A 席 M5：上一版只认 `post/put/patch`，而本仓那个生产写口用的是
        # `urllib.request.urlopen(Request(..., method='POST'))` ⇒ 触发器精确覆盖了"仓库里没有的形状"。

        # 第 39 轮 B：`_declares_http_target` 只做"守卫"、不做"触发"，于是
        # 一个不带 `--confirm` 字样的 HTTP 写口（口令写死在代码里也算）依旧全隐身。
        return True
    if any(WRITE_SWITCH.search(fl) for fl in f['flags']):
        return True
    if '--confirm' in f['flags']:
        # 第 38 轮两份复评交叉核对抓到的漏网：`push_sector_mappings_to_prod.py` 是这仓库里
        # **唯一往生产 POST 的写口**，而旧写法要求"文本里还得有硬删字样"才算受管 ⇒ 它整个不在集合里，
        # 连"你正在往哪儿写"都不用自己说。要口令才动 = 就是写操作，与删不删无关。
        return True
    if f['called'] & {'confirm', 'execute'}:
        return bool(HARD_DELETE.search(f['text']))
    return bool(HARD_DELETE.search(f['text']) and f['direct_db'])


def _refuses_direction(f, direction):
    """有没有一条分支**判的就是那个方向**并且停下来（第 44 轮 A-M1 / B-BL-1）。

    `direction` 取 `'postgres'` 或 `'sqlite'`：'见远程就拒跑' 与 '见本地就拒跑' 是两条
    相反的护栏，混用会互相顶包 —— 上一版三个识别器都只问"文件里有没有一处护栏"。
    """
    return any(direction in r['words'] for r in f.get('refusals') or [])


def _refuses_something(f):
    """"会主动拒跑"必须是**条件分支里**的 raise（第 43 轮 A-MAJOR-1）。

    以前判的是 `'SystemExit' in f['raised']`，而 `raised` 是全文件收集 ——
    仓库里几乎每个脚本末尾都有那句入口样板 `raise SystemExit(main())`，
    于是"一句护栏都没写"的脚本也被判成立（A 席用三个样品复现）。
    """
    return bool(f.get('refusals'))


def _says_target(f):
    """目标自报：既要在代码里（不是注释/docstring），又要在**打印的参数里**。

    第 45 轮 A-M4：读侧第④档以前还留一条 `or any(str(c).startswith(TARGET_WORDS) for c in
    f['consts'])` —— 于是 `LABEL = "[目标] 线上生产库（引擎级只读）"` 这种**从不被打印的死赋值**
    也算自报，而写侧早就只认 `printed_targets` 了。同一件事在两处各判一次、一边严一边松，
    松的那一边就是绕法。
    """
    return bool(f.get('printed_targets'))


def _refuses_remote_without_a_flag(f):
    """`sync_db_columns.py` 那一类：不 pin，但**默认见到远程就拒跑**，要远程必须显式加旗。

    这一条要的是真代码：常量里出现 `postgres` _scheme 判断 + **条件分支里**主动 raise，
    写在注释或 docstring 里不算（`_docstring_consts` 把说明文整段剔掉了）。
    """
    return (_refuses_direction(f, 'postgres')
            and any(PROD_FLAG.search(fl) for fl in f['flags']))


# Render 上跑的**生产入口**：它就该连生产，护栏是"把库名印进日志"，不是钉镜像。
PRODUCTION_ENTRY = {'run_scheduled_tasks.py'}

# **只**面向生产的修复脚本（它要清的就是线上数据，"默认钉镜像"对它没意义）。
# 护栏必须反过来：见 SQLite 就拒跑 + 必须显式 `--production`。名字写错（文件不存在）
# 会被下面那条反空判用例当场抓住，所以这张名单不是绕过闸门的后门。
PRODUCTION_ONLY = {'purge_test_rows_from_prod.py'}


def _refuses_local_without_a_flag(f):
    """与 `_refuses_remote_without_a_flag` 对称：常量里判 `sqlite` + 主动 `raise SystemExit`
    + CLI 上有 `--production` 旗子，三者齐了才算"靶子声明清楚了"。"""
    return (_refuses_direction(f, 'sqlite')
            and any('--production' in fl for fl in f['flags']))


def _declares_http_target(f):
    """走 HTTP 写生产的脚本（`push_sector_mappings_to_prod.py` 那一族）没有 ORM 会话，
    `database_label` 对它没意义 —— 那它必须自己打一行 `[目标] …` 说清往哪台机器 POST。

    第 44 轮 B-M5 补的第二半：**光"有这个字面量"不算**，它必须出现在某个 print/log 调用的参数里。
    `LABEL = '[目标] 线上生产库'` 是一个从不被打印的死赋值 ⇒ 操作者一个字都看不见，
    守卫却判"声明了目标"，那这条声明就只是写给判据看的。
    """
    return bool(f.get('printed_targets'))


# 读侧的"受管的门"：走这三把之一，目标就已经说清楚了（默认钉镜像 / 显式 --production）
READ_DOORS = {'read_only_connect', 'pin_local_sqlite', 'resolve_read_target'}


def _read_side_guarded(name, f):
    """**只读**脚本也要答"连的是哪个库"（第 41 轮 B-MAJOR-1）。

    以前整份扫描只判"能不能写"，于是三个纯读的分析脚本（`audit_l3_clear_labels.py` 等）
    带着 `create_engine(os.getenv("DATABASE_URL"))` 过了每一道闸 —— 而 `.env` 里那条
    就是生产串。命中形状（`engine_from_env`）后，四选一才算过：
    ① 走统一的门（`read_only_connect` / `pin_local_sqlite` / `resolve_read_target`）；
    ② 自己把 `DATABASE_URL` 钉成 SQLite（`import_export.py` 那一族，`env_written`）；
    ③ "见远程就拒跑 + 显式旗子"或"见 SQLite 就拒跑 + `--production`"（两个方向都要有旗子）；
    ④ 只面向生产的预检工具：引擎级 `postgresql_readonly` + 自报 `[目标]` + 见非 PG 就 `SystemExit`。
    """
    if set(f['called']) & READ_DOORS or f['env_written']:
        return True
    if 'database_label' in f['called']:
        # `database_label()` 干的就是"把连的是哪个库印出来"这件事（`run_migrations.py` 的护栏）。
        # 第 44 轮 A/B 都复核过那行 `[库] …` 报的是**真 engine 的目标**，所以这条该算过。
        return True
    if _refuses_remote_without_a_flag(f) or _refuses_local_without_a_flag(f):
        return True
    consts = f['consts']
    return ('postgresql_readonly' in consts
            and _says_target(f)
            and _refuses_direction(f, 'postgres'))


def _guarded(name, f, schema_ddl=False):
    if name in PRODUCTION_ONLY:
        return _refuses_local_without_a_flag(f)
    if name in PRODUCTION_ENTRY:
        return 'database_label' in f['called']
    # `os.environ["DATABASE_URL"] = ...` 以前的含义是"这脚本自己动过连接串"，
    # 但它**不区分方向**：`run_migrations.py` 那句 `= ALEMBIC_DATABASE_URL` 可以是生产，
    # 照样被判"有守卫"（第 37 轮 B 的 M-3，实测 5 条用例全绿）。
    # 所以发 DDL 的脚本只认三种真守卫：钉镜像、自报库名、或"见远程就拒跑 + 显式旗子"。
    if schema_ddl:
        return ('pin_local_sqlite' in f['called'] or 'database_label' in f['called']
                or _refuses_remote_without_a_flag(f))
    return ('pin_local_sqlite' in f['called'] or f['env_written']
            or 'database_label' in f['called'] or _declares_http_target(f)
            or _refuses_remote_without_a_flag(f))


def test_there_are_write_capable_scripts_left_to_guard():
    """用例不能变成空判：受管脚本的数量必须>0，否则这条扫描已经失效。"""
    n = sum(1 for f in _scripts().values() if _write_capable(f))
    assert n >= 5, '只找到 %d 个受管脚本 ⇒ 先确认这条扫描还有效，再放行' % n
    # 第 38 轮两份复评都点到旧那条 `assert n >= direct`：**`direct` 是 `n` 的子集定义，恒真**
    # （它的条件里已经含 `_write_capable`）⇒ 扫描器退化时两个数一起缩，永远不响。
    # 换成一条**第二个证据源**的判据：正文里带着写开关/口令的脚本，至少得占一样
    # （被认成能改数据、或自己声明了目标）。这条当场抓出过一个真漏网：
    # `push_sector_mappings_to_prod.py` —— 全仓唯一往生产 POST 的写口，两头都不占。
    scripts = _scripts()
    text_switches = {name for name, f in scripts.items()
                     if WRITE_SWITCH.search(f['text']) or '--confirm' in f['text']}
    blind = sorted(name for name in text_switches
                   if not _write_capable(scripts[name])
                   and not _guarded(name, scripts[name],
                                    schema_ddl=_issues_schema_ddl(scripts[name])))
    assert not blind, ('这些脚本正文里就写着写开关，却既不被认成"能改数据"、也不自报目标：%s'
                       % '、'.join(blind))
    assert 'push_sector_mappings_to_prod.py' in text_switches, \
        '文本证据源自己失效了（push 那条 HTTP 写口不见了）⇒ 上面那条交叉核对会变成空判'
    # 第 36 轮 B-MINOR-3：旧判据的前提是"CLI 上有写开关"，于是**没有开关、上来就 commit**
    # 的脚本永远进不了集合。这条把那种形状自己钉住：会 commit 的直连脚本必须全部受管。
    committing = {name for name, f in _scripts().items()
                  if f['direct_db'] and f['called'] & {'commit', 'add', 'delete'}}
    unmanaged = sorted(name for name in committing if not _write_capable(_scripts()[name]))
    assert not unmanaged, '这些脚本直连 ORM 又写库，却没被当成"能改数据"：%s' % '、'.join(unmanaged)
    assert 'seed_sector_mappings.py' in committing, \
        'seed 脚本从集合里掉了 ⇒ 判据又被"有没有写开关"卡回去了（它当初就没有开关）'
    # 第 37 轮 B 的 M-3：**发 DDL 的迁移脚本**以前两条判据都不沾（无写开关、不 commit），
    # 整条扫描对它没印象。这两个名字必须仍在集合里，否则说明"迁移型 DDL"信号又退化成一个字面词。
    ddl = {name for name, f in _scripts().items() if _issues_schema_ddl(f)}
    assert 'run_migrations.py' in ddl, \
        'run_migrations.py 不再被认成"会发 DDL" ⇒ 信号被删或改名了，而它每次 Render 启动都在动生产表结构'
    leaked = sorted(name for name in ddl if not _write_capable(_scripts()[name]))
    assert not leaked, '这些脚本能改表结构，却没被当成"能改数据"：%s' % '、'.join(leaked)


def test_write_capable_scripts_declare_their_database_in_code():
    """判据**不再要求"直连 ORM"**（第 35 轮 B 的残留）：只走 service 层写的脚本一样会跟着
    `.env` 连生产，旧口径把它当不存在。今天加宽后没有新增漏网（16 个带写开关的脚本全都自报了库），
    这条改的是"以后新加的脚本必须自报"这件事本身。
    """
    bad = [name for name, f in _scripts().items()
           if _write_capable(f) and not _guarded(name, f, schema_ddl=_issues_schema_ddl(f))]
    assert not bad, ('这些脚本能改数据，却没在代码里说清算哪个库'
                     '（.env 默认指向生产）：%s' % '、'.join(bad))


def test_renaming_the_database_url_env_is_not_a_guard(tmp_path, monkeypatch):
    """`os.environ["DATABASE_URL"] = 别的库` 不能算"有守卫"，否则方向反了的脚本照样过关。

    起因：`run_migrations.py` 把 `ALEMBIC_DATABASE_URL` 赋进 `DATABASE_URL` —— 那是**指向生产**
    的赋值，旧 `_guarded` 只看"有没有对 DATABASE_URL 赋值"，于是把发 DDL 的脚本判成已声明。

    第 44 轮 B-M4 把这条做实：`env_written` 从此只在**方向能证明**（字面 sqlite /
    `pin_local_sqlite` / 一跳可证的本地变量）时才真，方向不明的赋值落到 `env_written_unknown`。
    所以判据分成两半，缺一条都可能是空判：
    ① 真脚本这一半问"今天它凭什么过关"（自报库名，不是赋值）—— 把自报行抽掉它必须失去守卫；
    ② 合成那一半现造"赋值 + 发 DDL + 什么都不自报"的形状，它必须既是"能改数据"又不是"有守卫"
       （只测①的话，`run_migrations.py` 一旦改得不再赋值，这条就退化成空判）。
    """
    scripts = _scripts()
    f = scripts.get('run_migrations.py')
    assert f is not None, 'run_migrations.py 不在了（它仍被 render.yaml 的 startCommand 每次启动跑一遍）'
    assert _issues_schema_ddl(f), '前提变了：它不再发 DDL ⇒ 这条用例失去意义，改判据而不是留着空判'
    assert f['env_written_unknown'] and not f['env_written'], \
        ('它给 DATABASE_URL 赋的值现在被当成了"方向能证明的 sqlite"（%s / %s）'
         '⇒ 赋值又重新算守卫了' % (f['env_written'], f['env_written_unknown']))
    assert _write_capable(f)
    assert _guarded('run_migrations.py', f, schema_ddl=True), \
        'run_migrations.py 现在靠什么过关？发 DDL 的脚本必须钉镜像/自报库名/见远程拒跑'
    stripped = dict(f)
    stripped['called'] = set(f['called']) - {'database_label'}
    assert not _guarded('run_migrations.py', stripped, schema_ddl=True), \
        '把自报行抽掉它仍算"有守卫" ⇒ 赋 DATABASE_URL（方向不明）还在当守卫用'

    silent = """\"\"\"把环境里另一条连接串顶进 DATABASE_URL，然后发 DDL，什么都不说。\"\"\"
import os
from alembic import command
from alembic.config import Config

os.environ["DATABASE_URL"] = os.environ["MAINT_DB_URL"]
command.upgrade(Config("alembic.ini"), "head")
"""
    self_reported = silent + """
from src.services.verdict_evidence import database_label
print("[库] %s" % database_label(engine))
"""
    scanned = _scan_into(tmp_path, {'_x_env_mover.py': silent,
                                    'ok_env_mover.py': self_reported}, monkeypatch)
    here, there = scanned['_x_env_mover.py'], scanned['ok_env_mover.py']
    assert here['env_written_unknown'] and not here['env_written'], \
        '合成样品自己就没走到"方向不明"那一支 ⇒ 上面那条真脚本判据也站不住'
    assert _write_capable(here), '赋值 + 发 DDL 的脚本不被认成"能改数据" ⇒ 它就是下一条无守卫的 run_migrations'
    assert not _guarded('_x_env_mover.py', here, schema_ddl=True), \
        '它只做过一件事：把一条来路不明的连接串赋进 DATABASE_URL —— 那不算守卫'
    assert _guarded('ok_env_mover.py', there, schema_ddl=True), \
        '对照组（真自报了库名）被判没守卫 ⇒ 上一条的"没守卫"是判据坏了，不是脚本坏了'


def test_the_two_new_triggers_can_actually_fire():
    """HTTP 写与 `os.system` 起 CLI 这两个触发器**今天 0 实例**（我把 `scripts/*.py` 扫了一遍：
    唯一的 HTTP 写口 `push_sector_mappings_to_prod.py` 走的是自己的 `request()` 助手）。
    没有这条合成判据，那两个集合就是两段"写在代码里却永远不会响"的死逻辑。"""
    def facts(**kw):
        base = {'text': '', 'called': set(), 'flags': set(), 'raised': set(), 'consts': set(),
                'env_written': False, 'direct_db': False, 'alembic': False}
        base.update(kw)
        return base

    assert _write_capable(facts(called={'post'})), 'HTTP 写不算能改数据 ⇒ 下一条 POST 脚本又隐身'
    assert _write_capable(facts(called={'put'})), '同上（put）'
    assert not _write_capable(facts(called={'get'})), 'GET 也算写 ⇒ 判据过宽会淹掉真信号'
    assert _issues_schema_ddl(facts(alembic=False, called={'system'}, consts={'alembic upgrade head'})), \
        '`os.system("alembic upgrade head")` 不被认成发 DDL（上一版只认 subprocess 那一族）'


def _scan_into(tmp_path, files, monkeypatch):
    """判据不许依赖"我机器上恰好有 7 个未入库的 `_tmp_*.py`"。

    第 40 轮 A 席 M2：上一版那条 `any(n.startswith('_tmp_'))` 在**干净克隆**上必红
    ——tracked 的 `scripts/*.py` 只有 45 个，磁盘上 52 个，差额全在 `.gitignore` 里。
    要验形状就现造文件。
    """
    import sys as _sys
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding='utf-8')
    monkeypatch.setattr(_sys.modules[__name__], 'SCRIPTS', tmp_path)
    return _scripts()


_WRITER = '''"""会写库、什么都没声明的脚本"""
import os
from src.models.database import SessionLocal
db = SessionLocal()
db.add(1)
db.commit()
'''


def test_underscore_prefixed_scripts_are_still_scanned(tmp_path, monkeypatch):
    """`_` 前缀不再是整族豁免（第 39 轮 B：新写一个 `_x.py` 的生产写脚本可以永远没人管）。"""
    scanned = _scan_into(tmp_path, {'_x_writer.py': _WRITER, 'ok_writer.py': _WRITER}, monkeypatch)
    assert {'_x_writer.py', 'ok_writer.py'} <= set(scanned), sorted(scanned)
    for name in ('_x_writer.py', 'ok_writer.py'):
        f = scanned[name]
        assert _write_capable(f), '%s 直连 ORM 又 commit，却不被认成能改数据' % name
        assert not _guarded(name, f), '前提：这两个桩文件都没自报库名 ⇒ 下面那条判据必须抓到它们'
    bad = sorted(n for n, f in scanned.items()
                 if _write_capable(f) and not _guarded(n, f, schema_ddl=_issues_schema_ddl(f)))
    assert bad == ['_x_writer.py', 'ok_writer.py'],         '扫描或判据失效（这两个都该被抓到，实际：%s）' % bad


def test_a_syntax_broken_script_fails_the_gate_instead_of_the_scanner(tmp_path, monkeypatch):
    """解析不了 = **无法证明它不能写** ⇒ fail-closed 按能写处理。

    以前只有 docstring 里一句"标成未受管让下面变红"，实际兜底 dict 缺 `consts`/`raised`
    两个键 ⇒ 扫描器先 KeyError；补上键之后仍是静默放过（它没有写开关、也没 commit 调用）。
    第 39 轮 A-MINOR-7 与第 40 轮 A-MAJOR-3 是同一条账的两半。
    """
    import pytest as _pt
    scanned = _scan_into(tmp_path, {'broken_one.py': 'def (:{', 'ok_writer.py': _WRITER},
                         monkeypatch)
    assert scanned['broken_one.py'].get('broken') is True
    assert _write_capable(scanned['broken_one.py']), '解析失败被放过了 ⇒ fail-closed 没生效'
    with _pt.raises(AssertionError):
        test_write_capable_scripts_declare_their_database_in_code()


def test_the_http_write_trigger_catches_the_shape_this_repo_actually_uses(tmp_path, monkeypatch):
    """仓库里的生产写口用的是 `urlopen(Request(..., method='POST'))`，不是 `session.post(...)`。

    第 40 轮 A-M5：触发器只认 `post/put/patch` 时，它精确覆盖了"今天没有实例"的那种形状，
    而真实那种一旦不带 `--confirm` 字样照样隐身。这条用**真写法**验，不用假写法。
    """
    body = ("""import urllib.request
"""
            """def push(url, payload):
"""
            """    req = urllib.request.Request(url, data=payload, method='POST')
"""
            """    return urllib.request.urlopen(req)
""")
    scanned = _scan_into(tmp_path, {'http_writer.py': body}, monkeypatch)
    f = scanned['http_writer.py']
    assert _write_capable(f), 'urllib 的 POST 不被认成写操作 ⇒ 下一份不带口令的 HTTP 写口仍隐身'
    assert not _guarded('http_writer.py', f)
    only_get = body.replace("method='POST'", "method='GET'")
    other = tmp_path / 'get_only'
    other.mkdir()
    s2 = _scan_into(other, {'reader.py': only_get}, monkeypatch)
    assert not _write_capable(s2['reader.py']), 'GET 也算写 ⇒ 判据过宽会淹掉真信号'


def test_every_way_of_changing_the_schema_counts_as_ddl():
    """`downgrade` 与 `stamp` 也算"能改表结构"，起裸 `alembic` CLI 也算（第 38 轮 B 的附带项）。

    上一版我只认 `command.upgrade`：而 `20260722_0002` 的 downgrade 是
    `drop_table("prediction_change_logs")`（审计台账本体），`stamp` 会让"库里有什么"和
    "记录说有什么"分家 —— 三个都能改结构，旧判据只盯其中一个。
    """
    def facts(called=(), consts=(), alembic=True, direct_db=True):
        return {'text': '', 'called': set(called), 'flags': set(), 'raised': set(),
                'consts': set(consts), 'env_written': False, 'direct_db': direct_db,
                'alembic': alembic}

    for verb in ('upgrade', 'downgrade', 'stamp'):
        assert _issues_schema_ddl(facts(called={'command', verb})), \
            '%s 不被算成改结构 ⇒ 动词集合又缩回只剩 upgrade 了' % verb
    assert _issues_schema_ddl(facts(alembic=False, called={'run'}, consts={'alembic', 'upgrade'})), \
        '拿 subprocess 起裸 alembic 的脚本不算能改结构（env.py 那条方向翻转就是被它绕过的）'
    assert not _issues_schema_ddl(facts(called={'current', 'heads'})), \
        '只读命令也算 DDL ⇒ 判据过宽会把信号淹掉'


def test_a_production_flag_alone_is_not_a_guard():
    """声明了 `--against-production` 却没真 pin、也没"见远程就拒跑"= 有旗子没护栏（H-MAJOR-1）。"""
    bad = [name for name, f in _scripts().items()
           if any(PROD_FLAG.search(fl) for fl in f['flags'])
           and 'pin_local_sqlite' not in f['called']
           and not f['env_written'] and not _refuses_remote_without_a_flag(f)]
    assert bad == [], '只有 --against-production 字样、代码里没有真护栏：%s' % '、'.join(bad)


def test_hard_delete_scripts_pin_the_mirror_by_default():
    """物理删除的脚本必须默认钉镜像；只"印出库名"不够（H-MAJOR-2：删除发生在 service 里时
    上一版扫 `.delete(` 完全漏掉了 `run_three_bucket_retention.py`）。"""
    bad = []
    for name, f in _scripts().items():
        if name in PRODUCTION_ENTRY:      # Render Cron 入口本就跑生产，护栏由上一条管
            continue
        delegates = bool(HARD_DELETE.search(f['text']))
        direct = '.delete(' in f['text']
        if not (delegates or direct):
            continue
        if name in PRODUCTION_ONLY and _refuses_local_without_a_flag(f):
            continue                      # 只清线上误写的修复脚本：见 SQLite 就拒跑，反向护栏
        if not ('pin_local_sqlite' in f['called'] or f['env_written']
                or _refuses_remote_without_a_flag(f)):
            bad.append(name + ('（删除发生在 service 里，扫 .delete( 扫不到）'
                              if not direct else ''))
    assert not bad, '这些会物理删数据的脚本默认连的不是本地镜像：%s' % '、'.join(bad)


def test_the_production_only_allow_list_is_not_a_backdoor():
    """`PRODUCTION_ONLY` 里的名字必须**存在**且真的带反向护栏；少一个条件就红。

    为什么单独一条：往任何"白名单"里加名字都是给闸门打洞的最短路径（第 24 轮起反复出现）。
    这里把两件事钉死：① 文件真的在（拼错的名字会静默放行同名脚本）；
    ② 它自己必须过 `_refuses_local_without_a_flag` —— 把 `_reject_local` 的 `SystemExit`
    或 `--production` 旗子删掉，这条立刻变红。
    """
    scripts = _scripts()
    for name in sorted(PRODUCTION_ONLY):
        assert name in scripts, 'PRODUCTION_ONLY 里的 %s 不存在（拼错的名字=闸门静默放行）' % name
        assert _refuses_local_without_a_flag(scripts[name]), \
            '%s 挂着"只面向生产"的名字却没有反向护栏（见 SQLite 就拒跑 + 显式 --production）' % name
    # `PRODUCTION_ENTRY` 同样是名单：它的护栏是"自报库名"，所以名字必须存在、
    # 代码里必须**真调用** database_label（写进 docstring 不算，_facts 只收 AST）。
    for name in sorted(PRODUCTION_ENTRY):
        assert name in scripts, 'PRODUCTION_ENTRY 里的 %s 不存在（拼错的名字=闸门静默放行）' % name
        assert 'database_label' in scripts[name]['called'], \
            '%s 挂着"生产入口"的名字却没自报库名' % name


def _touches_a_database(f, graph=None):
    """读侧那道闸的**触发条件**：只要"碰到过一个活的数据库连接"就得问连哪儿。

    第 43 轮 B-MAJOR-2：上一版触发只看 `engine_from_env`（自建 engine 那一族），
    于是"函数体里 `from src.models.database import SessionLocal` + `db.query(...)`、
    一句不写"的脚本三道判据一条都不响 —— 而 `.env` 指生产 ⇒ 它默认就在读线上库。
    这一族在本仓不是假想：`direct_db` 为真的脚本有 30 个，其中 9 个既不受写侧管
    也不被旧读侧触发命中（今天它们各自走了门或被 `env_written` 覆盖，所以是**下一个脚本**的洞）。

    第 44 轮 B-MAJOR-3 补的是**一次间接**：脚本自己一句 `src.models` 都不写，
    只 `from src.services.l1_weighting import …`，而那个 service 的**顶层**就 import 了 ORM ⇒
    导入它的那一刻全局 engine 已经按 `.env` 建好了。`direct_db` 看不见这一层，
    所以要顺着 import 图走一遍（`graph` 为空/没给 ⇒ 这一路不亮，宁可少判不误判）。
    """
    if f['engine_from_env'] or f['direct_db']:
        return True
    if f.get('capabilities') and (f['capabilities'] & {'opaque_exec'}):
        return True             # `exec("…SessionLocal().query…")`：语句写在字符串里，也是碰库
    if not graph:
        return False
    return any(_reaches_orm(module, graph) for module in f['src_imports'])


def test_read_side_scripts_also_say_which_database_they_read():
    """只读脚本也要答"连的是哪个库"（第 41 轮 B-MAJOR-1：读侧不是攻击面 = 这整族的根因）。

    三个 L3/L1 分析脚本以前都写着 `create_engine(os.getenv("DATABASE_URL"))` —— 本地 `.env`
    里那条就是生产 Supabase，于是"跑一下估算"默认读线上，还把结果连同一个只写着
    `"DATABASE_URL"` 的标签落进 `docs/`。守卫扫描只看"能不能写"，所以它们一路绿灯。

    第 44 轮 B-MAJOR-3 把触发条件补到"隔一层也认得"：判据带着 `src` 的 import 图跑，
    图本身由 `_src_import_graph()` 从**真文件**读出来（现造一棵 src 树的对照在
    `test_touching_the_database_through_one_import_of_the_service_layer_counts`）。
    """
    graph = _src_import_graph()
    naked = [name for name, f in _scripts().items()
             if _touches_a_database(f, graph) and not _read_side_guarded(name, f)
             and name not in PRODUCTION_ENTRY]
    assert naked == [], '这些脚本碰得到一个活的数据库连接，却没走任何受管的门：%s' % '、'.join(naked)


def test_the_read_side_trigger_fires_on_the_shape_that_broke_and_not_on_a_fixed_path(tmp_path,
                                                                                     monkeypatch):
    """触发器本身要能红：现造一个"照旧写法"的脚本，它必须命中且被判裸奔。

    为什么不只测仓库现状：那三个脚本已经改好了 ⇒ `engine_from_env` 若永远False，
    上一条就退化成空判（第 40 轮 A 席 M2 同一条教训：判据不许依赖"我机器上恰好有什么"）。
    """
    scripts = _scan_into(tmp_path, {
        # 肇事形状：读了 DATABASE_URL，又自己 create_engine
        '_x_env_engine.py': 'import os\nfrom sqlalchemy import create_engine\n'
                            'e = create_engine(os.getenv("DATABASE_URL"))\n',
        # 已改好的形状：走统一的门
        '_x_through_the_door.py': 'import _db_guard\n'
                                  'e, s, label = _db_guard.read_only_connect()\n',
        # 目标写死在代码里（副本回放）：不算命中，也不该被这条管
        '_x_fixed_path.py': 'from sqlalchemy import create_engine\n'
                            'e = create_engine("sqlite:///data/copy.db")\n',
        # 一次间接：文件里根本没有 `DATABASE_URL` 这个字面量，值却仍来自配置（第 42 轮 B-MAJOR-3）
        '_x_via_settings.py': 'from sqlalchemy import create_engine\n'
                              'from src.core.config import settings\n'
                              'e = create_engine(settings.database_url)\n',
        # 一个别名就隐身（第 43 轮 B-MAJOR-1）：`as ce` 换掉函数名，语义一个字没变
        '_x_aliased_engine.py': 'import os\nfrom sqlalchemy import create_engine as ce\n'
                                'c = ce(os.environ["DATABASE_URL"])\n',
        # 纯读、不写、不自建 engine，只在函数体里拿 ORM 会话（第 43 轮 B-MAJOR-2 的主形状）
        '_x_orm_read_in_a_body.py': 'def main():\n'
                                    '    from src.models.database import SessionLocal\n'
                                    '    db = SessionLocal()\n'
                                    '    try:\n        return db.query(1).all()\n'
                                    '    finally:\n        db.close()\n',
        # 同一个形状的对照组：走门之后就该放行
        '_x_orm_read_through_the_door.py': 'import _db_guard\n'
                                           'def main():\n'
                                           '    e, db, label = _db_guard.read_only_connect()\n'
                                           '    return db.query(1).all()\n',
    }, monkeypatch)
    hits = {n: f['engine_from_env'] for n, f in scripts.items()}
    assert hits['_x_env_engine.py'] is True, '环境变量 + 自建 engine 没被认出来 ⇒ 触发器是死的'
    assert hits['_x_fixed_path.py'] is False, '写死路径的副本 engine 被误伤 ⇒ 判据过宽'
    assert hits['_x_via_settings.py'] is True, \
        '把 `os.getenv("DATABASE_URL")` 换成 `settings.database_url` 就隐身 ⇒ 触发器仍可绕过'
    assert hits['_x_aliased_engine.py'] is True, \
        '`from sqlalchemy import create_engine as ce` 一个别名就隐身 ⇒ 触发器认的是名字不是意思'
    touched = {n: _touches_a_database(f) for n, f in scripts.items()}
    assert touched['_x_orm_read_in_a_body.py'] is True, \
        '函数体里 `from src.models.database import SessionLocal` 的纯读脚本没被碰库触发 ⇒ B-MAJOR-2 没修'
    assert not _read_side_guarded('_x_env_engine.py', scripts['_x_env_engine.py']), \
        '旧写法被判"已经有守卫"'
    assert not _read_side_guarded('_x_orm_read_in_a_body.py', scripts['_x_orm_read_in_a_body.py']), \
        '只读的 ORM 会话没走门却被判"已经有守卫"'
    assert _read_side_guarded('_x_through_the_door.py', scripts['_x_through_the_door.py'])
    assert _read_side_guarded('_x_orm_read_through_the_door.py',
                              scripts['_x_orm_read_through_the_door.py'])


def test_touching_the_database_through_one_service_import_counts(tmp_path, monkeypatch):
    """脚本自己一句 `src.models` 都不写，只 import 一个"顶层就 import ORM"的 service ⇒ 也算碰库。

    第 44 轮 B-MAJOR-3 的第二半：`direct_db` 只看**这个文件**里有没有 `src.models`，
    于是 `from src.services.l1_weighting import …`（它第 16 行就 `from src.models.database import
    Prediction`）在扫描器面前是"干净的"，而那一句 import 已经把全局 engine 按 `.env` 建好了。

    这里现造一棵临时 src 让图有真形状可读（图由 `_src_import_graph()` 从文件读出来，
    不是我抄一份邻接表）：一支样品 import 会拉起 ORM 的 service（必须命中），
    一支 import 纯工具模块（不许命中 —— 否则任何 `import src.utils.x` 都被迫去自报库名）。
    """
    root = tmp_path
    # 先照一眼**真仓库**的图（下面会把 SCRIPTS 指到临时目录，那时就读不到本仓了）
    real = _src_import_graph()
    (root / 'scripts').mkdir()
    # 临时 src 树**照仓库的写法**造：包 `__init__.py` 用相对 import。
    # 上一版这里造的是绝对 import（`from src.models.database import engine`）+ 空 `__init__.py`
    # —— 恰好避开了仓库真正踩坑的那种形状（第 45 轮 A-M5：`src/fund/__init__.py:4`、
    # `src/services/__init__.py:6` 全是 `from .x import …`，而图把 `node.level` 丢了）。
    tree_files = {
        'src/__init__.py': '',
        'src/models/__init__.py': 'from .database import engine\n',
        'src/models/database.py': 'import os\nfrom sqlalchemy import create_engine\n'
                                  'engine = create_engine(os.getenv("DATABASE_URL"))\n',
        'src/services/__init__.py': 'from . import l1\n',
        'src/services/l1.py': 'from ..models.database import engine\n',
        'src/utils/__init__.py': '',
        'src/utils/plain.py': 'import json\n',
    }
    for rel, body in tree_files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding='utf-8')
    for name, body in {'_x_via_service.py': 'from src.services import l1\n',
                       '_x_via_tool.py': 'from src.utils.plain import dumps\n',
                       '_x_via_package.py': 'import src.models\n'}.items():
        (root / 'scripts' / name).write_text(body, encoding='utf-8')
    monkeypatch.setattr(sys.modules[__name__], 'SCRIPTS', root / 'scripts')
    graph = _src_import_graph()
    assert graph, '临时 src 建好了却读不到 import 图 ⇒ 下面全是空判'
    scripts = _scripts()
    assert _touches_a_database(scripts['_x_via_service.py'], graph) is True, \
        '隔一层 import 把 ORM 拉起来不算碰库 ⇒ B-MAJOR-3 只修了一半'
    assert _touches_a_database(scripts['_x_via_package.py'], graph) is True, \
        '`import src.models`（一个包）看不见它 `__init__.py` 里的相对 import ⇒ 相对写法仍会漏'
    assert _touches_a_database(scripts['_x_via_tool.py'], graph) is False, \
        '任何 `import src.utils.*` 都被算成碰库 ⇒ 判据过宽，真信号会被淹掉'
    # 没给图 ⇒ 这一路不亮（别的用例拿不到 src 树时，不能让脚本凭空"受管"）
    assert _touches_a_database(scripts['_x_via_service.py']) is False

    # 真仓库这一边：`import 任何 src.*` 都会执行 `src/__init__.py`
    # （它写着 `from src.fund import fund_api, fund_data_manager`）⇒ 一路拉起 ORM、
    # 按 `.env` 建出全局 engine。A 席实测这三条以前全判 False。
    for mod in ('src.models', 'src.services', 'src.fund', 'src.utils.mutation_lock'):
        assert _reaches_orm(mod, real), \
            '`import %s` 说它不会拉起 ORM ⇒ 相对 import / 父包 `__init__` 又被丢了' % mod
    # 反向对照（过宽检查）：**不是 src 包**的名字不许亮 —— 判据只该管本仓自己那棵树
    assert not _reaches_orm('sqlalchemy.orm', real), \
        '第三方模块也被判"会拉起本仓 ORM" ⇒ 这条判据过宽'
    assert not _reaches_orm('src', _empty_graph := {'src': set()}), \
        '一棵什么都没有的图也说"会拉起 ORM" ⇒ 这条判据恒真'


def _fallback_keys():
    """`_scripts()` 里那个"解析失败兜底 dict"的**顶层**键集合（AST 里抠，不手抄）。

    第 45 轮：上一版用正则扫文本片段，于是 `refusals` 里面那层 dict 的 `words`/`line`/`scope`
    也被当成兜底 dict 的键 —— 判据"两边漂了就红"当场变成"我自己造了一个漂"。
    改成认节点：找那个**含 `capabilities` 键**的 dict 字面量，只取它的顶层键。
    """
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(_scripts)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and any(
                isinstance(k, ast.Constant) and k.value == 'capabilities' for k in node.keys):
            return {k.value for k in node.keys if isinstance(k, ast.Constant)}
    return set()


def test_the_fallback_dict_cannot_lag_behind_the_facts_it_stands_in_for():
    """解析失败时那份兜底 dict 必须与 `_facts` 返回的键**完全一致**。

    为什么单独立一条（第 43 轮 A/B 都提到"两处靠注释维持一致"）：兜底 dict 少一个键，
    下一个读 `f['新键']` 的判据就 KeyError 崩掉整个扫描器；多一个键则没人发现它已经没人读了。
    注释里写"必须与 `_facts` 完全一致"没有用 —— 这句承诺现在就由这条用例代持。
    """
    probe = SCRIPTS / '_db_guard.py'          # 任何一个真脚本都行：只为拿一次键集合
    real = set(_facts(probe))
    fallback = _fallback_keys()
    assert real - fallback == set(), (
        '兜底 dict 缺了这些键 ⇒ 解析失败的文件会在判据里 KeyError：%s' % sorted(real - fallback))
    assert fallback - real == {'broken'}, (
        '兜底 dict 多出这些键（`broken` 是它独有的标记，别的一律说明两边漂了）：%s'
        % sorted(fallback - real))


def test_prose_cannot_buy_a_guard_signal(tmp_path, monkeypatch):
    """**说明文与入口样板句买不到守卫**（第 43 轮 A-MAJOR-1，我自己复现过才修的）。

    上一版 `_facts` 用 `ast.walk` 收所有字符串常量 ⇒ docstring 也是常量，而每个脚本末尾都有
    `raise SystemExit(main())`。于是"在 docstring 里写 postgres / `[目标]` / `postgresql_readonly`
    + 保留那句样板"就能让三个识别器一起点头 —— 而 `_refuses_remote_without_a_flag` 自己的
    docstring 写着"写在注释或 docstring 里不算"，那句话当场是假的。
    这一条把 A 席那三个样品钉成用例：每个都配一个"把说明文换成护栏代码"的对照，
    否则它可能只是恒红或恒绿。
    """
    scripts = _scan_into(tmp_path, {
        # ① 发 DDL 的脚本：`postgres` 只在 docstring 里，"拒跑"只是入口样板句
        '_x_prose_ddl.py': '"""同步工具。\n\n本脚本见 postgres 目标就拒跑，要动线上加 --against-production。\n"""\n'
                           'import argparse\nfrom alembic import command\n'
                           'p = argparse.ArgumentParser()\np.add_argument("--against-production")\n'
                           'a = p.parse_args([])\ncommand.upgrade(a, "head")\n'
                           'raise SystemExit(main())\n',
        # ① 的对照组：同一件事，但护栏是真代码（条件分支里印 [abort] 并停下来）
        '_x_real_ddl_guard.py': 'import argparse\nfrom alembic import command\n'
                                'p = argparse.ArgumentParser()\np.add_argument("--against-production")\n'
                                'a = p.parse_args([])\nurl = "x"\n'
                                'if url.startswith("postgres") and not a.against_production:\n'
                                "    print('[abort] 目标是远程库，加 --against-production 再来')\n"
                                '    raise SystemExit(4)\n'
                                'command.upgrade(a, "head")\n',
        # ② HTTP 写：`[目标]` 只在 docstring 里
        '_x_prose_http.py': '"""推送。\n\n[目标] 线上生产库\n"""\n'
                            'import urllib.request\nurllib.request.urlopen('
                            'urllib.request.Request("http://x", method="POST"))\n',
        # ② 的对照组：`[目标]` 是一句真 print
        '_x_real_http_target.py': 'import urllib.request\n'
                                  "print('[目标] http://x —— 经 HTTP 写线上')\n"
                                  'urllib.request.urlopen(urllib.request.Request("http://x", method="POST"))\n',
        # ③ 读侧第④档：三个信号全在 docstring / 样板句里
        '_x_prose_readonly.py': '"""只读预检。\n\npostgresql_readonly + [target] 线上生产库\n"""\n'
                                'from sqlalchemy import create_engine\n'
                                'create_engine("postgresql://u@h/db")\n'
                                'raise SystemExit(main())\n',
    }, monkeypatch)
    prose = {n: (_write_capable(f), _refuses_remote_without_a_flag(f),
                 _declares_http_target(f), _read_side_guarded(n, f)) for n, f in scripts.items()}
    assert prose['_x_prose_ddl.py'][1] is False, 'docstring 里的 postgres + 入口样板句被判"见远程就拒跑"'
    assert prose['_x_prose_http.py'][2] is False, 'docstring 里的 `[目标]` 被判"声明了 HTTP 写目标"'
    assert prose['_x_prose_readonly.py'][3] is False, '说明文凑齐三个词就被判"读过侧的门"'
    assert prose['_x_real_ddl_guard.py'][1] is True, \
        '真护栏（条件分支里 print [abort] + 停下来）反被判没守卫 ⇒ 这条判据在误伤'
    assert prose['_x_real_http_target.py'][2] is True, '真 print 一行 `[目标] …` 必须算声明'


_GUARD_HEADER = '''"""样品脚本（判据用）。"""
import argparse
import os
from alembic import command
from alembic.config import Config

ap = argparse.ArgumentParser()
ap.add_argument("--against-production", action="store_true")
a = ap.parse_args()
url = os.environ.get("DATABASE_URL", "")
'''

# 每条样品：(文件名, 源码, 期望"有守卫"吗, 这一条在钉什么)
_BYPASS_SAMPLES = [
    ('_x_ok_real_guard.py', _GUARD_HEADER + '''
if url.startswith("postgres") and not a.against_production:
    print("[abort] 目标是远程库，加 --against-production 再来")
    raise SystemExit(4)
command.upgrade(Config("alembic.ini"), "head")
''', True, '真护栏：条件看方向、印了 `[abort]`、停下来，而且**排在 DDL 前面**'),
    ('_x_dead_conjunct.py', _GUARD_HEADER + '''
if url.startswith("postgres") and not a.against_production and False:
    print("[abort] 目标是远程库，加 --against-production 再来")
    raise SystemExit(4)
command.upgrade(Config("alembic.ini"), "head")
''', False, '`and False` 让条件永不成立：话说满了，一步都没挡（第 45 轮 B-M1①）'),
    # ↓ 第 46 轮 A-M3：上一版只认 `False` / `and False` 两种拼写，这三条当时都判"有守卫"
    ('_x_dead_eq.py', _GUARD_HEADER + '''
if url.startswith("postgres") and not a.against_production and 1 == 0:
    print("[abort] 目标是远程库，加 --against-production 再来")
    raise SystemExit(4)
command.upgrade(Config("alembic.ini"), "head")
''', False, '`and 1 == 0` 与 `and False` 是同一件事：恒假。死路要**求值**判，不能按拼写列'),
    ('_x_dead_not_true.py', _GUARD_HEADER + '''
if not True and url.startswith("postgres"):
    print("[abort] 目标是远程库，加 --against-production 再来")
    raise SystemExit(4)
command.upgrade(Config("alembic.ini"), "head")
''', False, '`if not True and …` 同样永不成立（取反的常量也要折叠）'),
    ('_x_dead_empty_tuple.py', _GUARD_HEADER + '''
if () and url.startswith("postgres"):
    print("[abort] 目标是远程库，加 --against-production 再来")
    raise SystemExit(4)
command.upgrade(Config("alembic.ini"), "head")
''', False, '空容器作合取首项 ⇒ 整条恒假；这一族以前只认 `False` 字样'),
    # ↓ 第 46 轮 A-M4：顺序与可达都要走到**调用图**上（以前只比"同一最内层函数里的行号"）
    ('_x_ddl_via_helper_first.py', _GUARD_HEADER + '''
def apply_it():
    command.upgrade(Config("alembic.ini"), "head")


apply_it()
if url.startswith("postgres") and not a.against_production:
    print("[abort] 目标是远程库，加 --against-production 再来")
    raise SystemExit(4)
''', False, 'DDL 抽进 helper、主流程先调用它再判方向 ⇒ 换个函数名就绕过"谁在前面"这条判据'),
    ('_x_guard_only_from_dead_helper.py', _GUARD_HEADER + '''
def check(u):
    if u.startswith("postgres") and not a.against_production:
        print("[abort] 目标是远程库，加 --against-production 再来")
        raise SystemExit(4)


def nobody_calls_me():
    check(url)


command.upgrade(Config("alembic.ini"), "head")
''', False, '护栏待在一个**只被死函数调用**的函数里 ⇒ 从模块顶层走不到它（以前"名字被写过"就算可达）'),
    ('_x_helper_guard_called_first.py', _GUARD_HEADER + '''
def check(u):
    if u.startswith("postgres") and not a.against_production:
        print("[abort] 目标是远程库，加 --against-production 再来")
        raise SystemExit(4)


check(url)
command.upgrade(Config("alembic.ini"), "head")
''', True, '控制：抽成 helper 的**真**护栏（调用点排在 DDL 前面）必须仍然算守卫 —— '
            '否则上面两条只是把闸门改成"凡在函数里都不算"'),
    ('_x_ddl_first.py', _GUARD_HEADER + '''
command.upgrade(Config("alembic.ini"), "head")
if url.startswith("postgres") and not a.against_production:
    print("[abort] 目标是远程库，加 --against-production 再来")
    raise SystemExit(4)
''', False, 'DDL 已经执行完了才"拒跑"：护栏排在危险动作后面（第 45 轮 B-M1③）'),
    ('_x_dead_stopper.py', _GUARD_HEADER + '''
if url.startswith("postgres") and not a.against_production:
    def _never_called():
        return 4
    print("[abort] 目标是远程库，加 --against-production 再来")
command.upgrade(Config("alembic.ini"), "head")
''', False, '分支里那个 `return 4` 属于一个从不调用的函数 ⇒ 分支根本没停（B-M1④）'),
    ('_x_except_only.py', _GUARD_HEADER + '''
if url.startswith("postgres") and not a.against_production:
    try:
        print("[准备] 先看一眼能不能连")
    except Exception:
        print("[abort] 目标是远程库，加 --against-production 再来")
        raise SystemExit(4)
command.upgrade(Config("alembic.ini"), "head")
''', False, '`[abort]` 只挂在 except 里：连上了就一句都不说，照样发 DDL（B-M1⑤）'),
    ('_x_uncalled_guard.py', _GUARD_HEADER + '''
def _check(url):
    if url.startswith("postgres") and not a.against_production:
        print("[abort] 目标是远程库，加 --against-production 再来")
        raise SystemExit(4)


command.upgrade(Config("alembic.ini"), "head")
''', False, '护栏待在一个**没人调用**的函数里（第 45 轮 A-M-3）'),
]


def test_a_guard_must_be_reachable_and_stand_in_front_of_the_danger(tmp_path, monkeypatch):
    """"同一条分支"做到了，这一轮要的是**这条分支真的挡在危险动作前面**（A-M3 / B-M1）。

    六条样品只差"护栏长什么样"，其中五条是两份评审各自复现出来的绕法：
    `and False`、先执行再拒跑、`def` 里那句永不生效的 `return 4`、只挂在 `except` 的 `[abort]`、
    写在没人调用的函数里的护栏。每一条都必须被判"**没有**守卫"，而第一条（真护栏）必须判"有"
    —— 没有这条对照，"全判没有"也能让前五条一起绿。
    """
    scripts = _scan_into(tmp_path, {n: src for n, src, _want, _why in _BYPASS_SAMPLES}, monkeypatch)
    for name, src, want, why in _BYPASS_SAMPLES:
        f = scripts[name]
        assert _issues_schema_ddl(f) and _write_capable(f), \
            '%s 没被判成"能改表结构"⇒ 它压根进不了受管集合，下面那条断言是空判' % name
        got = _guarded(name, f, schema_ddl=True)
        assert got is want, '%s：期望 guarded=%s，实际 %s（判据在钉：%s）' % (name, want, got, why)


def test_the_subprocess_borrow_list_is_not_a_stale_gate(tmp_path, monkeypatch):
    """注释里承诺过的"名单与真扫描对齐"，今天**真的有**这条用例（第 45 轮 B-M-5）。

    上一轮我在 `KNOWN_WRITERS` 上面写着"名单由 `test_the_subprocess_borrow_list_covers_
    every_known_writer` 与真扫描结果对齐"，而那条判据从来没被写出来 —— 正是这个仓库反复
    犯的那件事：一句没有实现体的承诺（本轮 grep 全仓，只有那条注释命中）。
    两条账：① 名单里不许留"今天已经不算能写"的条目（过期名单会让借道看起来比实际宽）；
    ② **不登记也必须现形**：命令指着 `scripts/某个没登记的脚本.py` 且带写开关 ⇒ 照样算借道。
    第二条才是关键 —— 否则"记得改名单"是这道闸的前提，而前提从来不会被记得。
    """
    scripts = _scripts()
    writers = {n for n, f in scripts.items() if _write_capable(f)}
    listed = {n + '.py' for n in re.findall(r'[a-z_]{4,}', KNOWN_WRITERS)}
    stale = sorted(n for n in listed if n not in writers)
    assert not stale, '借道名单里这些脚本今天并不被认成"能写"（删掉，或写明为什么留着）：%s' % stale

    made = _scan_into(tmp_path, {
        'caller_unlisted.py': 'import subprocess, sys\n'
                              'subprocess.run([sys.executable, "scripts/'
                              'brand_new_writer.py", "--apply", "--confirm", "X"])\n',
        'caller_read_only.py': 'import subprocess, sys\n'
                               'subprocess.run([sys.executable, "scripts/'
                               'brand_new_report.py"])\n',
    }, monkeypatch)
    assert _issues_schema_ddl(made['caller_unlisted.py']), \
        '借道一个**没登记在名单上**的写脚本仍然隐身 ⇒ 名单还是唯一入口，忘了改就是放行'
    assert not _issues_schema_ddl(made['caller_read_only.py']), \
        '只要 subprocess 跑一个 scripts/ 里的脚本就算借道写 ⇒ 判据过宽（不带写开关的只读调用很常见）'


def test_the_dynamic_import_and_exec_are_not_invisible(tmp_path, monkeypatch):
    """`importlib.import_module("src.models.database")` 与 `exec("…commit()")` 都要现形
    （第 45 轮 B-m4：上一版两条都不响 —— 换一种写 import 的说法，判据就当没看见）。"""
    made = _scan_into(tmp_path, {
        '_x_dyn_import.py': 'import importlib\n'
                            'db = importlib.import_module("src.models.database")\n'
                            'def main():\n    return db.SessionLocal().query(1).all()\n',
        '_x_exec_write.py': 'def main():\n'
                            '    exec("db.commit()")\n',
        '_x_harmless_exec.py': 'def main():\n    exec("print(1)")\n',
    }, monkeypatch)
    assert _touches_a_database(made['_x_dyn_import.py']), \
        '动态 import 把 ORM 拉起来了却没被认成"碰库" ⇒ 读侧三道判据一条都不响'
    assert 'src.models.database' in made['_x_dyn_import.py']['src_imports'], \
        '动态 import 的模块名没进 src_imports ⇒ import 图那条判据也看不见它'
    assert _write_capable(made['_x_exec_write.py']), \
        '`exec("db.commit()")` 不被当成能改数据 ⇒ 字符串里的写动作免检'
    assert not _write_capable(made['_x_harmless_exec.py']), \
        '任何 exec/eval 都算能写 ⇒ 判据过宽（`exec("print(1)")` 这种也要被抓）'


def test_a_direction_comes_from_a_value_not_from_a_name_or_a_sentence(tmp_path, monkeypatch):
    """"方向能证明"不看变量叫什么、也不看函数怎么解释自己（第 45 轮 A-M1 / A-M2 / A-M4）。

    三条失败的样品各复现一种"用字样冒充证据"：
    ① 变量名里带 `MIRROR` —— 那台完全可以是生产；
    ② helper 的 **docstring** 里写着 sqlite，而它 `return` 的是另一个环境变量
      （第 43 轮 A-MAJOR-1 刚把 docstring 从常量里剔掉，我在新加的 helper 上又把它放了回来）；
    ③ `LABEL = "[目标] 线上生产库"` 是一个从不被打印的死赋值（写侧早就不认了，读侧那一档还认）。
    对照组是同形状的**真**证据：helper 真的返回 sqlite 串、目标真的被 print 出来。
    """
    scripts = _scan_into(tmp_path, {
        '_x_named_mirror.py': 'import os\n'
                              'os.environ["DATABASE_URL"] = os.environ["SUPABASE_MIRROR_URL"]\n',
        '_x_docstring_helper.py': 'import os\n\n'
                                  'def pick_url():\n'
                                  '    """这只库是本地 sqlite 镜像副本。"""\n'
                                  '    return os.environ["MAINT_DB_URL"]\n\n'
                                  'os.environ["DATABASE_URL"] = pick_url()\n',
        '_x_two_returns.py': 'import os\n\n'
                             'def pick_url(prod):\n'
                             '    if prod:\n        return os.environ["MAINT_DB_URL"]\n'
                             '    return "sqlite:///data/copy.db"\n\n'
                             'os.environ["DATABASE_URL"] = pick_url(bool(os.environ.get("P")))\n',
        '_x_reassign_url.py': 'import os\n'
                              'url = "sqlite:///data/copy.db"\n'
                              'if os.environ.get("PROD"):\n'
                              '    url = os.environ["MAINT_DB_URL"]\n'
                              'os.environ["DATABASE_URL"] = url\n',
        # ↓ 第 46 轮 A-M1 / B-M3：以前只问"表达式里**出没出现过** sqlite 字样"，这三条都能过
        '_x_ternary_switch.py': 'import os\n'
                                'os.environ["DATABASE_URL"] = "sqlite:///data/copy.db"'
                                ' if a.local else os.environ["PROD_DB_URL"]\n',
        '_x_env_default.py': 'import os\n'
                             'os.environ["DATABASE_URL"] = os.environ.get('
                             '"ANY_DB", "sqlite:///data/copy.db")\n',
        # ↓ 第 46 轮 A-M2：helper 的正常出口是 sqlite，出事那条出口返回环境变量
        '_x_except_exit.py': 'import os\n\n'
                             'def local_url():\n'
                             '    try:\n        return "sqlite:///data/mirror.db"\n'
                             '    except Exception:\n        return os.environ["DATABASE_URL"]\n\n'
                             'os.environ["DATABASE_URL"] = local_url()\n',
        '_x_dead_label.py': 'LABEL = "[目标] 线上生产库（引擎级只读）"\n'
                            'X = "postgresql_readonly"\n',
        # 对照：真证据
        '_x_real_helper.py': 'import os\n\n'
                             'def to_url(p):\n'
                             '    return "sqlite:///" + str(p)\n\n'
                             'os.environ["DATABASE_URL"] = to_url("data/copy.db")\n',
        '_x_both_arms_sqlite.py': 'import os\n\n'
                                  'def url(local):\n'
                                  '    return "sqlite:///data/copy.db" if local else '
                                  '"sqlite:///data/mirror.db"\n\n'
                                  'os.environ["DATABASE_URL"] = url(bool(os.environ.get("L")))\n',
        '_x_printed_label.py': 'print("[目标] 线上生产库（引擎级只读）")\n',
    }, monkeypatch)
    got = {n: scripts[n]['env_written'] for n in
           ('_x_named_mirror.py', '_x_docstring_helper.py', '_x_two_returns.py',
            '_x_real_helper.py', '_x_ternary_switch.py', '_x_env_default.py',
            '_x_except_exit.py', '_x_both_arms_sqlite.py')}
    assert got['_x_named_mirror.py'] is False, '变量名里有 `MIRROR` 就判"方向是 sqlite"'
    assert got['_x_docstring_helper.py'] is False, '函数 docstring 里的"sqlite"判成了返回值的方向'
    assert got['_x_two_returns.py'] is False, \
        'helper 有一条出口返回的是环境变量 ⇒ 方向取决于走到哪条分支，不能算证明'
    for name, why in (('_x_ternary_switch.py', '一条**三目**里有一臂是环境变量：本地/生产开关'),
                      ('_x_env_default.py', '`os.environ.get(k, "sqlite:///…")` 的默认值买不到方向'),
                      ('_x_except_exit.py', '`except` 里那条出口也是出口——出事时才走的那条'
                                            '最可能返回生产串')):
        assert got[name] is False, '%s 被判成"方向已证明"：%s' % (why, name)
        assert scripts[name]['env_written_unknown'] is True, \
            '%s 连"它动过连接串"都没记下来 ⇒ 这一族在台账上隐身' % name
    assert got['_x_real_helper.py'] is True, \
        '真返回 sqlite 串的 helper 被判"方向不明" ⇒ 这条判据在误伤诚实脚本'
    assert got['_x_both_arms_sqlite.py'] is True, \
        '两臂都是 sqlite 字面量的三目被判不明 ⇒ 闸门建成了墙（诚实写法也该走得通）'
    dead = scripts['_x_dead_label.py']
    assert not _says_target(dead) and not _declares_http_target(dead), \
        '从不被打印的 `LABEL = "[目标] …"` 仍算"声明了目标"'
    assert _declares_http_target(scripts['_x_printed_label.py']), '真 print 出来的那一行必须算'


def test_write_capability_is_judged_by_what_a_script_can_do_not_by_its_names(tmp_path, monkeypatch):
    """落笔的**能力**分类要逐类有牙（第 43 轮 B 的"能力×判据"盲区清单）。

    B 席量到 12 类里 10 类三道判据一条不响；这一条把其中最能改数据的六类各做一个样品：
    每一个都必须被判"能改数据"，且都必须**因为没声明库名**而被受管集合抓到。
    对照组 `_x_只读查询.py` 不许被判成能写（否则这条闸会退化成"所有脚本都受管"，
    而误报的闸最后没人信）。
    """
    scripts = _scan_into(tmp_path, {
        '_x_create_all.py': 'from src.models.database import Base, engine\n'
                            'Base.metadata.create_all(engine)\n',
        '_x_raw_sql.py': 'from src.models.database import engine\n'
                         'def go(conn):\n'
                         '    with engine.begin() as c:\n'
                         '        c.execute("DELETE FROM predictions WHERE id = 7")\n',
        '_x_dbapi.py': 'import sqlite3\n'
                       'c = sqlite3.connect("data/fund_insight.db")\n'
                       'c.execute("delete from bloggers"); c.commit()\n',
        '_x_tosql.py': 'import pandas as pd\n'
                       "def go(df):\n    return df.to_sql('predictions', engine, if_exists='replace')\n",
        '_x_file_over.py': 'import shutil\n'
                           "shutil.copyfile('data/copy.db', 'data/fund_insight.db')\n",
        '_x_subproc_ddl.py': 'import subprocess, sys\n'
                             "subprocess.run([sys.executable, 'scripts/run_migrations.py'])\n",
        '_x_http_generic.py': 'import requests\n'
                              "def go(url):\n    return requests.request('DELETE', url)\n",
        # 对照：只读、走门、且没有任何写能力
        '_x_read_only_query.py': 'import _db_guard\n'
                                 'e, s, label = _db_guard.read_only_connect()\n'
                                 'rows = s.execute("select 1")\n',
    }, monkeypatch)
    capable = {n: _write_capable(f) for n, f in scripts.items()}
    for name in ('_x_create_all.py', '_x_raw_sql.py', '_x_dbapi.py', '_x_tosql.py',
                 '_x_file_over.py', '_x_subproc_ddl.py', '_x_http_generic.py'):
        assert capable[name] is True, '%s 这类落笔能力仍然隐身' % name
    assert capable['_x_read_only_query.py'] is False, '只读查询被判成能写 ⇒ 判据过宽，会误伤'
    naked = [n for n, f in scripts.items() if _write_capable(f) and not _guarded(n, f)]
    assert '_x_read_only_query.py' not in naked
    assert set(naked) == set(capable) - {'_x_read_only_query.py'}, \
        '这些"能改数据"的样品没被抓进受管集合：%s' % sorted(set(capable) - set(naked) - {'_x_read_only_query.py'})


def test_the_scanned_set_is_the_repository_s_not_this_disk_s():
    """受管集合必须由 git 定义，不跟着本机那些未入库的 `_tmp_*.py` 草稿变。

    第 41 轮 A 席 M2 把两条判据改成"临时目录现造文件"，可**扫描入口**仍是
    `SCRIPTS.glob('*.py')`：本机 53 个、干净克隆 46 个（差额是 7 个被 `.gitignore` 的草稿）
    ⇒ "我这台机器全绿"与"这个仓库全绿"依然不是同一句话（第 42 轮 A-MINOR-3）。
    """
    import subprocess
    listed = subprocess.run(['git', 'ls-files', '--', 'scripts'], cwd=str(SCRIPTS.parent),
                            capture_output=True, text=True, timeout=60)
    names = {ln.rsplit('/', 1)[-1] for ln in (listed.stdout or '').splitlines()
             if ln.endswith('.py')}
    if not names:
        pytest.skip('这台机器拿不到 git 名单（扫描已退回磁盘范围，并会在 scope 里注明）')
    scanned = set(_scripts())
    assert scanned == names - {'_db_guard.py'}, (
        '受检集合与 git 名单不一致：多 %s、少 %s' % (
            sorted(scanned - names), sorted((names - scanned) - {'_db_guard.py'})))
    on_disk = {p.name for p in SCRIPTS.glob('*.py')}
    extras = sorted(on_disk - scanned)
    # 不在受检集合里的文件必须**叫得出名字**：`_db_guard.py` 是大家调用的守卫本体（不是使用者），
    # 剩下只许是被 `.gitignore` 掉的本机草稿。新来一个"没入库又不是草稿"的脚本 ⇒ 这条变红。
    assert all(e == '_db_guard.py' or e.startswith(('_tmp', '__')) for e in extras), \
        '被排除在受检集合之外的竟然不全是草稿/守卫本体：%s' % extras


def test_the_scanned_set_falls_back_to_disk_when_git_is_gone(tmp_path, monkeypatch):
    """"git 不可用时退回磁盘**并明说**"这句承诺自己得跑过一次（第 44 轮 A-m3）。

    代码里那句 `except Exception` 看着像已经兜住了 `FileNotFoundError`，但从没有用例走过这一支
    ⇒ 它同样兜不住"退回磁盘却仍然把自己报成 git"这种更坏的情形：范围悄悄换了、屏幕上的话没换。
    这里现造"根本没有 git"，两件事都要成立：
    ① 不抛异常，且集合**等于磁盘上的全部**；
    ② `source` 说得清范围换了（含"磁盘"、不含"git"）—— `_scripts().scope` 带着这句话，
       只看"全绿"的人也能看见它靠什么定的范围。
    """
    import subprocess as sp

    def _no_git(*a, **kw):
        raise FileNotFoundError(2, 'git：这台机器上没有')

    monkeypatch.setattr(sp, 'run', _no_git)
    names, source = _tracked_scripts()
    assert source != 'git' and '磁盘' in source, '退回磁盘却仍说自己是 git：%r' % source
    assert names == {p.name for p in SCRIPTS.glob('*.py')}, \
        '退回磁盘那一支给的不是磁盘集合 ⇒ 范围既不是仓库也不是磁盘，谁都不知道是什么'

    # 控制：把 `SCRIPTS` 指到临时目录，磁盘那一支必须真把那里的文件收进来
    # （否则上面那条相等可能在"空集 == 空集"之间成立 = 空判）。
    (tmp_path / '_x_on_disk_only.py').write_text('import os\n', encoding='utf-8')
    monkeypatch.setattr(sys.modules[__name__], 'SCRIPTS', tmp_path)
    names2, source2 = _tracked_scripts()
    assert names2 and '_x_on_disk_only.py' in names2 and '磁盘' in source2, (
        '磁盘退回那一支没把文件收进来：%s / %s ⇒ 上面那条相等是空集对空集' % (sorted(names2), source2))


def _climb(dotted, levels):
    """`_climb('src.models', 1)` → `'src'`：相对 import 里 `..` 往上走几层。"""
    parts = dotted.split('.') if dotted else []
    if levels:
        parts = parts[:len(parts) - levels] if levels <= len(parts) else []
    return '.'.join(parts)


def _src_import_graph():
    """src 包的**顶层** import 图：模块名 → 它 import 时就会执行的模块集合。

    `SCRIPTS` 已经指向 `<repo>/scripts`，所以仓库根是它的 `.parent` 一层
    （上一版我写成 `.parent.parent`，图直接空了 ⇒ 这条判据本来会**恒真**，
    是下面那对控制断言把它抓出来的）。

    第 45 轮 A-M5 / B-m3：上一版把 `node.level`（相对 import 的点数）整个丢了，
    于是 `src/models/__init__.py:3` 的 `from .database import Base, engine, SessionLocal`
    在图里变成裸名字 `database` —— 而 `_reaches_orm` 只顺着 `src.*` 走 ⇒
    **"import `src.models` 会当场建全局 engine"这件事在图里是假的**。
    本仓 `src/fund/__init__.py`、`src/services/__init__.py` 全都用相对 import，
    也就是说这条判据恰好瞎在它最该看见的地方。
    """
    root = SCRIPTS.parent
    src = root / 'src'
    graph = {}
    for py in sorted(src.rglob('*.py')):
        rel = py.relative_to(root).with_suffix('').as_posix().strip('/')
        mod = rel.replace('/__init__', '').replace('/', '.')
        here = rel.rsplit('/', 1)[0].replace('/', '.') if '/' in rel else ''
        try:
            tree = ast.parse(py.read_text(encoding='utf-8', errors='replace'), filename=str(py))
        except SyntaxError:
            graph[mod] = set()
            continue
        out = set()
        for node in tree.body:        # 只看顶层：函数体里的 import 在调用时才跑
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    base = _climb(here, node.level - 1)
                    target = '%s.%s' % (base, node.module) if node.module else base
                else:
                    target = node.module or ''
                if target:
                    out.add(target)
                    for a in node.names:
                        out.add('%s.%s' % (target, a.name))
            elif isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
        graph[mod] = {m for m in out if m}
    return graph


def _reaches_orm(module, graph):
    """`import module` 会不会**顺带**把 `src.models.database` 拉起来（建全局 engine）。

    种子不只这一个名字：**import 一个子模块会先执行它每一层父包的 `__init__.py`**
    （`from src.services import base` 会跑 `src/services/__init__.py`，而那里就是
    `from .l1_weighting import …` 的所在地）。第 45 轮 A-M5 数的正是这一层。
    """
    parts = module.split('.')
    seeds = ['.'.join(parts[:i]) for i in range(1, len(parts) + 1)]
    seen, stack = set(), list(seeds)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        if cur == 'src.models.database':
            return True
        stack.extend(m for m in graph.get(cur, ()) if m.startswith('src'))
    return False


def test_an_orm_import_must_come_after_the_database_is_decided():
    """`import src.*` 排在"定库"之前 = 守卫形同虚设（第 41 轮 B-MINOR-1）。

    为什么判"可达"而不是"名字里有没有 models"：`scripts/backtest_l1_weighting.py` 导入的是
    `src.services.l1_weighting`，而它第 16 行写着 `from src.models.database import Prediction`
    ⇒ 导入的那一刻 `src/models/database.py` 顶层的 `engine = create_engine(DATABASE_URL)`
    已经按 `.env` 那串**生产**地址建好了；之后再 `pin_local_sqlite()` 只是改环境变量，
    救不回那个 engine。这与第 33 轮 `tests/conftest.py` 那次生产误连同源。
    """
    graph = _src_import_graph()
    # 先钉住"可达"这件事本身：一条真会、一条真不会，否则这判据是我编的
    assert _reaches_orm('src.services.l1_weighting', graph), \
        '可达性判据连 l1_weighting→models.database 都走不通 ⇒ 下面的用例是空判'
    assert not _reaches_orm('concurrent.futures', graph), \
        '第三方模块也被判"会拉起本仓 ORM" ⇒ 可达性太宽，会把无辜脚本一起拦下'
    # 第 45 轮 A-M5 之后，"真不会"那一档**在本仓已经不存在了**：`src/__init__.py`
    # 写着 `from src.fund import fund_api, fund_data_manager`，所以
    # `import src.utils.mutation_lock`（一个纯标准库的锁）也会执行父包 `__init__`
    # 并一路拉起全局 engine。旧版这里拿 mutation_lock 当"不会"的对照，
    # 是因为图把相对 import 与父包都看漏了 —— 现在它必须判"会"。
    assert _reaches_orm('src.utils.mutation_lock', graph), \
        '连 `import src.<任何子模块>` 都不算拉起 ORM ⇒ 父包 `__init__` 这条链又断了'

    doors = {'pin_local_sqlite', 'resolve_read_target', 'read_only_connect'}
    offenders = []
    for name, f in sorted(_scripts().items()):
        if name in PRODUCTION_ENTRY:      # Render Cron 入口：就该连生产，护栏是 `database_label`
            continue
        tree = ast.parse(f['text'], filename=name)
        alias = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == '_db_guard':
                for a in node.names:
                    alias[a.asname or a.name] = a.name

        def _is_door(node):
            if not isinstance(node, ast.Call):
                return False
            raw = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
            return raw in doors or alias.get(raw or '') in doors

        door_lines = [node.lineno for node in ast.walk(tree) if _is_door(node)]
        first_door = min(door_lines) if door_lines else 10 ** 9
        for node in tree.body:            # 顶层顺序
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            mod = getattr(node, 'module', None) or ''
            targets = {a.name for a in node.names} | ({mod} if mod else set())
            targets |= {(mod + '.' + a.name) for a in node.names} if mod else set()
            bad = sorted(t for t in targets if t.startswith('src') and _reaches_orm(t, graph))
            if bad and node.lineno < first_door:
                offenders.append('%s 第 %s 行 import %s（定库调用排在它%s）'
                                 % (name, node.lineno, bad[0],
                                    '后面' if first_door < 10 ** 9 else '从来没有'))
    assert offenders == [], '这些脚本在决定连哪个库**之前**就把全局 engine 建好了：%s' % '；'.join(offenders)


def _run_guard_child(code, env):
    """在**子进程**里跑一段真代码（父进程的 sys.modules 已经被 src 占满，钉不了这个洞）。"""
    import os
    import subprocess
    e = dict(os.environ)
    e.update(env)
    e['PYTHONIOENCODING'] = 'utf-8'
    return subprocess.run([sys.executable, '-c', code], cwd=str(SCRIPTS.parent), env=e,
                          capture_output=True, text=True, timeout=180,
                          encoding='utf-8', errors='replace')


# 假得连不上的远程串，只用来验"报错不许泄露口令"（create_engine 不连线，永不碰它）
_FAKE_REMOTE = 'postgresql://someone:SECRETPASS@db.invalid.example/never'


def test_the_door_refuses_to_pin_when_the_engine_is_already_built(tmp_path):
    """`src.models.database` 已经导过 ⇒ 钉库必须当场响，不许"改了环境变量"当成成功。

    为什么上面那条静态判据不够（第 42 轮 B-MAJOR-4）：AST 只能可靠地比**顶层** import
    与门调用的行号。仓库里 100 多处 `from src.…` 写在函数体里（`main()` 才调用），
    按行号比大小要么冤枉一片、要么干脆漏掉——函数体里的顺序**在定义处看不出来**。
    运行期问一句 `sys.modules` 才是真正的执行顺序，且对所有脚本一次性生效。
    """
    mirror = tmp_path / 'mirror.db'
    code = ('import sys, os;'
            "sys.path.insert(0, 'scripts');"
            'import src.models.database as orm;'
            'import _db_guard;'
            '_db_guard.pin_local_sqlite(use_mirror_default=True);'
            "print('AFTER-THE-DOOR', os.environ.get('DATABASE_URL', ''))")
    out = _run_guard_child(code, {'DATABASE_URL': _FAKE_REMOTE, 'LOCAL_DB_URL': str(mirror)})
    assert out.returncode == 4, 'engine 已经按远程串建好了，钉库却"成功" ⇒ 守卫在自证清白：%s' % out.stdout
    assert '来得太晚' in out.stdout, out.stdout
    assert 'SECRETPASS' not in out.stdout + out.stderr, 'abort 信息把口令原样印出来了'
    assert 'src.models.database' in out.stdout
    # 报的是**当时那个**目标（引擎已经焊死的地方），不是我想去的镜像
    assert 'db.invalid.example' in out.stdout, out.stdout

    # 控制：顺序反过来（先钉库再导入）必须**放行**，否则这条判据恒真
    ok = _run_guard_child(
        'import sys;'
        "sys.path.insert(0, 'scripts');"
        'import _db_guard;'
        '_db_guard.pin_local_sqlite(use_mirror_default=True);'
        'import src.models.database as orm;'
        "print('ENGINE', orm.engine.url)",
        {'DATABASE_URL': _FAKE_REMOTE, 'LOCAL_DB_URL': str(mirror)})
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert 'sqlite' in str(ok.stdout).lower() and 'mirror.db' in ok.stdout, \
        '先钉库再导入，engine 却没落在镜像上 ⇒ 这条控制没在放行正确的路：%s' % ok.stdout


def test_an_engine_already_built_on_sqlite_warns_instead_of_aborting(tmp_path):
    """分档：已建在**非 SQLite** 才 abort；已建在 SQLite 只警告并说清写的是哪个文件。

    为什么要这一档：`tests/conftest.py` 就是把全局 engine 建在临时 SQLite 上的，
    一律 abort 会把 8 条"脚本只写它说的那个库"的用例一起打死（那是把闸门建成墙）。
    但静默放行同样是撒谎 —— 所以这里必须**看得见**那句警告，且它报的是真文件名。
    """
    first = tmp_path / 'first.db'
    out = _run_guard_child(
        'import sys;'
        "sys.path.insert(0, 'scripts');"
        'import src.models.database as orm;'
        'import _db_guard;'
        '_db_guard.pin_local_sqlite(use_mirror_default=True);'
        "print('CONTINUED', orm.engine.url)",
        {'DATABASE_URL': 'sqlite:///%s' % first.as_posix(), 'LOCAL_DB_URL': ''})
    assert out.returncode == 0, out.stdout + out.stderr
    assert '[警告]' in out.stdout and 'first.db' in out.stdout, out.stdout
    assert 'CONTINUED' in out.stdout and 'first.db' in out.stdout.split('CONTINUED')[-1], \
        '警告之后脚本继续跑了，但它写的还是原来那个文件 —— 这句话必须说得住：%s' % out.stdout


def test_pinning_also_refuses_when_a_stray_module_holds_a_remote_engine(tmp_path):
    """钉库要问的是"进程里**还有谁**连着别处"，不是只问 `src.models.database` 那一个名字。

    第 44 轮 B-MAJOR-6 的复现形状：`already_built_url()` 只认 `sys.modules` 里那一个键名，
    于是把那条标记抹掉（`del sys.modules['src.models.database']`，或任何从没注册过的等价写法）
    就能让钉库"看起来成功"，而调用方手里那个 engine 对象仍然绑在生产串上 ——
    守卫自证清白，写照样落线上。

    这里**不真导入 ORM**（那会当场创建一个绑远程串的 engine，正是这个项目反复避免的动作），
    而是现造两种"调用方"：① 直接挂在 `sys.modules` 上的模块，② 只作为父包属性存在的子模块
    （`import src.models.database as x` 之后 `x` 就是这种引用）。
    """
    mirror = tmp_path / 'mirror.db'
    env = {'DATABASE_URL': _FAKE_REMOTE, 'LOCAL_DB_URL': str(mirror)}
    holder = ("holder = types.ModuleType('caller_module'); "
              "holder.__file__ = 'scripts/fake_caller.py'; "      # 扫描只认"这个仓库里的模块"
              "holder.engine = sa.create_engine(%r); "
              "sys.modules['caller_module'] = holder; " % _FAKE_REMOTE)
    package = ("fake_db = types.ModuleType('src.models.database'); "
               "fake_db.engine = sa.create_engine(%r); "
               "parent = types.ModuleType('src.models'); parent.database = fake_db; "
               "sys.modules['src.models'] = parent; " % _FAKE_REMOTE)
    prelude = ('import sys, types, sqlalchemy as sa;'
               "sys.path.insert(0, 'scripts');"
               'import _db_guard;\n')      # 换行是必须的：`;class` 写在同一行是语法错误
    tail = ('_db_guard.pin_local_sqlite(use_mirror_default=True);'
            "print('PINNED', os.environ.get('DATABASE_URL', '')[:7])")
    for label, body in (('sys.modules 里的调用方', holder), ('只挂在父包上的子模块', package)):
        out = _run_guard_child(prelude + 'import os;' + body + tail, env)
        assert out.returncode == 4, \
            '%s 手里握着生产 engine，钉库却报成功 ⇒ 守卫仍然只认那一个键名：%s' % (
                label, out.stdout[-400:])
        assert '来得太晚' in out.stdout, out.stdout
        assert 'SECRETPASS' not in out.stdout + out.stderr, 'abort 信息把口令原样印出来了'
        assert 'db.invalid.example' in out.stdout, '报的不是**当时那个**目标：%s' % out.stdout

    # 控制：形状一样、但那个引用连的是 SQLite ⇒ 必须放行（否则上面两条恒红，
    # 而这道闸会变成一堵谁都用不了的门）。
    ok = _run_guard_child(prelude + 'import os;' + holder.replace(
        'sa.create_engine(%r)' % _FAKE_REMOTE,
        "sa.create_engine('sqlite:///%s')" % mirror.as_posix()) + tail, env)
    assert ok.returncode == 0, '本地 sqlite 的引用也被 abort ⇒ 这道门建成了墙：%s' % (
        ok.stdout + ok.stderr)[-300:]
    assert 'PINNED sqlite' in ok.stdout, ok.stdout


def test_the_stray_probe_also_sees_async_engines_and_instance_holders(tmp_path):
    """散落连接扫描的两处隐身：async 一族、以及**实例属性**（第 46 轮 B-M11）。

    ① `issubclass(AsyncEngine, sa.engine.Engine)` 实测是 **False** —— async 会话工厂
      与 `async_sessionmaker` 同样不是同步类的子类，而上一版按同步的五个类做 isinstance，
      于是 `src.models.database` 换成 async 引擎就整族免检（而 `_url_of` 的注释里
      明写着上一版按名字找的三个名字之一就是 `async_engine`）。
    ② 本仓有十几处 `self.db = db or SessionLocal()`（`src/tasks/cleanup_enhanced.py:100`）
      —— 模块直接持有工厂看得见，持有**实例**看不见。
    两条都用真子进程跑：不连线（`create_engine` 只把 URL 焊进对象），也不 import src。
    """
    prelude = ('import sys, types, sqlalchemy as sa;'
               "sys.path.insert(0, 'scripts');"
               'import _db_guard;\n')
    tail = ('_db_guard.pin_local_sqlite(use_mirror_default=True);'
            "print('PINNED')")
    env = {'DATABASE_URL': _FAKE_REMOTE, 'LOCAL_DB_URL': str(tmp_path / 'm.db')}
    async_shape = (
        # 本机没装 asyncpg，`create_async_engine` 会在建引擎时就 import_dbapi 崩掉；
        # 而这条判据要测的是"扫描认不认得 async 那一族的**类型**"，不是驱动在不在。
        # `async_sessionmaker` 构造时不校验 bind ⇒ 用它来代表这一族
        # （`issubclass(async_sessionmaker, sessionmaker)` 实测是 False ⇒ 旧 isinstance 名单收不到）。
        "from sqlalchemy.ext.asyncio import async_sessionmaker; "
        "m = types.ModuleType('src.async_holder'); "
        "m.SessionLocal = async_sessionmaker(bind=sa.create_engine(%r)); "
        "sys.modules['src.async_holder'] = m; " % _FAKE_REMOTE)
    instance_shape = (
        "class Holder:\n"
        "    pass\n"
        "Holder.__module__ = 'src.services.holder'\n"
        "m = types.ModuleType('src.instance_holder');\n"
        "h = Holder();\n"
        "h.db = sa.create_engine(%r);\n"
        "m.holder = h;\n"
        "sys.modules['src.instance_holder'] = m;\n" % _FAKE_REMOTE)
    for label, shape in (('async 引擎', async_shape), ('实例属性里的 engine', instance_shape)):
        got = _run_guard_child(prelude + shape + tail, env)
        assert got.returncode == 4, \
            '%s 握着生产 engine，钉库却报成功 ⇒ 这一类仍然免检：%s' % (
                label, (got.stdout + got.stderr)[-400:])
        assert 'SECRETPASS' not in got.stdout + got.stderr
    # 控制：同一个形状换成 SQLite 必须**放行**，否则我只是把扫描变成了墙
    ok = _run_guard_child(prelude + instance_shape.replace(
        _FAKE_REMOTE, 'sqlite:///%s' % (tmp_path / 'x.db').as_posix()) + tail, env)
    assert ok.returncode == 0, '本机 SQLite 的实例持有者也被判成远程 ⇒ 误伤：%s' % ok.stdout[-300:]


def test_the_stray_probe_never_bricks_the_door_itself(tmp_path):
    """探测"散落连接"这一步不许把守卫自己弄成故障源（第 44 轮我自己踩出来的）。

    第一版对 `sys.modules` 里**每个**值取 `vars()`，而 Windows 上那里面混着 `kernel32.dll`
    这类 ctypes 对象 —— 对它取 `vars()` 直接抛
    `ffi.error: symbol 'RtlNtStatusToDosError' not found in library 'kernel32.dll'`，
    于是 `pin_local_sqlite()` 当场崩、**12 条** in-process 用例全红
    （`test_audit_fund_info_identity` ×4 / `test_sector_seed_route_honesty` ×2 /
    `test_seed_owner_proxies_gate` ×4 / `test_snapshot_prod_mappings` ×2 ——
    这份分布是那次失败清单里当场读出来的，不是回忆）。
    现在只认"这个仓库里的真模块"（`type is ModuleType` 且名字以 `src` 开头或 `__file__` 在仓库内）。

    对照（第二半）：加了这层过滤之后，**真**散落的远程 engine 仍然必须被拦住 ——
    否则"修崩溃"就等于把闸门拆了。
    """
    junk = ('class Weird(types.ModuleType):\n'
            '    @property\n'
            '    def __dict__(self):\n'
            '        raise RuntimeError("symbol not found")\n'
            'sys.modules["weird_thing"] = Weird("weird_thing")\n')
    stray = ("holder = types.ModuleType('src.fake_holder'); "
             "holder.engine = sa.create_engine(%r); "
             "sys.modules['src.fake_holder'] = holder; " % _FAKE_REMOTE)
    prelude = ('import sys, types, sqlalchemy as sa;'
               "sys.path.insert(0, 'scripts');"
               'import _db_guard;\n')      # 换行是必须的：`;class` 写在同一行是语法错误
    tail = ('_db_guard.pin_local_sqlite(use_mirror_default=True);'
            "print('PINNED')")
    env = {'DATABASE_URL': _FAKE_REMOTE, 'LOCAL_DB_URL': str(tmp_path / 'm.db')}

    clean = _run_guard_child(prelude + junk + tail, env)
    assert clean.returncode == 0, '守卫自己被怪对象弄崩（rc=%s）：%s' % (
        clean.returncode, (clean.stdout + clean.stderr)[-500:])
    assert 'PINNED' in clean.stdout, clean.stdout

    caught = _run_guard_child(prelude + junk + stray + tail, env)
    assert caught.returncode == 4, \
        '加了"只扫仓库内真模块"这层过滤之后连真的散落连接也不拦了 ⇒ 修崩溃把闸门拆了：%s' % (
            caught.stdout[-400:])
    assert 'SECRETPASS' not in caught.stdout + caught.stderr, caught.stdout


if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import migration_policy    # noqa: E402  迁移判据的**唯一**实现（运行期与测试共用一份）

# 迁移在**往上走**的那一支里删结构/删数据 = apply 即丢东西。今天一支都没有
# （实测 9 支的删除全在 `downgrade()`，那是回滚路径，属于正常写法）。
# **名单只有一份**：`alembic/destructive-upgrades.json`，由 `migration_policy.load_allowlist()`
# 读。第 46 轮 B-M1 数的就是这件事：上一版这里另写了一个本地空集合 `DATA_LOSSING_UPGRADES`
# 传进 `audit()` ⇒ "登记进 JSON"这个动作会让运行期放行、让测试变红，
# 而错误消息还指挥人"去登记进 destructive-upgrades.json"（死循环）。
# 下面那条 `test_the_allowlist_under_test_is_the_one_runtime_reads` 钉住"不再出现第二份"。


def _migration_dir():
    return Path(__file__).resolve().parents[2] / 'alembic' / 'versions'


def _tracked_migrations():
    """同样以仓库为准（`git ls-files`），拿不到 git 才退回磁盘并说明。"""
    import subprocess
    versions = _migration_dir()
    try:
        out = subprocess.run(['git', 'ls-files', '--', 'alembic/versions'],
                             cwd=str(versions.parent.parent), capture_output=True,
                             text=True, timeout=60)
        names = {ln.rsplit('/', 1)[-1] for ln in (out.stdout or '').splitlines()
                 if ln.endswith('.py')}
        if names:
            return sorted(versions / n for n in names)
    except Exception:                                  # noqa: BLE001  没 git / 超时
        pass
    return sorted(versions.glob('*.py'))


def _migration_facts(path):
    """**委托给 `scripts/migration_policy.py`**：判据只许有一份实现。

    第 45 轮 B-M-2 数的就是这件事：上一版这支扫描住在测试文件里，而每次 Render 启动
    真正 apply 迁移的是 `run_migrations.py` —— 它从不跑 pytest，也就不读这张名单
    ⇒ "apply 即丢数据要登记"只在有人记得跑测试时成立。现在测试与运行期共用同一份代码。
    """
    return migration_policy.scan_migration(str(path))


def _scan_migrations(tmp_path, files):
    """把样品写进临时目录再逐支解析（判据不许依赖我这台机器上恰好有哪几支迁移）。"""
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding='utf-8')
    return {name: _migration_facts(tmp_path / name) for name in files}


def test_migrations_are_scanned_for_data_loss_on_the_way_up(tmp_path):
    """`alembic/versions/*.py` 以前不在**任何**扫描范围里（第 44 轮 B-m4、第 45 轮 A-M8/B-M2）。

    守卫扫描只看 `scripts/*.py`；迁移文件不改数据、不 commit，也不走 `_write_capable`
    那三条判据 —— 于是"往上有 `drop_table` 的一支迁移"可以在没人问一句的情况下
    被 `run_migrations.py`（Render 每次启动都跑它）发到生产。
    `alembic/env.py` 那道方向闸管的是"能不能连过去"，管不了"过去之后删什么"。

    四条账：① 每一支都得有 `upgrade` 与 `downgrade`；② `upgrade` 里删结构/删数据必须
    登记在 `alembic/destructive-upgrades.json` 并写明原因（今天为空 —— 实测 9 支的删除
    全在 `downgrade`，那是回滚路径）；③ 参数是变量、我看不见内容的语句**也要登记**
    （猜"应该没事"的代价是删掉生产表）；④ 判据自己会响：下面现造九支样品，
    七支坏写法逐一点名（含 alembic 文档里的正规 raw SQL 写法、两跳 helper、
    `getattr(op, "drop_" + "table")`、根级别直接执行），两支正常写法不许误伤。
    """
    migrations = _tracked_migrations()
    assert len(migrations) >= 9, '只认出 %d 支迁移 ⇒ 扫描范围变了，这条判据快成空判' % len(migrations)
    problems, seen = migration_policy.audit(str(_migration_dir()))
    assert seen == len(migrations), '仓库里 %d 支、审计只看了 %d 支' % (len(migrations), seen)
    assert problems == [], ('这些迁移在**往上走**的那一支里删东西或让我看不清：%s ⇒ '
                            '结构变更要先问老板；真要上，就登记进 '
                            'alembic/destructive-upgrades.json 并写明原因' % '；'.join(problems))

    made = _scan_migrations(tmp_path, {
        # ① 最朴素的写法（上一版唯一认得的一种）
        '_x_upgrade_drops.py': ('from alembic import op\n'
                                'def upgrade():\n    op.drop_table("prediction_change_logs")\n'
                                'def downgrade():\n    pass\n'),
        # ② alembic 文档里的正规 raw SQL 写法（上一版隐身）
        '_x_raw_text_drop.py': ('from alembic import op\nimport sqlalchemy as sa\n'
                                'def upgrade():\n    op.execute(sa.text("DROP TABLE x"))\n'
                                'def downgrade():\n    pass\n'),
        # ③ 连接对象直接执行：TRUNCATE 不在动词表里也不行
        '_x_truncate.py': ('from alembic import op\n'
                           'def upgrade():\n    op.get_bind().execute("TRUNCATE predictions")\n'
                           'def downgrade():\n    pass\n'),
        # ④ f-string 拼出来的 DROP（不是 Constant，上一版直接跳过）
        '_x_fstring_drop.py': ('from alembic import op\nT = "x"\n'
                               'def upgrade():\n    op.execute(f"DROP TABLE {T}")\n'
                               'def downgrade():\n    pass\n'),
        # ⑤ 两跳 helper：upgrade → _a → _b → drop
        '_x_two_hop.py': ('from alembic import op\n'
                          'def _b():\n    op.drop_table("audit_log")\n'
                          'def _a():\n    _b()\n'
                          'def upgrade():\n    _a()\n'
                          'def downgrade():\n    pass\n'),
        # ⑥ 根级别（import 这支迁移就执行，不用等 upgrade()）
        '_x_module_level.py': ('from alembic import op\n'
                               'op.drop_table("prediction_change_logs")\n'
                               'def upgrade():\n    pass\n'
                               'def downgrade():\n    pass\n'),
        # ⑦ 动态取方法名
        '_x_dynamic.py': ('from alembic import op\n'
                          'def upgrade():\n    getattr(op, "drop_" + "table")("x")\n'
                          'def downgrade():\n    pass\n'),
        # ⑧ 参数是个变量：看不见内容 ⇒ 不许当成"没事"
        '_x_variable_sql.py': ('from alembic import op\nSQL = _whatever()\n'
                               'def upgrade():\n    op.execute(SQL)\n'
                               'def downgrade():\n    pass\n'),
        # ⑨ 正常迁移：删除只出现在回滚那一支，且加列时用了 `sa.text(默认值)`
        '_x_clean.py': ('from alembic import op\nimport sqlalchemy as sa\n'
                        'def upgrade():\n    op.add_column("bloggers", sa.Column('
                        '"grade", sa.Text, server_default=sa.text("x")))\n'
                        'def downgrade():\n    op.drop_column("bloggers", "grade")\n'),
        # ⑩ 少一个 downgrade：不可回滚
        '_x_no_downgrade.py': ('from alembic import op\n'
                               'def upgrade():\n    op.add_column("bloggers", None)\n'),
    })
    for name in ('_x_upgrade_drops.py', '_x_raw_text_drop.py', '_x_truncate.py',
                 '_x_fstring_drop.py', '_x_two_hop.py', '_x_module_level.py', '_x_dynamic.py'):
        assert made[name]['upgrade_drops'], '%s 在 upgrade 那一支删东西却没被点名 ⇒ 换这种写法就绕过' % name
    assert made['_x_variable_sql.py']['upgrade_unclear'], \
        'SQL 是个变量就当"没事" ⇒ 猜错的代价是删掉生产表'
    assert made['_x_clean.py']['upgrade_drops'] == [] and not made['_x_clean.py']['upgrade_unclear'], \
        '正常迁移被误伤（回滚路径的删除、或 `sa.text(默认值)`）⇒ 这条闸会变成挡路的墙：%s' \
        % made['_x_clean.py']
    assert made['_x_no_downgrade.py']['has_downgrade'] is False, made

    # 运行期那一半：登记了就不拦、没登记就拦（判据与 `run_migrations.py` 用的是同一份代码）
    problems, _seen = migration_policy.audit(
        str(tmp_path), {'_x_upgrade_drops.py': '老板 2026-09-xx 批准：这张表已废弃，前像已导出'})
    assert not any('_x_upgrade_drops.py' in p for p in problems), \
        '登记过的迁移仍被拦 ⇒ 名单不起作用：%s' % problems
    assert any('_x_raw_text_drop.py' in p for p in problems), problems
    no_reason, _s = migration_policy.audit(str(tmp_path), {'_x_raw_text_drop.py': '   '})
    assert any('_x_raw_text_drop.py' in p and '原因' in p for p in no_reason), \
        '登记却什么都不写 ⇒ 名字成了盖章，这条闸就白建了：%s' % no_reason


def test_a_migration_that_hides_by_position_or_depth_is_still_named(tmp_path):
    """B-M4 / A-M5（第 46 轮）：同一句危险 SQL **换个位置、换个深度**就隐身。

    上一版的第二条循环只认"这一整个语句就是一次执行"（`ast.Expr`），于是
    `r = op.execute("DROP TABLE t")`、`if op.execute("TRUNCATE t"):`、推导式、`with` 块
    四种写法全部看不见，而且**连"看不清"都不标** —— 审计回的是"干净"，比报"可疑"更坏。
    另一半：`_funcs` 只收模块级函数 ⇒ 藏在嵌套 `def` 里的 `os.system("alembic downgrade -1")`
    也一样隐身。
    反面必须同时成立：`server_default=sa.text("now()")`、`json.loads(SETTINGS)`、
    `df.apply(...)` 不许被误伤 —— 第 45 轮那次误报就是把闸门建成墙，
    Render 每次启动都会因此退 4（而墙是会被绕的，绕的人是我自己）。
    """
    head = ('from alembic import op\nimport sqlalchemy as sa\nimport json\nimport pandas as pd\n'
            'import subprocess\n')
    tail = '\ndef downgrade():\n    pass\n'
    made = _scan_migrations(tmp_path, {
        '_x_assigned_execute.py': head + 'def upgrade():\n    r = op.execute("DROP TABLE t")\n' + tail,
        '_x_in_condition.py': head + 'def upgrade():\n    if op.execute("TRUNCATE t"):\n        pass\n' + tail,
        '_x_in_comprehension.py': head + 'def upgrade():\n    xs = [op.execute(s) for s in SQLS]\n' + tail,
        '_x_in_with.py': head + 'def upgrade():\n    with op.get_bind() as c:\n        r = c.execute(VAR)\n' + tail,
        '_x_nested_def.py': head + 'def upgrade():\n    def inner():\n        op.drop_table("t")\n    inner()\n' + tail,
        '_x_nested_system.py': head + 'def upgrade():\n    def inner():\n        import os\n'
                                   '        os.system("alembic downgrade -1")\n    inner()\n' + tail,
        '_x_subprocess.py': head + 'def upgrade():\n    subprocess.run(["psql", "-c", SQL])\n' + tail,
        # ↓ 三条对照：正常写法，一条都不许报
        '_x_c_default.py': head + 'def upgrade():\n    op.add_column("t", sa.Column('
                               '"c", sa.Text, server_default=sa.text("now()")))\n' + tail,
        '_x_json.py': head + 'def upgrade():\n    cfg = json.loads(SETTINGS)\n' + tail,
        '_x_apply.py': head + 'def upgrade():\n    df.apply(lambda r: r, axis=1)\n' + tail,
    })
    for name in ('_x_assigned_execute.py', '_x_in_condition.py', '_x_nested_def.py'):
        assert made[name]['upgrade_drops'], \
            '%s 在执行危险语句却因为"不在语句位置/不在顶层"而隐身：%s' % (
                name, made[name])
    for name in ('_x_in_comprehension.py', '_x_in_with.py', '_x_nested_system.py',
                 '_x_subprocess.py'):
        assert made[name]['upgrade_unclear'], \
            '%s 把内容交给变量/子进程，我看不见却报了"干净"（fail-open）：%s' % (
                name, made[name])
    for name in ('_x_c_default.py', '_x_json.py', '_x_apply.py'):
        facts = made[name]
        assert not facts['upgrade_drops'] and not facts['upgrade_unclear'], \
            '正常迁移被误伤 ⇒ 每次 Render 启动都会为它退 4：%s %s' % (name, facts)


def test_the_gate_walks_the_same_tree_alembic_will_apply(tmp_path):
    """这道闸"看见的迁移集合"必须等于 alembic 将要 apply 的集合（第 46 轮 B-M5）。

    旧写法 `os.listdir` 只看顶层，而 alembic 收版本目录用的是 `path_walk`（递归）
    ⇒ 子目录里放一支 `op.drop_table("predictions")`，屏幕上照样印"已核对 N 支、0 处未登记"，
    然后那支从未被核对的迁移就被 apply 了。
    另外两条一起钉：目录空/路径打错 ⇒ 不许报"干净"（一支都没看见＝尺子没跑）；
    名单传 set（A-m8 的 `AttributeError: 'set' object has no attribute 'get'`）⇒ 不许崩。
    """
    (tmp_path / 'nested').mkdir()
    (tmp_path / 'top_ok.py').write_text(
        'from alembic import op\ndef upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n',
        encoding='utf-8')
    (tmp_path / 'nested' / 'deep_drop.py').write_text(
        'from alembic import op\n'
        'def upgrade():\n    op.drop_table("predictions")\n\n\ndef downgrade():\n    pass\n',
        encoding='utf-8')
    problems, seen = migration_policy.audit(str(tmp_path), {})
    assert seen == 2, '递归没走：看见 %d 支，而 alembic 会 apply 2 支 ⇒ 子目录那支免检' % seen
    assert any('deep_drop' in p for p in problems), \
        '子目录里那支删 `predictions` 的迁移没被点名：%s' % problems
    for p in problems:                      # 报的是相对路径，人要能照着登记
        assert not p.startswith(str(tmp_path)), '消息里印的是绝对路径，没人能复制：%s' % p

    empty_problems, empty_seen = migration_policy.audit(str(tmp_path / 'nope'), {})
    assert empty_seen == 0 and empty_problems, \
        '目录不存在/一支都没看见 ⇒ 却可以"没有问题"，这是尺子没跑而不是干净'
    set_named, _ = migration_policy.audit(str(tmp_path), {'nested/deep_drop.py'})
    assert not any('deep_drop' in p and '未登记' in p for p in set_named), \
        '名单传 set 就崩/失效（A-m8）⇒ 同一份登记在两种类型下要走同一条路：%s' % set_named


def test_the_allowlist_under_test_is_the_one_runtime_reads(tmp_path, monkeypatch):
    """测试对账的那张名单，必须**就是**运行期 `run_migrations.py` 读的那份 JSON（第 46 轮 B-M1）。

    上一版这里传的是本文件自己的 `DATA_LOSSING_UPGRADES = set()`，注释却写着
    "测试与 run_migrations 读同一份" ⇒ 那句话是恒假的（两边都是空，所以看不出来）。
    真后果：将来谁把一支丢数据迁移**按文档登记进 JSON**，运行期放行、pytest 变红，
    而失败消息还指挥人"去登记进 destructive-upgrades.json" —— 一个转圈的假闸门。
    三条一起钉：① 本文件里不许再出现第二份名单常量；② `run_migrations()` 走的确实是
    `load_allowlist()`；③ 把一支危险迁移登记进**临时 JSON** ⇒ `audit()`（不传名单）
    必须同时放行 —— 这一条是"登记这个动作真的有用"的控制断言，没有它①②都能空过。
    """
    # 判 AST，不判文本：我第一版用 grep 读本文件，结果被**解释这句话的那段注释**自己点红
    # —— 与第 44 轮"文本 grep 会被自家 docstring 点红"完全同一课，这次栽的是我自己刚写的判据。
    module_names = {t.id for n in ast.parse(Path(__file__).read_text(encoding='utf-8')).body
                    if isinstance(n, ast.Assign)
                    for t in n.targets if isinstance(t, ast.Name)}
    assert 'DATA_LOSSING_UPGRADES' not in module_names, \
        '测试文件里又长出一份模块级名单常量 ⇒ "只有一份实现"这句话只剩一半'

    source = (SCRIPTS / 'run_migrations.py').read_text(encoding='utf-8')
    assert 'migration_policy' in source, '运行期不读那份判据 ⇒ 两张表又各说各话'
    # 行为判据，不判字符串：把 JSON 换到临时目录并**登记这一支**，运行期那道闸必须跟着放行。
    # （"测试与运行期读同一份名单"这句话，只有拿一次真的登记动作去问它才算被检验过。）
    import json as _json2
    import importlib.util as _ilu
    allow2 = tmp_path / 'runtime-allow.json'
    _json2.dump({}, open(str(allow2), 'w', encoding='utf-8'))
    monkeypatch.setattr(migration_policy, 'ALLOWLIST', str(allow2))
    spec2 = _ilu.spec_from_file_location('run_migrations_allowlist_probe',
                                         str(SCRIPTS / 'run_migrations.py'))
    run_migrations = _ilu.module_from_spec(spec2)
    spec2.loader.exec_module(run_migrations)
    versions2 = tmp_path / 'versions2'
    versions2.mkdir()
    (versions2 / '_x_runtime.py').write_text(
        'from alembic import op\ndef upgrade():\n    op.drop_table("gone")\n'
        'def downgrade():\n    pass\n', encoding='utf-8')
    import pytest as _pt
    with _pt.raises(SystemExit) as before:
        run_migrations._migration_gate(versions_dir=str(versions2))
    assert before.value.code == 4, '未登记时运行期居然放行 ⇒ 下面那半没有对照意义'
    _json2.dump({'_x_runtime.py': '老板批准：前像已导出'},
                open(str(allow2), 'w', encoding='utf-8'))
    run_migrations._migration_gate(versions_dir=str(versions2))     # 不抛＝放行

    versions = tmp_path / 'versions'
    versions.mkdir()
    (versions / '_x_registered.py').write_text(
        'from alembic import op\ndef upgrade():\n    op.drop_table("gone")\n'
        'def downgrade():\n    pass\n', encoding='utf-8')
    allow = tmp_path / 'allow.json'
    unregistered, _ = migration_policy.audit(str(versions))
    assert any('_x_registered.py' in p for p in unregistered), \
        '没登记时居然不报 ⇒ 下面的控制没有对照意义'
    import json as _json
    _json.dump({'_x_registered.py': '老板 2026-09-25 批准：该表前像已导出'},
               open(str(allow), 'w', encoding='utf-8'))
    monkeypatch.setattr(migration_policy, 'ALLOWLIST', str(allow))
    after, seen = migration_policy.audit(str(versions))          # **不传**名单：读的就是这份 JSON
    assert seen == 1 and not any('_x_registered.py' in p and '未登记' in p for p in after), after
    _json.dump({'_x_registered.py': None}, open(str(allow), 'w', encoding='utf-8'))
    null_reason, _ = migration_policy.audit(str(versions))
    assert any('原因' in p for p in null_reason), \
        '登记值写 `null`/`{}`/`0` 也算"写了原因" ⇒ 空登记就是通行证（B-minor-1）：%s' % null_reason


def test_the_dry_run_report_does_not_claim_a_ddl_is_coming(tmp_path, capsys):
    """`--dry-run` 那一支的自报行不许说"会向它发 DDL"（第 46 轮我自己撞出来的措辞）。

    跑 `python scripts/run_migrations.py --dry-run` 时屏幕上曾印：
    `[库] 线上生产库（…） —— alembic upgrade head 会向它发 DDL`，紧接着一行才是
    "只报目标，没连线、没发 DDL"。两句互相打脸，而**第一句是操作者决定要不要接着敲的那句**
    —— 同一族已经修过三次（`--base` 指本机却自称线上生产库、`ok=True` 却印"已写入"），
    这条措辞当时零覆盖：`_report_target()` 全仓没有任何用例读过它的内容。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location('run_migrations_words',
                                                  str(SCRIPTS / 'run_migrations.py'))
    rm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rm)
    real = rm._report_target()
    dry = rm._report_target(dry_run=True)
    assert real.startswith('[库]') and dry.startswith('[库]'), (real, dry)
    assert '发 DDL' in real and 'dry-run' not in real, real
    assert '不发 DDL' in dry or '不会' in dry, \
        'dry-run 那一支仍把目标说成"马上要动它"：%s' % dry
    assert dry != real, '两档共用同一句话 ⇒ 其中一档一定在说谎'
    # 控制：这句必须**真的**带了目标库，否则"改措辞"可以改成什么都不报
    assert len(dry.split('——')[0]) > len('[库] '), dry


def test_run_migrations_checks_the_migrations_before_it_connects(tmp_path, capsys):
    """核对必须**真的发生在连库之前**，而且用的是同一份判据（第 45 轮 B-M-2 的执行位点）。

    上一轮我把"upgrade 那一支不许偷偷删结构"写成 pytest 里的一条判据 —— 可每次真的 apply
    迁移的是 `run_migrations.py`（`render.yaml:11` 的 startCommand，Render 每次启动都跑），
    它从不跑测试，也就不读那张名单 ⇒ 那句话只在有人记得跑 pytest 时成立。
    三条：① 有一支未登记的丢数据迁移 ⇒ 退 4；② 登记了就放行；
    ③ 在 `run_migrations()` 的函数体里，这道闸的调用行必须**排在 `import src.*` 之前**
      （本仓 `src/__init__.py` 会拉起全局 engine，排在它后面等于"先把引擎连上再想要不要动"）。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location('run_migrations_under_test',
                                                  str(SCRIPTS / 'run_migrations.py'))
    run_migrations = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_migrations)

    versions = tmp_path / 'versions'
    versions.mkdir()
    (versions / '_x_bad_revision.py').write_text(
        'from alembic import op\n'
        'def upgrade():\n    op.drop_table("prediction_change_logs")\n'
        'def downgrade():\n    pass\n', encoding='utf-8')
    import pytest as _pt
    with _pt.raises(SystemExit) as got:
        run_migrations._migration_gate(versions_dir=str(versions), allowlist={})
    assert got.value.code == 4, '未登记的丢数据迁移居然放行 ⇒ 这道闸只是装饰'
    out = capsys.readouterr().out
    assert '[abort]' in out and '_x_bad_revision.py' in out, out

    run_migrations._migration_gate(versions_dir=str(versions),
                                   allowlist={'_x_bad_revision.py': '老板批准，前像已导出'})
    assert '[abort]' not in capsys.readouterr().out, '登记过了仍拦 ⇒ 名单不起作用'

    tree = ast.parse((SCRIPTS / 'run_migrations.py').read_text(encoding='utf-8'))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == 'run_migrations')
    gate = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
            and getattr(n.func, 'id', None) == '_migration_gate']
    orm = [n.lineno for n in ast.walk(fn)
           if isinstance(n, (ast.Import, ast.ImportFrom))
           and 'src' in (getattr(n, 'module', None) or getattr(n, 'names', [None])[0].name or '')]
    assert gate, '`run_migrations()` 里根本没调用这道闸 ⇒ 核对又只活在测试里'
    assert orm, '找不到那句 `from src.models.database import …` ⇒ 顺序判据是空判'
    assert min(gate) < min(orm), (
        '核对排在 import 之后（第 %s 行 vs 第 %s 行）⇒ 全局 engine 已经按 .env 连上了'
        % (min(gate), min(orm)))
