# -*- coding: utf-8 -*-
"""前端变异体检与 pytest 的互斥闸（`src/utils/mutation_lock.py`）。

为什么要有：`scripts/mutation_proof_frontend.py` 会就地改写 `web/index.html` 再跑 pytest。
第 30 轮 B 实测并发跑会假报 12 条 GREEN + 3 条锚点失配，而当时"不能并发"这句话
**只写在体检的 docstring 里**（第 32 轮 B 抓到：没有代码拦着的承诺等于没有承诺）。
这里把两头都钉住：体检抢不到锁要走开、别的 pytest 会话看到锁要拒绝。
"""
import ast
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.utils import mutation_lock

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_a_second_holder_is_refused_and_the_release_is_automatic(tmp_path):
    """锁必须真的互斥，且持有者退出后自己放开（否则一次崩溃就把仓库永久锁死）。"""
    with mutation_lock.held_exclusively(tmp_path):
        assert mutation_lock.is_being_mutated(tmp_path) is True
        with pytest.raises(RuntimeError) as exc:
            with mutation_lock.held_exclusively(tmp_path):
                pass
        assert '等它跑完' in str(exc.value)
    assert mutation_lock.is_being_mutated(tmp_path) is False


def test_the_lock_file_is_not_tracked_by_git(tmp_path):
    """锁落在仓库根目录：不 gitignore 的话每次体检都会往工作树里塞一个脏文件。"""
    ignore = (PROJECT_ROOT / '.gitignore').read_text(encoding='utf-8')
    assert mutation_lock.LOCK_NAME in ignore, '锁文件没进 .gitignore'


