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
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'

WRITE_SWITCH = re.compile(r'--(apply|execute|hard-delete|import)\b')
HARD_DELETE = re.compile(r'CONFIRM_TOKEN|ThreeBucketRetentionService|three-buckets-hard-delete')
PROD_FLAG = re.compile(r'--against-production')


def _facts(py):
    """从 AST 里取"代码真正做了什么"，不是"文件里出现过哪些字"。"""
    text = py.read_text(encoding='utf-8', errors='replace')
    tree = ast.parse(text, filename=str(py))
    called, flags, raised, consts, direct_db = set(), set(), set(), set(), False
    env_written = False
    alias = {}          # `from _db_guard import pin_local_sqlite as _pin_x` 也要认得出来
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == '_db_guard':
            for a in node.names:
                alias[a.asname or a.name] = a.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, 'id', None) or getattr(func, 'attr', None)
            if name:
                called.add(alias.get(name, name))
            if name == 'add_argument':
                flags.update(a.value for a in node.args
                             if isinstance(a, ast.Constant) and isinstance(a.value, str))
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            exc = node.exc.func
            raised.add(getattr(exc, 'id', None) or getattr(exc, 'name', '') or '')
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            consts.add(node.value)
        elif isinstance(node, (ast.ImportFrom, ast.Import)):
            mod = getattr(node, 'module', None) or ''
            if mod.startswith('src.models'):
                direct_db = True
            for al in node.names:
                if (al.name or '').startswith('src.models.database'):
                    direct_db = True
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
            t = node.targets[0]
            if isinstance(t.value, ast.Attribute) and t.value.attr == 'environ':
                key = t.slice
                if isinstance(key, ast.Constant) and key.value in ('DATABASE_URL', 'LOCAL_DB_URL'):
                    env_written = True
    return {'text': text, 'called': called, 'flags': flags, 'raised': raised,
            'consts': consts, 'env_written': env_written, 'direct_db': direct_db}


def _scripts():
    out = {}
    for py in sorted(SCRIPTS.glob('*.py')):
        if py.name.startswith('_'):
            continue
        try:
            out[py.name] = _facts(py)
        except SyntaxError:
            # 解析不了的文件不能悄悄放过：标成"未受管"，让下面三条用例为它变红
            out[py.name] = {'text': py.read_text(encoding='utf-8', errors='replace'),
                            'called': set(), 'flags': set(), 'env_written': False,
                            'direct_db': True, 'broken': True}
    return out


def _write_capable(f):
    """CLI 上有写开关 / 确认口令 / 直接执行删除的服务 —— 都算"能改数据"。"""
    if any(WRITE_SWITCH.search(fl) for fl in f['flags']):
        return True
    if '--confirm' in f['flags'] or f['called'] & {'confirm', 'execute'}:
        return bool(HARD_DELETE.search(f['text']))
    return bool(HARD_DELETE.search(f['text']) and f['direct_db'])


def _refuses_remote_without_a_flag(f):
    """`sync_db_columns.py` 那一类：不 pin，但**默认见到远程就拒跑**，要远程必须显式加旗。

    这一条要的是真代码：常量里出现 `postgres` _scheme 判断 + 主动 `raise SystemExit`，
    写在注释或 docstring 里不算（`_facts` 只收 AST 节点）。
    """
    return (any(c.startswith('postgres') for c in f['consts'])
            and 'SystemExit' in f['raised']
            and any(PROD_FLAG.search(fl) for fl in f['flags']))


# Render 上跑的**生产入口**：它就该连生产，护栏是"把库名印进日志"，不是钉镜像。
PRODUCTION_ENTRY = {'run_scheduled_tasks.py'}


def _guarded(name, f):
    if name in PRODUCTION_ENTRY:
        return 'database_label' in f['called']
    return ('pin_local_sqlite' in f['called'] or f['env_written']
            or 'database_label' in f['called'] or _refuses_remote_without_a_flag(f))


def test_there_are_write_capable_scripts_left_to_guard():
    """用例不能变成空判：受管脚本的数量必须>0，否则这条扫描已经失效。"""
    n = sum(1 for f in _scripts().values() if _write_capable(f))
    assert n >= 5, '只找到 %d 个受管脚本 ⇒ 先确认这条扫描还有效，再放行' % n


def test_write_capable_scripts_declare_their_database_in_code():
    bad = [name for name, f in _scripts().items()
           if f['direct_db'] and _write_capable(f) and not _guarded(name, f)]
    assert not bad, ('这些脚本能改数据、又直连 ORM，却没在代码里说清算哪个库'
                     '（.env 默认指向生产）：%s' % '、'.join(bad))


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
        if not ('pin_local_sqlite' in f['called'] or f['env_written']
                or _refuses_remote_without_a_flag(f)):
            bad.append(name + ('（删除发生在 service 里，扫 .delete( 扫不到）'
                              if not direct else ''))
    assert not bad, '这些会物理删数据的脚本默认连的不是本地镜像：%s' % '、'.join(bad)
