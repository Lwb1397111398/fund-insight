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
HTTP_WRITE = {'post', 'put', 'patch'}     # 会话/requests 的写方法（ORM 侧没有这三个名字）
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
FILE_WRITERS = {'copyfile', 'copy', 'copy2', 'copytree', 'move', 'rename', 'remove', 'unlink',
                'rmtree', 'write_text', 'write_bytes', 'truncate'}
PROC_WRAPPERS = {'run', 'Popen', 'call', 'check_call', 'check_output', 'system', 'popen',
                 'execv', 'execve', 'spawn', 'spawnl'}
GENERIC_HTTP = {'request'}      # `requests.request("DELETE", url)` / `httpx.request(...)`
# 所有"能把状态落到某个库/文件上"的能力类别（第 43 轮 B 的盲区清单逐类补）。
# `engine_from_env` / `orm_session` / `own_engine` **不在这里** —— 它们是"读侧那道闸"的触发条件
# （见 `test_read_side_scripts_also_say_which_database_they_read`），把它们算成"能写"会
# 把三个纯读脚本推进写侧的受管集合，而 `_guarded` 认的信号里没有 `read_only_connect`。
WRITE_CAPABILITIES = {'raw_sql_write', 'schema_ddl_call', 'ddl_via_subprocess', 'bulk_replace',
                      'dbapi_direct', 'file_overwrite', 'http_write'}
