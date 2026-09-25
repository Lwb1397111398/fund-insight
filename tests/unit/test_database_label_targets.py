# -*- coding: utf-8 -*-
"""`database_label()` 必须认得 Session / Engine / Connection 三种"库"的形状。

起因（第 37 轮 B 的 M-3 顺带照出来）：`scripts/run_migrations.py` 为了自报"我要往哪个库发
DDL"而调用它，传的是 `engine.connect()` 得到的 **Connection** —— SQLAlchemy 2.0 起
`Connection` 没有 `get_bind()`，函数里那个 `except Exception: return '未知库'` 把异常吞了，
于是打印出 `[库] 未知库（sqlite）`：一个看起来不像在说谎的谎。
报"哪个库"这件事的全部意义就是**不许说 Unknown**，所以这条得有用例钉着。

第 42 轮 B-(c) 补的第二半：认出"是 sqlite"不等于报出"是哪个库"。
`sqlite:///data/fund_insight.db`（真镜像）、`sqlite:///data/copy_20260920.db`（回放副本）、
`sqlite:///:memory:`（测试夹具）以前都印同一句"本地镜像库"，而拿副本的数当镜像的数
正是第 23 轮那次错的最省事的复现方式。现在标签后面必须跟着文件或主机。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.services.verdict_evidence import database_label, target_name

ROOT = Path(__file__).resolve().parents[2]

# 一把尺子的两端都要喂同一批样品：`scripts/_db_guard.machine_name()/db_kind()` 与
# `src/services/verdict_evidence.target_name()/describe_url()` 是**同一件事的两份实现**
# （src 不能 import scripts；`_db_guard` 必须在"连哪个库还没定"之前就能被 import，
# 所以它也不能 import src）。两份实现漂了没人发现 = 两条自报各说各话。
#
# 第 43 轮 A-MINOR-1 的教训：上一版这里**手抄**了 10 条样品，而样品之外当场量到分叉
# （`sqlite:////E:x.db`：src 剥了前导斜杠、守卫没剥）。所以样品不再手抄，
# 而由 scheme × 斜杠数 × 路径形状 × 是否带口令/query **笛卡尔积生成** ——
# 覆盖面不再取决于我记得哪些形状。
_SCHEMES = ['sqlite', 'sqlite+pysqlite', 'postgresql', 'postgres', 'mysql']
_SLASHES = ['://', ':///', ':////']
_PATHS = ['data/fund_insight.db', 'data/copy_20260920.db', '/home/me/f.db', '/E:/AI Agent/data/f.db',
          'E:/AI Agent/data/f.db', 'E:x.db', ':memory:', 'x.db', '']
_CREDENTIALS = ['', 'u@', 'u:S3cr3tPW@']
_QUERIES = ['', '?mode=ro', '?sslmode=require&foo=1']
# 远程那一族的**主机**形状（第 45 轮 B-m5 / B-m7）：本机回环、私网 IP、带方括号的 IPv6、
# 大写 scheme、带端口、`@` 前后带不带口令 —— 两把尺子对"这是哪一档库"的判断必须逐条一致，
# 而这套判断从 scheme 之外还要看主机，所以样品必须连主机一起笛卡尔积。
_REMOTE_HOSTS = ['', 'h', 'localhost', 'LOCALHOST', '127.0.0.1', '8.8.8.8', '::1', '[::1]',
                 '192.168.1.20', '10.0.0.7', '172.16.0.5', '172.32.0.5', '169.254.1.1',
                 'db.example.com', 'db.example.com:6543', 'u@db.example.com', 'u:p@db.example.com',
                 'x.supabase.co', 'AWS-0-X.POOLER.SUPABASE.COM', 'notsupabase.evil.example',
                 # 第 46 轮 A-m1 / B-M6：多主机列表与"主机只写在 `?host=` 里"
                 'a.supabase.co,b.backup', 'x.supabase.co.', '0.0.0.0', '[::ffff:0.0.0.2]',
                 'mysupabase.internal']


def _all_samples():
    out = ['', 'not-a-url', 'sqlite://', '://x', 'postgresql://u:p@h', 'postgres://h/db',
           # 第 44 轮 A-m6：**没有 `://` 的连接串**。libpq/pgbouncer 允许 key=value 写法
           # （`host=h port=5432 user=u password=真口令 dbname=d`），而旧尺子对它的处理是
           # "认不出 scheme 就原样返回" ⇒ 口令跟着自报行进 stdout / Render 日志 / `docs/` 报告。
           # 这几条样品就是那条漏洞的复现样本：把它们喂给修复前的尺子，
           # 下面那条 `assert 'S3cr3tPW' not in …` 必须响。
           'host=db.example.com port=5432 user=u password=S3cr3tPW dbname=proddb',
           'password=S3cr3tPW',
           'user=u;password=S3cr3tPW;host=h;dbname=d',
           'u:S3cr3tPW@h/db',
           # 盘符不是 ASCII 字母时两把尺子曾经分叉（src 用 `str.isalpha()`、守卫用 `[A-Za-z]`）
           'sqlite:////é:/data/f.db', 'sqlite:///é:/x.db', '/é:/data/f.db']
    for sch in _SCHEMES:
        for sl in _SLASHES:
            for cred in (['', 'u@'] if sch.startswith('sqlite') else _CREDENTIALS):
                for path in _PATHS:
                    for q in _QUERIES:
                        out.append('%s%s%s%s%s' % (sch, sl, cred, path, q))
    # 远程那一族单独再乘一遍**主机**：`db_kind` 从第 45 轮起要看主机（本机/生产/别的远程），
    # 只看 scheme 的旧样品覆盖不到这条新判据 —— 两把尺子必须在这些主机上也逐条相等。
    for sch in ('postgresql', 'postgres', 'PostgreSQL', 'mysql'):
        for host in _REMOTE_HOSTS:
            out.append('%s://%s/db' % (sch, host))
            out.append('%s://u:S3cr3tPW@%s:5432/db?sslmode=require' % (sch, host))
    # 主机**不在 netloc 里**的那些写法（第 46 轮 B-M6）：`postgresql:///db?host=…` 是
    # SQLAlchemy 的正规形状之一，旧尺子在这里解析出空主机 ⇒ 把真生产说成"本机"。
    for q in ('host=aws-0-x.pooler.supabase.co', 'host=x.supabase.co&port=5432',
              'host=a.supabase.co,b.backup', 'host=localhost', 'host=127.0.0.1',
              'dbname=d', 'password=S3cr3tPW'):
        out.append('postgresql:///postgres?%s' % q)
        out.append('postgres:///%s' % q.replace('=', '='))
    out.append('host=aws-0-x.pooler.supabase.co port=5432 dbname=postgres user=u password=S3cr3tPW')
    out.append('sqlite:///u:S3cr3tPW@/tmp/x.db')            # sqlite 这一支的口令（B-minor-4）
    return sorted(set(out))


TARGET_SAMPLES = _all_samples()


class _UrlOnly:
    """只想让 `url` 有值的最小对象（不用真连一个 Postgres）。"""

    def __init__(self, url):
        self.url = url


def test_an_engine_tells_which_database_it_points_at():
    """直接在 2.0 上跑过的形状：`create_engine('sqlite:///...')` 自己就是"库"。"""
    assert database_label(create_engine('sqlite:///data/fund_insight.db')) \
        == '本地镜像库（data/fund_insight.db）'
    assert database_label(create_engine('postgresql://u:p@x.supabase.co/db')) \
        == '线上生产库（postgresql://x.supabase.co/db）'


def test_a_postgres_url_is_not_automatically_the_production_database():
    """"这是 PostgreSQL"要说三档：本机 / 本项目的 Supabase 生产 / 别的远程（第 45 轮 B-m5）。

    旧写法只要 scheme 以 `postgres` 开头就印"线上生产库"。可 `postgresql://u@127.0.0.1/db`
    是本机起的一个 Postgres、`postgresql://u@10.0.0.5/db` 是内网某台 ——
    而**这句自报是操作员决定要不要按确认的依据**，把不是生产的东西说成生产，
    比报"远程库"更坏（同一族缺陷第 43 轮在 `push_sector_mappings_to_prod.py` 上已经
    修过一次：`--base http://127.0.0.1:9/` 那时也自称"线上生产库"）。
    """
    def kind(url):
        sys.path.insert(0, str(ROOT / 'scripts'))
        try:
            import _db_guard
            return _db_guard.db_kind(url)
        finally:
            sys.path.remove(str(ROOT / 'scripts'))

    assert kind('postgresql://u@127.0.0.1/db').startswith('本机 PostgreSQL'), \
        kind('postgresql://u@127.0.0.1/db')
    # 判"有没有自称生产"要看**这一句的开头是什么档**，不能看整串里出没出现过那四个字 ——
    # "本机 PostgreSQL（…，不是线上生产库）"里就含那四个字，用子串判会把对的话读成谎话
    # （我自己第一版就是这样，被这条用例的对照组当场点红）。
    for local in ('postgresql://u@localhost:5432/db', 'postgres://u@::1/db',
                  'postgresql://u@[::1]/db', 'postgresql://u@192.168.1.20/db',
                  'postgresql://u@10.0.0.7/db', 'postgresql://u@169.254.1.1/db'):
        assert kind(local).startswith('本机 PostgreSQL'), '%s 被说成了：%s' % (local, kind(local))
    assert kind('postgresql://u:p@aws-0-x.pooler.supabase.co:6543/postgres').startswith(
        '线上生产库'), kind('postgresql://u:p@aws-0-x.pooler.supabase.co:6543/postgres')
    other = kind('postgresql://u:p@db-partner.example.com/db')
    assert other.startswith('远程 PostgreSQL'), other
    # 子串买不到"生产"这一档（第 43 轮 `push_sector_mappings_to_prod.py` 的同一条课）
    for fake in ('notsupabase.evil.example', 'supabase.co.attacker.example',
                 'mysupabase.internal'):
        assert kind('postgresql://u@%s/db' % fake).startswith('远程 PostgreSQL'), \
            '%s 靠子串混成了线上生产库' % fake
    # 控制：四档必须**真的**互不相同 —— 如果实现退化成"一律远程"或"一律生产"，上面会一起响；
    # 这一条保证我没有只是把标签全删（那等于把这条闸拆了）。
    tiers = {kind('postgresql://u@127.0.0.1/db').split('（')[0],
             kind('postgresql://u:p@x.supabase.co/db').split('（')[0],
             kind('postgresql://u:p@db.example.com/db').split('（')[0],
             kind('postgresql:///var/run/postgresql/mydb').split('（')[0]}
    assert len(tiers) == 4, '几档糊在一起了：%s' % tiers
    assert kind('postgresql:///postgres?host=x.supabase.co').startswith('线上生产库'), \
        '主机只写在 query 里 ⇒ 应当仍认出生产档（它与"认不出主机"不是同一档）'


def test_a_host_written_only_in_the_query_is_never_reported_as_local():
    """主机只出现在 `?host=` 里时，旧尺子解析出**空主机**并把真生产说成"本机"（第 46 轮 B-M6）。

    `postgresql:///db?host=…` 是 SQLAlchemy 的正规写法之一（Unix socket / PgBouncer），
    而 `q.py --production` 与 `read_only_connect --production` 只看 scheme ⇒
    读的是线上、屏幕上写"本机，不是线上生产库"。
    三档都要各自钉：命中生产域 ⇒ 生产；`host=localhost` ⇒ 本机；完全认不出 ⇒ 明说认不出。
    多主机列表（`host=a.supabase.co,b.backup`，驱动自己做故障转移）里任一候选命中生产域
    就算生产（第 46 轮 A-m1：整串当一个主机名 ⇒ 真生产被说成"不是那台"）。
    """
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        import _db_guard
    finally:
        sys.path.remove(str(ROOT / 'scripts'))

    def kind(url):
        return _db_guard.db_kind(url)

    prod_in_query = 'postgresql:///postgres?host=aws-0-x.pooler.supabase.co&port=5432'
    assert kind(prod_in_query).startswith('线上生产库'), kind(prod_in_query)
    assert kind('postgresql:///postgres?host=localhost').startswith('本机 PostgreSQL')
    assert kind('postgresql:///db?host=a.supabase.co,b.backup').startswith('线上生产库'), \
        '多主机列表被整串当成一个名字 ⇒ 真生产混进"不是那台"那一档：%s' % kind(
            'postgresql:///db?host=a.supabase.co,b.backup')
    unknown = kind('postgresql:///var/run/postgresql/mydb')
    # 判"属于哪一档"要判这一句的**开头**，不能判整串里出没出现过"本机"两个字 ——
    # 这一档的尾巴正是"不敢说它是本机还是线上生产库"，用子串判会把对的话读成谎话
    # （同一条坑今天两个席位各踩一次，我自己写这条时又踩第三次）。
    assert unknown.startswith('PostgreSQL（') and '认不出主机' in unknown, unknown
    # 键值写法（没有 `://`）走同一条判据
    assert kind('host=aws-0-x.pooler.supabase.co port=5432 dbname=postgres').startswith(
        '线上生产库'), kind('host=aws-0-x.pooler.supabase.co port=5432 dbname=postgres')
    assert 'S3cr3tPW' not in kind('host=h port=5432 user=u password=S3cr3tPW dbname=d')
    # sqlite 那一支的口令（B-minor-4）
    assert 'S3cr3tPW' not in kind('sqlite:///u:S3cr3tPW@/tmp/x.db'), \
        kind('sqlite:///u:S3cr3tPW@/tmp/x.db')
    # 控制：合法文件名里带 `@` 的相对路径不许被剥坏
    assert kind('sqlite:///data/mail@copy.db').endswith('data/mail@copy.db') or \
        'data/mail@copy.db' in kind('sqlite:///data/mail@copy.db'), \
        '剥口令剥过头，把真文件名改了 ⇒ 自报错对象：%s' % kind('sqlite:///data/mail@copy.db')


def test_a_raw_connection_tells_which_database_it_points_at():
    """`with engine.connect() as conn:` 里那一行自报，传的就是这个形状。"""
    engine = create_engine('sqlite:///:memory:')
    with engine.connect() as connection:
        assert database_label(connection) == '内存 sqlite（不落盘，通常是测试夹具）'


def test_a_session_still_works():
    """原有的调用方（`span_report`、各脚本）传的是 Session，别为了修新的把旧的改坏。"""
    session = Session(bind=create_engine('sqlite:///data/fund_insight.db'))
    try:
        assert database_label(session) == '本地镜像库（data/fund_insight.db）'
    finally:
        session.close()


def test_other_and_unrecognisable_targets_are_reported_as_they_are():
    assert database_label(_UrlOnly('mysql://u:p@h/db')) == 'MySQL 库（mysql://h/db）'
    # 真认不出来的时候才许说"未知库"——而不是"认得出、但代码路径写错了"。
    assert database_label(object()) == '未知库'


def test_a_copy_is_never_reported_as_the_mirror():
    """"本地镜像库"这四个字必须**真的**落在那个文件上，副本与夹具不许共用同一个标签。

    为什么单独立一条：整个"报库名"的机制存在的唯一理由就是第 23 轮那次错
    （拿镜像的数当系统的数）。标签本身如果不区分镜像/副本/内存，它就只是把错误
    说得更自信了。
    """
    copy = database_label(create_engine('sqlite:///data/copy_20260920.db'))
    assert '不是镜像库' in copy and 'copy_20260920.db' in copy, copy
    assert not copy.startswith('本地镜像库'), copy
    assert '镜像' not in database_label(create_engine('sqlite:///:memory:'))

    # 第 43 轮 B-MINOR-1：光看后缀会认错人 —— 备份目录里那个文件**也叫** `data/fund_insight.db`。
    lookalike = database_label(_UrlOnly('sqlite:///C:/backup/2026-09/data/fund_insight.db'))
    assert '不是镜像库' in lookalike, \
        '一份放在别的目录、名字恰好也叫 data/fund_insight.db 的库被报成了镜像：%s' % lookalike
    assert not lookalike.startswith('本地镜像库'), lookalike
    # 真镜像那一支仍然要说自己是镜像（否则这条闸只是把标签全删了）
    assert database_label(create_engine('sqlite:///%s' % (ROOT / 'data' / 'fund_insight.db').as_posix())) \
        == '本地镜像库（data/fund_insight.db）'


# 手写"剥口令"的残留点（第 43 轮 A-MINOR-2）。**这张名单只许变短，不许变长**：
# 每一个条目都是 `machine_name()` 的一份手抄分身，而那个 idiom 在串里没有 `@` 时
# 会把整串原样印出来。三把要动生产的工具（`q.py` / `prod_writeback_preflight_readonly.py`
# / `purge_test_rows_from_prod.py`）与 `src/__main__.py` 本轮已经换成尺子；
# 剩下这些是**报错分支**，风险低但同样该收口 —— 收口时删条目，别新增。
HAND_ROLLED_REDACTION_ALLOWED = {
    'scripts/import_export.py',
    'scripts/serve_mirror.py',
    'scripts/sync_db_columns.py',
    'scripts/sync_sector_map_funds.py',
}


def _sep_is_at(call):
    """这个 `.split/.rsplit/.partition/.rpartition` 是不是按 `'@'` 切（位置参数或 `sep=` 都算）。"""
    import ast
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr in ('split', 'rsplit', 'partition', 'rpartition')):
        return None
    args = [a.value for a in call.args if isinstance(a, ast.Constant)]
    if args and args[0] == '@':
        return call.func.attr
    for kw in (call.keywords or []):
        if kw.arg == 'sep' and isinstance(kw.value, ast.Constant) and kw.value.value == '@':
            return call.func.attr
    return None


def _is_index(expr, value):
    """下标就是这个整数（`[-1]` 在 AST 里是 `UnaryOp(USub, Constant(1))`，不是 `Constant(-1)`）。"""
    import ast
    if isinstance(expr, ast.Constant):
        return expr.value == value
    if value < 0 and isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.USub):
        return isinstance(expr.operand, ast.Constant) and expr.operand.value == -value
    return False


def _takes_the_tail(node):
    """`X[...]` 取的是**最后一段**（= 剥掉 `user:pass@` 之后剩下的主机）吗？"""
    import ast
    if not isinstance(node, ast.Subscript):
        return False
    attr = _sep_is_at(node.value)
    if attr is None:
        return False
    sl = node.slice
    if attr in ('split', 'rsplit'):
        return _is_index(sl, -1) or (isinstance(sl, ast.Slice) and _is_index(sl.lower, -1))
    return _is_index(sl, 2) or (isinstance(sl, ast.Slice) and _is_index(sl.lower, 2))


def _finds_then_slices(node):
    """`url[url.find('@') + 1:]` —— 换了个动词、又换成切片，干的是同一件事。"""
    import ast
    if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
        return False
    low = node.slice.lower
    if not (isinstance(low, ast.BinOp) and isinstance(low.op, ast.Add)
            and isinstance(low.right, ast.Constant) and low.right.value == 1):
        return False
    head = low.left
    return (isinstance(head, ast.Call) and isinstance(head.func, ast.Attribute)
            and head.func.attr in ('find', 'rfind', 'index', 'rindex')
            and bool(head.args) and isinstance(head.args[0], ast.Constant)
            and head.args[0].value == '@')


def _rewrites_by_regex(node):
    """`re.sub(r'://[^@]*@', '://', url)` —— 用正则改写凭据也是同一件事的一份手抄。"""
    import ast
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == 're'
            and node.func.attr in ('sub', 'subn', 'split')):
        return False
    return bool(node.args) and isinstance(node.args[0], ast.Constant) \
        and isinstance(node.args[0].value, str) and '@' in node.args[0].value


def _hand_rolled_sites(root, bases=('scripts', 'src', 'alembic')):
    """找"手写剥口令"的代码位置（AST，不是文本 grep），四种写法都要认。

    `alembic/` 是第 47 轮 B-2 加进来的：`alembic/env.py` 里那把 `_redact` 是这仓库的
    **第三把**手搓尺子，而两个基目录都不含它 ⇒ 这条棘轮**结构上看不见**它，
    于是 `?password=S3cr3tPW` 这种"口令落在 query 里"的写法原样进了 `[abort]` / `[库]`
    那两行（后者走 stderr，直接落进 Render 日志）。上一轮那句"口令一个字符都不出现"
    只对钉住的两份尺子成立。
    第 44 轮 A-m7：第一版只认 `x.split('@')[-1]` 一种拼写，于是换个动词（`rsplit`）、
    换个参数名（`sep='@'`）、换成 `partition`、换成 `x[x.find('@')+1:]`、或直接用正则改写
    就**绕过了棘轮** —— 而棘轮的全部意义是"下一个手抄会被点名"。
    """
    import ast
    found = set()
    for base in bases:
        for py in sorted((root / base).rglob('*.py')):
            if py.name in ('_db_guard.py', 'verdict_evidence.py'):
                continue          # 这两份是尺子本身，剥 @ 写在它们内部
            try:
                tree = ast.parse(py.read_text(encoding='utf-8', errors='replace'))
            except SyntaxError:
                found.add(py.name + '（解析不了）')
                continue
            for node in ast.walk(tree):
                if _takes_the_tail(node) or _finds_then_slices(node) or _rewrites_by_regex(node):
                    found.add(str(py.relative_to(root)).replace('\\', '/'))
                    break
    return found


def test_no_new_hand_rolled_credential_stripping_appears(tmp_path):
    """"两处实现"漂了会有人对齐，"十处手抄"漂了没人知道 —— 所以给名单上一道棘轮。

    AGENTS.md 上一轮写的是"src 侧与守卫侧**两份** `machine_name`"，A 席当场数出至少 4 处
    手写自报（`url.split('@')[-1]`）。措辞改不准没关系，**没人盯着新增**才是问题：
    这条用例就是那只眼睛。

    判的是 **AST**，不是文件正文：第一版按文本 grep，结果我写来说明"以前这里是手写剥口令"
    的那句 docstring 自己把测试弄红了 —— 说明文不是代码（同一轮 A 席在守卫扫描那边
    抓到的正是这一族，教训现学现用）。
    """
    offenders = _hand_rolled_sites(ROOT)
    assert offenders <= HAND_ROLLED_REDACTION_ALLOWED, (
        '新增了一处手写剥口令（请改用 `machine_name()`/`target_name()`）：%s；'
        '顺手把已经换掉的条目从名单里删掉 ⇒ %s' % (
            sorted(offenders - HAND_ROLLED_REDACTION_ALLOWED),
            sorted(HAND_ROLLED_REDACTION_ALLOWED - offenders)))

    # 控制：这道棘轮自己得会响 —— 现造一个"新写了一处手抄"的目录，它必须被点名；
    # 而只在 docstring 里提这句话的文件**不许**被点名（否则规则会去误伤解释性文字）。
    (tmp_path / 'scripts').mkdir()
    variants = {
        '_x_split.py': 'def go(url):\n    return url.split("@")[-1]\n',
        # 第 44 轮 A-m7 的四条绕行写法，每一条都必须和上面那条一样被抓到
        '_x_rsplit.py': 'def go(url):\n    return url.rsplit("@", 1)[-1]\n',
        '_x_kwarg.py': 'def go(url):\n    return url.split(sep="@")[-1]\n',
        '_x_partition.py': 'def go(url):\n    return url.partition("@")[2]\n',
        '_x_find_slice.py': 'def go(url):\n    return url[url.find("@") + 1:]\n',
        '_x_regex.py': ('import re\n\ndef go(url):\n'
                        '    return re.sub(r"://[^@]*@", "://", url)\n'),
        '_x_only_prose.py': '"""以前这里写 url.split("@")[-1]，现在换成尺子了。"""\n',
        # 反向样品：取的是**凭据那一段**（不是剥口令）、或找的不是 '@' ⇒ 不该误伤
        '_x_head_not_tail.py': 'def go(url):\n    return url.split("@")[0]\n',
        '_x_other_char.py': 'def go(p):\n    return p[p.find("-") + 1:]\n',
    }
    for name, body in variants.items():
        (tmp_path / 'scripts' / name).write_text(body, encoding='utf-8')
    # `alembic/` 这一档必须**真的在扫**（第 47 轮 B-2：只在 bases 里加个目录名，
    # 而控制样品全塞在 `scripts/` 下，那"看得见 alembic"这句话还是没被验过）
    (tmp_path / 'alembic').mkdir()
    (tmp_path / 'alembic' / '_x_env_copy.py').write_text(
        'def go(url):\n    return url.split("@")[-1]\n', encoding='utf-8')
    found = _hand_rolled_sites(tmp_path)
    assert any('_x_env_copy.py' in f for f in found), \
        '写在 `alembic/` 下的手搓剥口令没被点名 ⇒ 第三把尺子还是能隐身：%s' % sorted(found)
    for name in variants:
        hit = any(name in f for f in found)
        if name.endswith('_only_prose.py') or name in ('_x_head_not_tail.py', '_x_other_char.py'):
            assert not hit, '误伤了 %s（它不是"剥口令后取主机"的写法）' % name
        else:
            assert hit, '棘轮认不出 %s ⇒ 换这种拼写就能绕过它' % name


def test_the_two_self_report_rulers_stay_identical():
    """两份 `machine_name` / 两份 `db_kind` 必须逐条给出同一个答案（漂了就红）。

    第 43 轮补的两件事：① 样品改成笛卡尔积（见 `TARGET_SAMPLES` 上面那段），
    手抄 10 条时样品外的分叉没人看见；② 不只比路径，**整句类别词**也要比 ——
    B 席量到守卫侧的 `read_only_connect()` 对任何 sqlite 文件都说"本地镜像库"，
    而 src 侧已经改口；两条自报一条尺子，比的却只是路径就等于没比。
    """
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        import _db_guard
    except Exception as exc:                              # noqa: BLE001
        # 第 45 轮 A-m3：这里以前 `pytest.skip` ⇒ 守卫文件一旦语法坏/改名/动了个顶层导入，
        # "两把尺子逐条相等"这条承诺就静默变成"这台机器没测成"。skip 只能表达"环境做不到"，
        # 而 `_db_guard` 只依赖标准库 + sqlalchemy（本文件下面已经在 import src.*），做不到
        # 不是环境的正常结局。
        raise AssertionError(
            '导入不了 `scripts/_db_guard.py` ⇒ 两把尺子的对表**没测**，不是"通过"：%s' % exc) from exc
    finally:
        sys.path.remove(str(ROOT / 'scripts'))
    from src.services.verdict_evidence import describe_url

    assert len(TARGET_SAMPLES) > 200, '样品只剩 %d 条 ⇒ 笛卡尔积生成器坏了' % len(TARGET_SAMPLES)
    for url in TARGET_SAMPLES:
        a, b = target_name(url), _db_guard.machine_name(url)
        assert a == b, '同一个连接串，src 侧报 %r、守卫侧报 %r（样品 %r）' % (a, b, url)
        c, d = describe_url(url), _db_guard.db_kind(url)
        assert c == d, '整句类别词分叉：src 侧 %r、守卫侧 %r（样品 %r）' % (c, d, url)
        assert 'S3cr3tPW' not in a + c + b + d, '自报把口令印出来了：%r' % url


def test_a_key_value_dsn_is_reported_without_its_credentials():
    """`host=… password=…` 这种没有 `://` 的写法：要**留下认得出的那半、摘掉涉密的那半**。

    为什么不只靠上面那条"不含 S3cr3tPW"：把整句改成返回 `'(未知)'` 同样能让它绿 ——
    那条闸只证明"没泄露"，不证明"还在报目标"。自报的全部意义是答"连的是哪个库"，
    所以这里两头都钉：口令不许出现，主机与库名必须出现。
    """
    dsn = 'host=db.example.com port=5432 user=u password=S3cr3tPW dbname=proddb'
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        import _db_guard
    finally:
        sys.path.remove(str(ROOT / 'scripts'))
    for name in (target_name(dsn), _db_guard.machine_name(dsn)):
        assert 'S3cr3tPW' not in name and 'user=u' not in name, name
        assert 'host=db.example.com' in name and 'dbname=proddb' in name, \
            '摘口令顺手把目标也摘了 ⇒ 自报又变成一句没有信息量的话：%r' % name
    kind = _db_guard.db_kind(dsn)
    assert 'S3cr3tPW' not in kind and 'db.example.com' in kind, kind
    # 只有口令、没有可报字段时：宁可说"隐去了"，也不许把原串回显
    alone = target_name('password=S3cr3tPW')
    assert 'S3cr3tPW' not in alone and '隐去' in alone, alone
    # 没有 `=` 又没有 `://` 的裸串（例如 `user:pw@host/db`）走"@ 之后"那一路
    naked = target_name('u:S3cr3tPW@h/db')
    assert 'S3cr3tPW' not in naked and naked.startswith('h/'), naked