def _child_pytest_env_audit(src):
    """`(起了几次子 pytest, 其中带 env= 的次数)` —— 按 AST 数，不按"文件里出现过 `env=env`"。

    第 55 轮 M-3：逻辑侧体检把放行标记收进了 `_child_env()`，调用点变成 `env=_child_env()`
    ⇒ 老判据按字面量 `'env=env'` 找，把一份**真的在传标记**的工具判成"会把自己拦死"，
    三条端到端用例连带一起红（那三条起的子会话正是本文件那条判据）。
    说明文买不到信号，反过来也一样：**换了拼法不该判红**。
    """
    tree = ast.parse(src)

    def _marked_names(node):
        """这个节点里被写进 `x[mutation_lock.ENV_PID] = …` 的那些字典名。"""
        out = set()
        for n in ast.walk(node):
            targets = ([n.target] if isinstance(n, ast.AugAssign)
                       else list(n.targets) if isinstance(n, ast.Assign) else [])
            for t in targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                        and isinstance(t.slice, ast.Attribute) and t.slice.attr == 'ENV_PID':
                    out.add(t.value.id)
        return out

    holders = _marked_names(tree)
    # 标记收在 helper 里、调用点写 `env=_child_env()` ⇒ 只要那个函数自己往字典里塞过标记就算数
    marked_funcs = {fn.name for fn in ast.walk(tree)
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _marked_names(fn)}
    total = wired = 0
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        names = [c.value for a in n.args for c in ([a] if isinstance(a, ast.Constant)
                                                   else (a.elts if isinstance(a, (ast.List, ast.Tuple)) else []))
                 if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        if '-m' not in names or 'pytest' not in names:
            continue
        total += 1
        env_kw = next((k for k in n.keywords if k.arg == 'env'), None)
        if env_kw is None:
            continue
        value = env_kw.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id in marked_funcs:
            wired += 1
            continue
        passed = [e.id for e in ast.walk(value) if isinstance(e, ast.Name)]
        passed += [e.value.id for e in ast.walk(value)
                   if isinstance(e, ast.Subscript) and isinstance(e.value, ast.Name)]
        if set(passed) & holders:
            wired += 1
    return total, wired


def test_both_sides_are_actually_wired():
    """两头都得真的接线：只留一个工具函数不叫闸。

    第 54 轮 B-10：`scripts/mutation_proof_lifecycle.py` 这批新加时**一处锁都没接**
    （它会就地改写 `src/*.py` 再跑 pytest），而这条判据以前只认前端那一份 ⇒
    "体检与 pytest 双向互斥"这句话对逻辑侧那份工具当场不成立。
    现在按名单逐个核，并且新造一个"会改文件又跑 pytest"的工具必须被认出来。
    """
    harnesses = ('mutation_proof_frontend.py', 'mutation_proof_lifecycle.py')

    def _is_a_harness(src):
        """会就地改写仓库文件、又自己起 pytest 的脚本 —— 这种必须握两把锁。

        落盘有两种拼法：前端那份按 `Path.write_text` 写、逻辑那份为防"写一半就崩"
        用"临时文件 + `os.replace`"，两种都得认（第一版只认后者，把前端那份判成"不是体检工具"）。
        """
        writes_back = 'write_text(' in src or 'os.replace(' in src
        runs_pytest = "-m', 'pytest'" in src or '"-m", "pytest"' in src
        return writes_back and runs_pytest

    for name in harnesses:
        harness = (PROJECT_ROOT / 'scripts' / name).read_text(encoding='utf-8')
        assert _is_a_harness(harness), \
            '%s 已经不改文件/不起 pytest 了 ⇒ 把它从名单里删掉，别留着当装饰' % name
        assert 'held_exclusively(' in harness, '%s 没抢锁就能开始改写文件' % name
        assert 'harness_may_start(' in harness, \
            '%s 不先问有没有 pytest 会话在跑 ⇒ 它会改写别人正在读的那些文件' % name
        # 放行标记要真的传进子进程：只准备一个 `env[ENV_PID]` 字典而不递给那一次调用是死的
        assert 'env[mutation_lock.ENV_PID]' in harness, '%s 没给自己起的子 pytest 准备放行标记' % name
        children, carried = _child_pytest_env_audit(harness)
        assert children > 0 and carried == children, \
            '%s 起了 %d 次子 pytest、只有 %d 次带上了放行标记 ⇒ 剩下的会把自己拦死' \
            % (name, children, carried)
    conftest = (PROJECT_ROOT / 'tests' / 'conftest.py').read_text(encoding='utf-8')
    assert 'is_being_mutated(' in conftest, 'pytest 看到锁被持有时不会拦'

    # 控制断言（第 55 轮 M-3）：认的是"递出去的那个 env 里真有标记"，不是某一句字面量
    marked = ("env = dict(os.environ)\n"
              "env[mutation_lock.ENV_PID] = str(os.getpid())\n"
              "subprocess.run([sys.executable, '-m', 'pytest', 'tests'], env=env)\n")
    via_helper = ("def _child_env():\n"
                  "    env = dict(os.environ)\n"
                  "    env[mutation_lock.ENV_PID] = str(os.getpid())\n"
                  "    return env\n"
                  "subprocess.run([sys.executable, '-m', 'pytest', 'tests'], env=_child_env())\n")
    helper_without_mark = via_helper.replace(
        "    env[mutation_lock.ENV_PID] = str(os.getpid())\n", "")
    assert _child_pytest_env_audit(marked) == (1, 1), '最直白的写法都不认 ⇒ 上面那条循环是空判'
    assert _child_pytest_env_audit(via_helper) == (1, 1), \
        '标记收进 helper、调用点只写 `env=_child_env()` 就不认 ⇒ 这条判据在一句字面量上找形状'
    assert _child_pytest_env_audit(helper_without_mark) == (1, 0), \
        'helper 里把标记那行摘掉也算接上 ⇒ 它验的是"有个 env 参数"，不是"带着标记"'
    assert _child_pytest_env_audit(
        marked.replace('env=env', 'env={"PATH": ""}')) == (1, 0), \
        '递一个不含标记的字典也算接上 ⇒ 摘掉标记它不会红'
    assert _child_pytest_env_audit(
        "subprocess.run([sys.executable, '-m', 'pytest', 'tests'])\n") == (1, 0), \
        '干脆不传 env 也算接上 ⇒ 同上'

    # 控制：识别本身要有牙 —— 现造一份"改文件 + 起 pytest"的脚本必须被认成体检工具，
    # 于是名单外多一个这样的脚本就会被下面这条循环点红。
    assert _is_a_harness("import os, subprocess\n"
                         "os.replace('a.tmp', 'src/x.py')\n"
                         "subprocess.run([sys.executable, '-m', 'pytest', 'tests'])\n"), \
        '这条识别是死的 ⇒ 名单核对形同虚设'

    # 形状命中、但**危害不同**的邻居：按名字登记并写清为什么不算体检工具。
    # 判据是"它会不会让别人的红绿变成假红绿"，不是"它碰过文件又起过 pytest"。
    adjacent = {
        'audit_doc_claims.py':
            '`--fix` 只改写 `docs/` 与 `AGENTS.md`（pytest 不把这些当源码读），起的子会话是 '
            '`--collect-only`（收集不执行用例）⇒ 不会把谁的断言弄成假红绿。它真正的风险是'
            '**与基线并发**（那也是一个 pytest 会话），那一半由 AGENTS 的"跑基线期间不起第二个'
            '会话"这条规矩管，不是这两把锁。',
    }
    for path in sorted((PROJECT_ROOT / 'scripts').glob('*.py')):
        if path.name in harnesses:
            continue
        name, hits = path.name, _is_a_harness(path.read_text(encoding='utf-8'))
        if hits:
            assert name in adjacent, (
                '%s 会改写文件又起 pytest，却没登记进两把锁的名单 ⇒ 它会与 pytest 会话互相污染。'
                '要么真把两把锁接上，要么写清它为什么不会弄脏别人的红绿。' % name)
        else:
            # 登记了却已经不当这个形状 ⇒ 死条目，同样要响（别留成装饰）
            assert name not in adjacent, \
                '%s 已经不满足体检形状了 ⇒ 把它从邻居名单里删掉' % name


def test_a_stray_pytest_session_is_blocked_while_the_harness_holds_the_lock():
    """端到端：锁真的在仓库根上时，一条普通 pytest 命令要非零退出并说明原因。"""
    guard = mutation_lock.held_exclusively(PROJECT_ROOT)
    guard.__enter__()
    try:
        env = {k: v for k, v in os.environ.items() if k != mutation_lock.ENV_PID}
        # 子 pytest 的中文报错必须按 UTF-8 出：中文用户名的机器默认 cp936，父进程按 utf-8
        # 解码就拿到替换字符 ⇒ 下面那条"报错里要说清原因"永不成立（第 33 轮两份复评同抓，
        # 我在自己的 UTF-8 环境里跑是绿的 —— 仓里 `test_database_url_routing.py` 早写过这条）。
        env['PYTHONIOENCODING'] = 'utf-8'
        r = subprocess.run([sys.executable, '-m', 'pytest',
                            'tests/unit/test_mutation_lock.py::test_both_sides_are_actually_wired',
                            '-q', '--no-header', '-p', 'no:cacheprovider'],
                           cwd=str(PROJECT_ROOT), capture_output=True, env=env,
                           text=True, encoding='utf-8', errors='replace')
        assert r.returncode != 0, '锁被持有时普通 pytest 仍然跑完了 ⇒ 闸是假的'
        assert '变异体检正在改写' in (r.stdout + r.stderr)
    finally:
        guard.__exit__(None, None, None)


def test_a_second_session_under_the_default_locale_still_runs(tmp_path):
    """并发会话那行警告不许把整个会话打死（第 47 轮 A4）。

    上面两条端到端用例都显式给了 `PYTHONIOENCODING=utf-8` ⇒ 真正会撞的形狀
    ——**第二个会话按控制台默认 locale 起**（Windows 上就是 cp936）—— 零覆盖，
    而 A 席就是这么撞上的：`print(... '⇒' ...)` 在 `pytest_configure` 里抛
    `UnicodeEncodeError` ⇒ `INTERNALERROR`，整个会话一条结果都没有。
    那行警告的本意是"跑不动与跑不绿分得开"，它自己却成了"跑不动"。
    判据两头：不许 INTERNALERROR，**而且那行警告必须真印出来**（只判"没崩"
    可以靠把 print 删掉混过去）。
    """
    env = {k: v for k, v in os.environ.items()
           if k not in (mutation_lock.ENV_PID, 'PYTHONIOENCODING')}
    r = subprocess.run([sys.executable, '-m', 'pytest',
                        'tests/unit/test_mutation_lock.py::test_both_sides_are_actually_wired',
                        '-q', '--no-header', '-p', 'no:cacheprovider'],
                       cwd=str(PROJECT_ROOT), capture_output=True, env=env,
                       text=True, errors='replace')      # encoding=None ⇒ 与子进程同一套 locale
    out = r.stdout + r.stderr
    assert 'INTERNALERROR' not in out, '默认 locale 下的第二个会话直接崩了：%s' % out[-400:]
    assert r.returncode == 0, '第二个会话没跑完（退码 %s）：%s' % (r.returncode, out[-400:])
    assert '已经有一个 pytest 会话握着' in out, \
        '并发警告没印出来 ⇒ 要么闸没了，要么这条用例走的是能编码的那条路：%s' % out[-300:]


def test_both_directions_are_blocked(tmp_path):
    """互斥必须是双向的（第 33 轮 A-MAJOR-3 / B-MINOR-11）。

    只有"体检持锁 → pytest 让路"那一半时，先起 pytest 再起体检照样能让体检改写 `web/`。
    现在 pytest 会话自己握一把 `.pytest-session.lock`，体检启动前先问它。
    """
    holder = mutation_lock.acquire_session_lock(tmp_path)
    assert holder is not None, '第一个 pytest 会话拿不到自己的会话锁 ⇒ 锁的实现有问题'
    assert mutation_lock.session_lock_is_held(tmp_path) is True
    assert mutation_lock.harness_may_start(tmp_path) is False, '体检仍可在 pytest 跑着时启动'
    second = mutation_lock.acquire_session_lock(tmp_path)
    assert second is None, '两个 pytest 会话应该能并存（只是体检会被其中一个挡住）'
    holder.close()
    assert mutation_lock.session_lock_is_held(tmp_path) is False
    assert mutation_lock.harness_may_start(tmp_path) is True


def test_the_harness_checks_the_session_lock_before_starting():
    """闸要在**体检那一侧**真的被调用，不是只写着一个函数。"""
    harness = (PROJECT_ROOT / 'scripts' / 'mutation_proof_frontend.py').read_text(encoding='utf-8')
    assert 'harness_may_start(' in harness, '体检启动时没问"有没有 pytest 在跑"'
    assert 'return [\'pytest-session-holds-the-lock\']' in harness, '问到了却没拦住'
    conftest = (PROJECT_ROOT / 'tests' / 'conftest.py').read_text(encoding='utf-8')
    assert 'acquire_session_lock(' in conftest, 'pytest 会话没握锁 ⇒ 反向拦截是空的'


def test_the_harness_own_child_is_let_through():
    """体检自己起的子 pytest 必须放行，否则它每次跑批都会把自己拦死。"""
    guard = mutation_lock.held_exclusively(PROJECT_ROOT)
    guard.__enter__()
    env = dict(os.environ)
    env[mutation_lock.ENV_PID] = str(os.getpid())
    env['PYTHONIOENCODING'] = 'utf-8'   # 同上：断言里要比中文字串
    try:
        r = subprocess.run([sys.executable, '-m', 'pytest',
                            'tests/unit/test_mutation_lock.py::test_both_sides_are_actually_wired',
                            '-q', '--no-header', '-p', 'no:cacheprovider'],
                           cwd=str(PROJECT_ROOT), capture_output=True, env=env,
                           text=True, encoding='utf-8', errors='replace')
        assert r.returncode == 0, '体检自己的子会话被闸拦掉了：\n%s' % (r.stdout + r.stderr)[-800:]
    finally:
        guard.__exit__(None, None, None)



def _frontend_harness_source():
    return (PROJECT_ROOT / 'scripts' / 'mutation_proof_frontend.py').read_text(encoding='utf-8')

def _frontend_harness():
    """按**文件路径**把前端体检加载进来（`scripts/` 不是包，没有第二条路）。

    为什么要在用例里 import 它（第 64 轮）：这份工具自己那份"注册表里的判据名指向哪个文件"
    的逻辑原来只有 `main()` 能走到 ⇒ 我把它写错（比整串 vs 比中括号前）时，1229 条用例
    一声不响，只有真跑一次 `--only manager` 才看得见。能 import 才有这条用例。
    """
    spec = importlib.util.spec_from_file_location(
        'mutation_proof_frontend', PROJECT_ROOT / 'scripts' / 'mutation_proof_frontend.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_every_mutation_points_at_a_judge_that_lives_where_it_routes_to():
    """注册表里每一处变异指向的判据，必须**在它被路由到的那个文件里真有其人**。

    这一条存在的意义就是它本可以当场说出第 64 轮那个错：参数化 id（`test_x[case]`）写在
    注册表里、文件里的 `def` 没有中括号 ⇒ 按整串比就把两条**真在跑**的接线闸判成 JUDGE-MISS。
    """
    h = _frontend_harness()
    defs = h._judge_defs()
    assert any(n for n in defs.values()), '三个判据文件一条 `def test_` 都没数到 ⇒ 这把尺子恒空'
    assert any('[' in test for test, *_rest in h.MUTATIONS), \
        '注册表里一条参数化判据 id 都没有 ⇒ 下面这问退化成"名字在不在"，参数化那一档没人走过'
    stranded = ['%s → %s（判据 %s）' % (name, h._judge_lookup(test, defs)[0].split('/')[-1], test)
                for test, name, *_rest in h.MUTATIONS
                if not h._judge_lookup(test, defs)[1]]
    assert not stranded, '这些变异指向的判据在它该在的文件里找不到：\n  %s' % '\n  '.join(stranded)


def test_the_judge_lookup_needs_both_the_right_file_and_the_right_name():
    """路由与"这个文件里有没有"两腿**都要**成立；少任何一腿就该点名为 JUDGE-MISS。

    反面样品（不靠真文件里的名字，全用现造的映射）：别的文件里有这个人 ⇒ 不许点头；
    压根没有这个名字 ⇒ 不许点头；参数化后缀 ⇒ 必须点头（这一格就是上一批写错的那一档）。
    """
    h = _frontend_harness()
    param = 'test_every_option_the_manager_reads_is_actually_injected[createViewpointManager]'
    ok_map = {h.T: set(), h.WIRING: {param.split('[')[0]}, h.FUND: set()}
    assert h._judge_lookup(param, ok_map) == (h.WIRING, True), \
        '参数化后缀认不出 ⇒ 真在跑的接线闸会被判成 JUDGE-MISS（第 64 轮那个错）'
    assert h._judge_lookup(param, {h.T: ok_map[h.WIRING], h.WIRING: set(), h.FUND: set()}) \
        == (h.WIRING, False), '名字只在**别的**文件里有也点头 ⇒ 变异会送到错的判据上'
    assert h._judge_lookup('test_a_judge_nobody_wrote', ok_map)[1] is False, \
        '注册表里写一个不存在的判据名却仍被当成接上 ⇒ 这一问恒真'


def test_the_orm_refusal_runs_before_the_harness_touches_anything():
    """第 65 轮 MINOR-7：那句 ORM 守卫从模块顶层挪进 `main()` 之后，"它排在哪儿"这件事没有判据。

    挪回顶层 ⇒ 用例想 import 这份工具就打死（第 64 轮的根因）；挪到抢锁之后 ⇒ 它在已经改写
    `web/*.py` 第一个字节之后才拒绝，那正是它要防的事。所以判的是**先后**，不是"有没有这句"
    （与第 57~58 轮"一把闸存在过 ≠ 它拦得住"同一把尺子）。
    """
    src = _frontend_harness_source()
    body = src[src.index('def main('):]
    guard = body.index('_refuse_if_the_orm_is_already_built()')
    lock = body.index('mutation_lock.harness_may_start')
    first_write = body.index('.write_text(')
    assert guard < lock < first_write,         'ORM 守卫(第%d) / 抢锁(第%d) / 第一次改写文件(第%d) 的先后不对 ⇒ 要么它来得太晚，'         '要么这份工具压根没启动就被打死' % (guard, lock, first_write)
    # 顶层不许留那句 assert：否则这份工具连"被用例问一句"都做不到（第 64 实测过的形状）
    top = src[:src.index('def main(')]
    assert "assert 'src.models.database' not in sys.modules" not in top.split('def _refuse')[0],         '那句 assert 又回到模块顶层 ⇒ 任何用例 import 这份工具都会被当场打死'


def _drop_bytecode_fn():
    """只取体检工具里那**一个函数**来跑，不 import 整份工具。

    直接 `exec_module` 会顺着它的 import 链把锁（以及锁里那句 ORM 守卫）拉起来，
    于是"这份工具不该连库"那条既有判据会在别人身上响 —— 问的是缓存，不是启动。
    """
    path = os.path.join(os.path.dirname(__file__), '..', '..',
                        'scripts', 'mutation_proof_lifecycle.py')
    tree = ast.parse(io.open(path, encoding='utf-8').read())
    defs = [n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == '_drop_bytecode']
    assert len(defs) == 1, '那个函数在工具里应当只有一个定义'
    ns = {'os': os}
    exec(compile(ast.Module(body=defs, type_ignores=[]), path, 'exec'), ns)
    return ns['_drop_bytecode'], path


def test_rewriting_a_source_file_also_drops_its_stale_bytecode(tmp_path):
    """同字节数的载荷会骗过 mtime+size ⇒ 改完 `.py` 必须连 `.pyc` 一起放下。

    2026-09-29 实测：M48 把 `kept_answer_unknown` 换成 `kept_window_not_due` —— 两边都是
    19 个字符，**字节数一模一样**。还原后的源文件若落进同一个 mtime 刻度，CPython 就接着吃
    上一轮编译出来的 `.pyc` ⇒ 下一轮 CONTROL 在"干净代码"上量到的其实是**上一处变异**
    （那一次整轮退 4 作废）。这一条问的是**结果**：那份缓存文件真的没了。
    """
    drop, _ = _drop_bytecode_fn()
    src = tmp_path / 'mod_x.py'
    src.write_text('A = 1' + chr(10), encoding='utf-8')
    cache = tmp_path / '__pycache__'
    cache.mkdir()
    mine = cache / 'mod_x.cpython-312.pyc'
    other = cache / 'mod_y.cpython-312.pyc'
    mine.write_text('stale', encoding='utf-8')
    other.write_text('keep', encoding='utf-8')
    drop(str(src))
    assert not mine.exists(), '陈旧的那份 .pyc 还留着 ⇒ 下一次导入吃的可能就是它'
    assert other.exists(), '它把别人的缓存一起删了 ⇒ 这不是这一条要做的动作（不许顺手扩大）'
    # 没有缓存目录的那一站也不许抛（体检不该因为"没人生成过 .pyc"而中断）
    drop(str(tmp_path / 'never_imported.py'))


def _os_replace_sites_without_a_cache_drop(text):
    r"""数出"`os.replace(...)` 落在源码里、同一语句块的下一句不是 `_drop_bytecode(...)`"的行号。

    三条刻意收窄，都写进返回语义：
    ① **只看 `os.replace`**（`Name('os')` + `attr == 'replace'`）—— 不相干的 `buf.replace(a, b)`
       不许被点名（第一版只比 `attr`，那一格是潜在误报）；
    ② **不限语句位置**：赋值右侧、`return` 的值、`except` 支里、嵌套 `def` 里都要看见
       （第一版要求"整句正好是一次调用"且不钻 handlers ⇒ 换个写法就隐身，第 69 轮 MINOR-5）；
    ③ 判的是**同一语句块的下一句**，不是"文件里出现过这个函数"。

    边界（要说白，别当已封）：这把尺子管不到**别的改写写法** —— `shutil.copyfile`、
    `Path.write_text`、`open(p, 'w')` 直接覆盖源文件都不在它眼里。今天这份工具里没有那种形状
    （复核：`grep -n "copyfile\|write_text\|open(" scripts/mutation_proof_lifecycle.py` 只命中
    `io.open(backup…)` / `io.open(tmp…)` 两处，写的都不是 `.py` 本体，紧随其后那次 `os.replace`
    才把内容放进源文件，而那两处后面都接了 `_drop_bytecode`）。
    谁把还原改成别的写法，这条就退回"看不见" —— 由这一段注释负责被下一轮想起来。
    """
    tree = ast.parse(text)
    missing = []

    def is_drop(node):
        return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, 'id', None) == '_drop_bytecode')

    def calls_os_replace(node):
        """本语句**自己**那些表达式里的 `os.replace`；子语句（if / for / try / def 的体）不算。

        子语句由它们自己的语句块去判 —— 那里"下一句"才是它真正的下一句。
        第一版让外层扫描一路钻进 `Try` / `For` 的体，结果把三处**明明接了**的站点
        报成没接（过宽的闸，下一轮就会被整条关掉）。
        """
        found = []

        def scan(cur):
            if isinstance(cur, (ast.stmt, ast.ExceptHandler)) and cur is not node:
                return        # 子语句：交给它自己的语句块
            if (isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute)
                    and cur.func.attr == 'replace'
                    and isinstance(cur.func.value, ast.Name) and cur.func.value.id == 'os'):
                found.append(cur.lineno)
            for _, value in ast.iter_fields(cur):
                if isinstance(value, ast.AST):
                    scan(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, ast.AST):
                            scan(item)

        scan(node)
        return found

    def sub_suites(node):
        out = []
        for _, value in ast.iter_fields(node):
            if not isinstance(value, list):
                continue
            if value and all(isinstance(v, ast.stmt) for v in value):
                out.append(value)
            for item in value:
                if isinstance(item, ast.ExceptHandler):
                    out.append(item.body)
        return out

    def walk(suite):
        for i, node in enumerate(suite):
            for line in calls_os_replace(node):
                nxt = suite[i + 1] if i + 1 < len(suite) else None
                if not is_drop(nxt):
                    missing.append(line)
            for block in sub_suites(node):
                walk(block)

    walk(tree.body)
    return sorted(set(missing))


