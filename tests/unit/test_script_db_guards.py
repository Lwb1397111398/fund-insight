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
HTTP_WRITE = {'post', 'put', 'patch'}     # 会话/requests 的写方法（ORM 侧没有这三个名字）
OS_SHELL = {'system', 'popen', 'execv', 'execve', 'spawn', 'spawnl'}


def _facts(py):
    """从 AST 里取"代码真正做了什么"，不是"文件里出现过哪些字"。"""
    text = py.read_text(encoding='utf-8', errors='replace')
    tree = ast.parse(text, filename=str(py))
    called, flags, raised, consts, direct_db = set(), set(), set(), set(), False
    env_written = False
    alembic = False      # `from alembic import command` / `import alembic...`
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
                if (al.name or '').split('.')[0] == 'alembic' or mod.split('.')[0] == 'alembic':
                    alembic = True
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Subscript):
            t = node.targets[0]
            if isinstance(t.value, ast.Attribute) and t.value.attr == 'environ':
                key = t.slice
                if isinstance(key, ast.Constant) and key.value in ('DATABASE_URL', 'LOCAL_DB_URL'):
                    env_written = True
    return {'text': text, 'called': called, 'flags': flags, 'raised': raised,
            'consts': consts, 'env_written': env_written, 'direct_db': direct_db,
            'alembic': alembic}


def _scripts():
    out = {}
    for py in sorted(SCRIPTS.glob('*.py')):
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
                            'called': set(), 'flags': set(), 'raised': set(), 'consts': set(),
                            'env_written': False, 'direct_db': True, 'alembic': False,
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
    return bool(via_api or via_cli)


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
    if f['direct_db'] and f['called'] & {'commit', 'add', 'delete'}:
        # 第 36 轮 B-MINOR-3：**什么写开关都没有、上来就 commit** 的脚本以前落在扫描集合外
        # （`scripts/seed_sector_mappings.py` 当时就是这个形状）。
        # "有没有开关"不该是"受不受管"的前提：会写库就得说清连的是哪个库。
        # 这条必须排在 `--confirm` 那个分支**前面**：加完它才发现旧顺序会短路
        # （`import_export.py` 带 `--confirm` 却没有硬删 ⇒ 老早退直接判"不受管"）。
        return True
    if f['called'] & HTTP_WRITE:
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

# **只**面向生产的修复脚本（它要清的就是线上数据，"默认钉镜像"对它没意义）。
# 护栏必须反过来：见 SQLite 就拒跑 + 必须显式 `--production`。名字写错（文件不存在）
# 会被下面那条反空判用例当场抓住，所以这张名单不是绕过闸门的后门。
PRODUCTION_ONLY = {'purge_test_rows_from_prod.py'}


def _refuses_local_without_a_flag(f):
    """与 `_refuses_remote_without_a_flag` 对称：常量里判 `sqlite` + 主动 `raise SystemExit`
    + CLI 上有 `--production` 旗子，三者齐了才算"靶子声明清楚了"。"""
    return (any(c.startswith('sqlite') for c in f['consts'])
            and 'SystemExit' in f['raised']
            and any('--production' in fl for fl in f['flags']))


def _declares_http_target(f):
    """走 HTTP 写生产的脚本（`push_sector_mappings_to_prod.py` 那一族）没有 ORM 会话，
    `database_label` 对它没意义 —— 那它必须自己打一行 `[目标] …` 说清往哪台机器 POST。
    要的是**代码里的字面量**（`_facts.consts` 只收 AST 常量），写在注释/docstring 里不算。"""
    return any(isinstance(c, str) and c.startswith('[目标]') for c in f['consts'])


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


def test_underscore_prefixed_scripts_are_not_a_whole_family_exemption():
    """以前 `startswith('_')` 让 7 个 `_tmp_*.py` 整族不进扫描 ⇒ 新写一个 `_x.py` 的生产写脚本
    可以永远没人管（第 39 轮 B）。现在豁免名单只有 `_db_guard.py` 一个文件，且它被别处引用。"""
    names = set(_scripts())
    assert '_db_guard.py' not in names
    on_disk = {p.name for p in SCRIPTS.glob('*.py')} - {'_db_guard.py'}
    assert names == on_disk, '扫描集合不等于目录清单：%s 被悄悄跳过了' % sorted(on_disk - names)
    assert any(n.startswith('_tmp_') for n in names), '_tmp_* 一个都没扫到 ⇒ 前缀豁免还在'


def test_a_syntax_broken_script_breaks_the_scan_not_the_tester():
    """兜底 dict 必须与 `_facts` 同形，否则"让它变红"这句承诺会先变成 KeyError。"""
    facts = _scripts()
    fake = dict(next(iter(facts.values())))
    fake.update({'text': 'def (:', 'called': set(), 'flags': set(), 'raised': set(),
                 'consts': set(), 'env_written': False, 'direct_db': True, 'alembic': False,
                 'broken': True})
    _write_capable(fake)                                  # 不许抛
    _guarded('broken.py', fake, schema_ddl=_issues_schema_ddl(fake))   # 不许抛
    broken = {'x': fake}
    orig = globals()['_scripts']
    globals()['_scripts'] = lambda: broken
    try:
        try:
            test_write_capable_scripts_declare_their_database_in_code()
        except AssertionError:
            pass                        # 正确形状：判据说"它没自报"，而不是扫描器崩
        else:
            raise AssertionError('语法坏掉的文件被判成"没问题" ⇒ 解析失败被静默放过')
    finally:
        globals()['_scripts'] = orig


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