ALL_CAPABILITIES = WRITE_CAPABILITIES | {'engine_from_env', 'orm_session', 'own_engine',
                                         'alembic_import'}


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
    env_written = False
    alembic = False      # `from alembic import command` / `import alembic...`
    reads_url_env = False    # 读 `DATABASE_URL` / `ALEMBIC_DATABASE_URL` 这两个名字
    builds_engine = False    # 自己 `create_engine(...)` / `sessionmaker(...)`
    builds_external_engine = False    # …且目标不是代码里写死的本地 sqlite 路径
    capabilities = set()
    alias = {}          # `from _db_guard import pin_local_sqlite as _pin_x` 也要认得出来
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ('_db_guard', 'sqlalchemy'):
            # sqlalchemy 那一支是第 43 轮 B-MAJOR-1：`from sqlalchemy import create_engine as ce`
            # 一个别名就让 `engine_from_env` 变 False（上一版只对 `_db_guard` 的名字做还原）。
            for a in node.names:
                alias[a.asname or a.name] = a.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, 'id', None) or getattr(func, 'attr', None)
            resolved = alias.get(name or '', name or '')
            if resolved:
                called.add(resolved)
                if resolved in ('create_engine', 'sessionmaker'):
                    builds_engine = True
                    if not _target_is_hardcoded_local(node):
                        builds_external_engine = True       # 含 `create_engine(settings.database_url)`
                        capabilities.add('engine_from_env')
            if name == 'add_argument':
                flags.update(a.value for a in node.args
                             if isinstance(a, ast.Constant) and isinstance(a.value, str))
            strs = [c.value for a in node.args for c in ast.walk(a)
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            # 参数里的字符串要**走进去找**：`subprocess.run([sys.executable, 'scripts/run_migrations.py'])`
            # 的第二个参数是 List 而不是常量，只看直接常量就会漏掉"借道子进程发 DDL"这一类。
            if resolved == 'create_all':
                capabilities.add('schema_ddl_call')          # `Base.metadata.create_all(engine)`
            if resolved in ('execute', 'exec_driver_sql', 'text') \
                    and any(DML_WORDS.search(s) for s in strs):
                capabilities.add('raw_sql_write')            # 裸 SQL 写：串里有 DML/DDL 动词
            if resolved == 'to_sql':
                capabilities.add('bulk_replace')             # df.to_sql(if_exists='replace')
            if resolved in DBAPI_MODULES and name == 'connect':
                capabilities.add('dbapi_direct')             # sqlite3.connect / psycopg2.connect
            if resolved in FILE_WRITERS or (resolved == 'open'
                                            and any('w' in s or 'a' in s for s in strs)):
                if any('.db' in s or '.env' in s for s in strs):
                    capabilities.add('file_overwrite')       # 覆盖 db 文件 / 重写 .env
            if resolved in PROC_WRAPPERS and any(
                    re.search(r'\balembic\b|run_migrations|sync_db_columns', s) for s in strs):
                capabilities.add('ddl_via_subprocess')       # 借道自己人发 DDL
            if resolved in GENERIC_HTTP and any(s.upper() in HTTP_METHODS for s in strs):
                capabilities.add('http_write')               # requests.request('DELETE', …)
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
            for al in node.names:
                if (al.name or '').startswith('src.models.database'):
                    direct_db = True
                if (al.name or '').split('.')[0] == 'alembic' or mod.split('.')[0] == 'alembic':
                    alembic = True
        elif isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Subscript):
            t = node.targets[0]
            if isinstance(t.value, ast.Attribute) and t.value.attr == 'environ':
                key = t.slice
                if isinstance(key, ast.Constant) and key.value in ('DATABASE_URL', 'LOCAL_DB_URL'):
                    env_written = True
    # "见某种目标就拒跑"只有在**条件分支里**才算数：入口那句 `raise SystemExit(main())`
    # 每个脚本都有，把它当护栏 = 一句护栏都没写也判"有守卫"（第 43 轮 A-MAJOR-1）。
    # 判据的形状是"**这个分支里既印了 `[abort]`，又停下来了**"——停下来可以是 raise，
    # 也可以是 `return 4`（`sync_db_columns.py` 用的就是后一种，A 席指出上一版只认前一种
    # 会把一个真有护栏的脚本判成没护栏，那是误报方向，一样要修）。
    refusals = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.ExceptHandler, ast.For, ast.While)):
            continue
        subs = list(ast.walk(node))

        def _says_no(s):
            """这个子节点是不是"把拒绝的原因说出来"：`print('[abort] …' % x)` 的第一参数
            常常是 BinOp/JoinedStr 而不是常量（`purge_test_rows_from_prod.py:57` 就是），
            所以要看**这个调用里**有没有任何以 `[abort]` 开头的字符串常量。"""
            if not isinstance(s, (ast.Call, ast.Raise)):
                return False
            return any(isinstance(c, ast.Constant) and isinstance(c.value, str)
                       and c.value.startswith(('[abort]', '[拒')) for c in ast.walk(s))

        def _is_print_or_raise(s):
            if isinstance(s, ast.Raise):
                return True
            return (isinstance(s, ast.Call)
                    and (getattr(s.func, 'id', None) or getattr(s.func, 'attr', None)) == 'print')
        says_abort = any(_is_print_or_raise(s) and _says_no(s) for s in subs)
        stops = any((isinstance(s, ast.Raise) and isinstance(s.exc, ast.Call))
                    or (isinstance(s, ast.Return) and isinstance(s.value, ast.Constant)
                        and isinstance(s.value.value, int) and s.value.value != 0)
                    for s in subs)
        if says_abort and stops:
            refusals += 1
    if direct_db:
        capabilities.add('orm_session')
    if builds_engine or builds_external_engine:
        capabilities.add('own_engine')
    if alembic:
        capabilities.add('alembic_import')
    return {'text': text, 'called': called, 'flags': flags, 'raised': raised,
            'refusals': refusals, 'consts': consts,
            'env_written': env_written, 'direct_db': direct_db,
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
                            'refusals': 9, 'consts': set(),
                            'env_written': False, 'direct_db': True, 'alembic': False,
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


def _refuses_something(f):
    """"会主动拒跑"必须是**条件分支里**的 raise（第 43 轮 A-MAJOR-1）。

    以前判的是 `'SystemExit' in f['raised']`，而 `raised` 是全文件收集 ——
    仓库里几乎每个脚本末尾都有那句入口样板 `raise SystemExit(main())`，
    于是"一句护栏都没写"的脚本也被判成立（A 席用三个样品复现）。
    """
    return f.get('refusals', 0) > 0


# 两种拼法都认（第 43 轮 A-MINOR-6：`[目标]` 与 `[target]` 一边管一条识别器，
# 照着 `push_…` 的中文写法新加一个只读预检工具会被莫名判"没走门"）。
TARGET_WORDS = ('[目标]', '[target]', '[TARGET]', '[Target]')


def _says_target(f):
    return any(str(c).startswith(w) for c in f['consts'] for w in TARGET_WORDS)


def _refuses_remote_without_a_flag(f):
    """`sync_db_columns.py` 那一类：不 pin，但**默认见到远程就拒跑**，要远程必须显式加旗。

    这一条要的是真代码：常量里出现 `postgres` _scheme 判断 + **条件分支里**主动 raise，
    写在注释或 docstring 里不算（`_docstring_consts` 把说明文整段剔掉了）。
    """
    return (any(c.startswith('postgres') for c in f['consts'])
            and _refuses_something(f)
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
    return (any(c.startswith('sqlite') for c in f['consts'])
            and _refuses_something(f)
            and any('--production' in fl for fl in f['flags']))


def _declares_http_target(f):
    """走 HTTP 写生产的脚本（`push_sector_mappings_to_prod.py` 那一族）没有 ORM 会话，
    `database_label` 对它没意义 —— 那它必须自己打一行 `[目标] …` 说清往哪台机器 POST。
    要的是**代码里的字面量**（`_facts.consts` 只收 AST 常量），写在注释/docstring 里不算。"""
    return _says_target(f)


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
    if _refuses_remote_without_a_flag(f) or _refuses_local_without_a_flag(f):
        return True
    consts = f['consts']
    return ('postgresql_readonly' in consts
            and _says_target(f)
            and _refuses_something(f))


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


def test_renaming_the_database_url_env_is_not_a_guard():
    """`os.environ["DATABASE_URL"] = 别的库` 不能算"有守卫"，否则方向反了的脚本照样过关。

    起因：`run_migrations.py` 把 `ALEMBIC_DATABASE_URL` 赋进 `DATABASE_URL` —— 那是**指向生产**
    的赋值，旧 `_guarded` 只看"有没有对 DATABASE_URL 赋值"，于是把发 DDL 的脚本判成已声明。
    这条把"赋过值但没有真守卫"的形状自己钉住：把 run_migrations 的自报行删掉，它必须响。
    """
    scripts = _scripts()
    f = scripts.get('run_migrations.py')
    assert f is not None, 'run_migrations.py 不在了（它仍被 render.yaml 的 startCommand 每次启动跑一遍）'
    assert f['env_written'] and _issues_schema_ddl(f), \
        '前提变了：它不再"赋 DATABASE_URL"或不再发 DDL ⇒ 这条用例失去意义，改判据而不是留着空判'
    assert _write_capable(f)
    assert _guarded('run_migrations.py', f, schema_ddl=True), \
        'run_migrations.py 现在又只靠"赋值 DATABASE_URL"过关 ⇒ 发 DDL 的脚本必须钉镜像/自报库名/见远程拒跑'


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


def _touches_a_database(f):
    """读侧那道闸的**触发条件**：只要"碰到过一个活的数据库连接"就得问连哪儿。

    第 43 轮 B-MAJOR-2：上一版触发只看 `engine_from_env`（自建 engine 那一族），
    于是"函数体里 `from src.models.database import SessionLocal` + `db.query(...)`、
    一句不写"的脚本三道判据一条都不响 —— 而 `.env` 指生产 ⇒ 它默认就在读线上库。
    这一族在本仓不是假想：`direct_db` 为真的脚本有 30 个，其中 9 个既不受写侧管
    也不被旧读侧触发命中（今天它们各自走了门或被 `env_written` 覆盖，所以是**下一个脚本**的洞）。
    """
    return bool(f['engine_from_env'] or f['direct_db'])


def test_read_side_scripts_also_say_which_database_they_read():
    """只读脚本也要答"连的是哪个库"（第 41 轮 B-MAJOR-1：读侧不是攻击面 = 这整族的根因）。

    三个 L3/L1 分析脚本以前都写着 `create_engine(os.getenv("DATABASE_URL"))` —— 本地 `.env`
    里那条就是生产 Supabase，于是"跑一下估算"默认读线上，还把结果连同一个只写着
    `"DATABASE_URL"` 的标签落进 `docs/`。守卫扫描只看"能不能写"，所以它们一路绿灯。
    """
    naked = [name for name, f in _scripts().items()
             if _touches_a_database(f) and not _read_side_guarded(name, f)
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


def _fallback_keys():
    """`_scripts()` 里那个"解析失败兜底 dict"的键集合（AST 里抠出来，不手抄）。"""
    import inspect
    src = inspect.getsource(_scripts)
    start = src.index("'text': py.read_text")
    body = src[start - 20:src.index('return out', start)]
    return set(re.findall(r"'([a-z_]+)':", body))


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


def _src_import_graph():
    """src 包的**顶层** import 图：模块名 → 它 import 时就会执行的模块集合。

    `SCRIPTS` 已经指向 `<repo>/scripts`，所以仓库根是它的 `.parent` 一层
    （上一版我写成 `.parent.parent`，图直接空了 ⇒ 这条判据本来会**恒真**，
    是下面那对控制断言把它抓出来的）。
    """
    root = SCRIPTS.parent
    src = root / 'src'
    graph = {}
    for py in sorted(src.rglob('*.py')):
        mod = py.relative_to(root).with_suffix('').as_posix()
        mod = mod.replace('/__init__', '').strip('/').replace('/', '.')
        try:
            tree = ast.parse(py.read_text(encoding='utf-8', errors='replace'), filename=str(py))
        except SyntaxError:
            graph[mod] = set()
            continue
        out = set()
        for node in tree.body:        # 只看顶层：函数体里的 import 在调用时才跑
            if isinstance(node, ast.ImportFrom):
                base = (node.module or '')
                out.add(base)
                for a in node.names:
                    out.add((base + '.' + a.name) if base else a.name)
            elif isinstance(node, ast.Import):
                out.update(a.name for a in node.names)
        graph[mod] = {m for m in out if m}
    return graph


def _reaches_orm(module, graph):
    """`import module` 会不会**顺带**把 `src.models.database` 拉起来（建全局 engine）。"""
    seen, stack = set(), [module]
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
    assert not _reaches_orm('src.utils.mutation_lock', graph), \
        'mutation_lock 被判"会拉起 ORM" ⇒ 可达性太宽，会把无辜脚本一起拦下'

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