def test_no_write_site_forgets_to_drop_the_bytecode():
    """每一处 `os.replace` 后面必须紧跟一次 `_drop_bytecode` —— 少一个方向就有一半假账。

    陈旧缓存两个方向都坏：**落载荷时不清** ⇒ 变异失效（假 GREEN，体检反而满分通过）；
    **还原时不清** ⇒ 变异残留（假 RED，像 2026-09-29 那次整轮作废，退 4）。
    所以判的是"每一处都接了"，不是"有没有这个函数"。
    """
    path = os.path.join(os.path.dirname(__file__), '..', '..',
                        'scripts', 'mutation_proof_lifecycle.py')
    assert not _os_replace_sites_without_a_cache_drop(
        io.open(path, encoding='utf-8').read()), \
        '有改写源码的站点后面没接 `_drop_bytecode`'

    # 控制断言（第 69 轮 MINOR-5：上一版这把尺子对四种改写位置全部隐身、对一种正常写法误伤）
    shapes = {
        '裸语句接了': "os.replace('a', 'b')\n_drop_bytecode('b')\n",
        '赋值右侧': "def f():\n    d = os.replace('a', 'b')\n    return d\n",
        'return 里': "def f():\n    return os.replace('a', 'b')\n",
        'except 支里': "def f():\n    try:\n        g()\n    except OSError:\n"
                      "        os.replace('a', 'b')\n    h()\n",
        '嵌套 def 里': "def f():\n    def inner():\n        os.replace('a', 'b')\n"
                       "    return inner\n",
        'except 支里接了': "def f():\n    try:\n        g()\n    except OSError:\n"
                          "        os.replace('a', 'b')\n        _drop_bytecode('b')\n",
        '别人的 replace': "def f(buf):\n    buf.replace('a', 'b')\n    return buf\n",
    }
    must_name = ('赋值右侧', 'return 里', 'except 支里', '嵌套 def 里')
    must_keep = ('裸语句接了', 'except 支里接了', '别人的 replace')
    for name, src in shapes.items():
        hits = _os_replace_sites_without_a_cache_drop(src)
        if name in must_name:
            assert hits, '%s 那一格的 os.replace 没被点名 ⇒ 这把尺子对它失明' % name
        else:
            assert not hits, '%s 被误伤 ⇒ 过宽的闸下一轮就会被整条关掉' % name
