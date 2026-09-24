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


def _all_samples():
    out = ['', 'not-a-url', 'sqlite://', '://x', 'postgresql://u:p@h', 'postgres://h/db']
    for sch in _SCHEMES:
        for sl in _SLASHES:
            for cred in (['', 'u@'] if sch.startswith('sqlite') else _CREDENTIALS):
                for path in _PATHS:
                    for q in _QUERIES:
                        out.append('%s%s%s%s%s' % (sch, sl, cred, path, q))
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
    assert database_label(create_engine('postgresql://u:p@h/db')) \
        == '线上生产库（postgresql://h/db）'


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


def _hand_rolled_sites(root, bases=('scripts', 'src')):
    """扫 AST 找 `x.split('@')[-1]` 这种"手写剥口令"的代码位置（不是文本 grep）。"""
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
                if not isinstance(node, ast.Subscript):
                    continue
                v = node.value
                if not (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                        and v.func.attr == 'split' and len(v.args) == 1
                        and isinstance(v.args[0], ast.Constant)
                        and v.args[0].value == '@'):
                    continue
                sl = node.slice
                # `x.split('@')[-1]` 的 `slice` **就是那个表达式本身**（单个下标不是 `ast.Slice`
                # —— `ast.Slice` 只出现在 `[-1:]` 这种切片里），而 `-1` 在 AST 里是
                # `UnaryOp(USub, Constant(1))`，不是 `Constant(-1)`。
                # 第一版两条都猜错了 ⇒ 整条棘轮恒空集（`offenders` 永远是 `set()`），
                # 是下面"现造一处违规"的控制断言把它抓红的。
                def _is_minus_one(expr):
                    return (isinstance(expr, ast.Constant) and expr.value == -1) or (
                        isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.USub)
                        and isinstance(expr.operand, ast.Constant) and expr.operand.value == 1)
                if _is_minus_one(sl):
                    found.add(str(py.relative_to(root)).replace('\\', '/'))
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
    (tmp_path / 'scripts' / '_x_new_offender.py').write_text(
        'def go(url):\n    return url.split("@")[-1]\n', encoding='utf-8')
    (tmp_path / 'scripts' / '_x_only_prose.py').write_text(
        '"""以前这里写 url.split("@")[-1]，现在换成尺子了。"""\n', encoding='utf-8')
    found = _hand_rolled_sites(tmp_path)
    assert found == {'scripts' + ('/' if '/' in str(tmp_path) else '/') + '_x_new_offender.py'} \
        or any('_x_new_offender.py' in f for f in found), found
    assert not any('_x_only_prose' in f for f in found), found


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
        pytest.skip('这台机器导入不了 `_db_guard`：%s' % exc)
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
