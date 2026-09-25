# -*- coding: utf-8 -*-
"""只读分析脚本的统一连库口 `_db_guard.read_only_connect()` 的行为判据（第 41 轮 B-MAJOR-1）。

起因：`scripts/audit_l3_clear_labels.py`、`scripts/estimate_l3_vague_labels.py`、
`scripts/backtest_l1_weighting.py` 三个脚本都写着"环境里有 `DATABASE_URL` 就连它"，
而本地 `.env` 里那条是**生产 Supabase** ⇒ 一句"跑一下 L3 估算"默认就读线上；
更糟的是它们把结果写进 `docs/` 报告，数据源那一栏只写 `"DATABASE_URL"`（变量名，
不是机器名）⇒ 一份不知道出自哪个库的数字进了文档。守卫扫描只判"能不能写"，
所以这三个纯读脚本从来没被问过连哪儿 —— 读侧不是攻击面这个假设就是这条的根因。

每一条都跑在**子进程 + 临时 sqlite**上：`resolve_read_target()` 有进程级 memo，
在同进程里跑两条会互相污染，而且它会写 `os.environ["DATABASE_URL"]`
（在测试进程里改这个 = 第 33 轮那次生产误连的同款事故）。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD_DIR = str(ROOT / 'scripts')

PRELUDE = (
    'import json, os, sys\n'
    'sys.path.insert(0, %r)\n'
    'sys.path.insert(0, r"scripts")\n'
    'import _db_guard\n'
) % GUARD_DIR


def _run(tmp_path, body, **env_overrides):
    """在子进程里跑一段 `_db_guard` 代码，返回解析后的 JSON（顺便保证父进程环境干净）。"""
    script = tmp_path / 'probe.py'
    script.write_text(PRELUDE + body, encoding='utf-8')
    env = os.environ.copy()
    env['PYTHONIOENCODING'] = 'utf-8'
    # 默认让 `.env` 那串生产串"在场"——判的就是"它在场时脚本会不会偷偷连上去"
    env['DATABASE_URL'] = env_overrides.pop('DATABASE_URL',
                                           'postgresql://u:S3cr3tPW@evil.invalid/proddb')
    env['LOCAL_DB_URL'] = ''
    for key, value in env_overrides.items():
        env[key] = value
    result = subprocess.run([sys.executable, str(script)], cwd=str(ROOT), env=env,
                            capture_output=True, text=True, encoding='utf-8',
                            errors='replace', timeout=300)
    return result


def _payload(result):
    tail = (result.stdout or '').strip().splitlines()
    assert tail, '子进程没吐出 JSON：rc=%s stderr=%s' % (result.returncode, result.stderr[-400:])
    return json.loads(tail[-1])


def _temp_db(tmp_path, name='probe.db', with_row=True):
    """建一个真 sqlite 文件，好让"只读"是引擎的事实而不是我的正则。"""
    import sqlite3
    path = tmp_path / name
    conn = sqlite3.connect(str(path))
    conn.execute('create table t(x int)')
    if with_row:
        conn.execute('insert into t values (1)')
    conn.commit()
    conn.close()
    return path


def test_no_flag_means_the_mirror_even_when_env_points_at_production(tmp_path):
    """`.env` 里躺着生产串、命令行没说话 ⇒ 必须钉到镜像，并且 label 报的是**机器名**。"""
    mirror = _temp_db(tmp_path)
    result = _run(tmp_path, (
        'url, is_prod = _db_guard.resolve_read_target([])\n'
        'print(json.dumps({"url": url, "is_prod": is_prod, "label": _db_guard.machine_name(url)}))\n'
    ), LOCAL_DB_URL=str(mirror))
    payload = _payload(result)
    assert payload['is_prod'] is False, '没给 --production 却判成生产 ⇒ 默认就读线上了'
    assert payload['url'].startswith('sqlite'), '默认连的不是 sqlite 镜像：%s' % payload['url']
    assert 'evil.invalid' not in payload['label'], '自报里还留着生产主机名：' + payload['label']
    assert 'S3cr3tPW' not in result.stdout + result.stderr, '输出里出现了连接串口令'


def test_reading_production_requires_the_explicit_flag(tmp_path):
    """反向对照（同一个 flag 的另一面）：显式 `--production` 才会拿那条远程串。

    只测"默认不连生产"是不够的 —— 把 `production_requested` 写成恒 False，
    上一条照样全绿，而"要看线上数字"这条路就悄悄没了（第 41 轮 A-m1 同一类错误）。
    """
    result = _run(tmp_path, (
        'url, is_prod = _db_guard.resolve_read_target(["--production"])\n'
        'print(json.dumps({"url": url[:16], "is_prod": is_prod}))\n'
    ))
    payload = _payload(result)
    assert payload['is_prod'] is True, '给了 --production 还不认 ⇒ 读线上的正门被焊死了'
    assert payload['url'].startswith('postgresql'), '生产分支拿的不是那条串：' + payload['url']


def test_asking_for_production_after_the_mirror_is_decided_says_so(tmp_path):
    """同进程里"先按镜像定过库、后面又要求 `--production`"必须**说破**（任务 #64）。

    memo 是真的（一次进程只定一次库，否则两行 `[env]` 会自相矛盾），今天唯一调用方
    `backtest_l1_weighting.py` 从同一个 `sys.argv` 取旗子 ⇒ 踩不到这条。
    但"静默复用镜像"的后果是报告里的数来自一个没人想要的库，而屏幕上一个字都没说。
    """
    mirror = _temp_db(tmp_path)
    result = _run(tmp_path, (
        'first = _db_guard.resolve_read_target([])\n'
        'second = _db_guard.resolve_read_target(["--production"])\n'
        'print(json.dumps({"same": first == second, "prod": second[1],'
        ' "url": first[0][:12]}))\n'
    ), LOCAL_DB_URL=str(mirror))
    payload = _payload(result)
    assert payload['same'] is True and payload['prod'] is False, \
        '第二次调用把判过的事改掉了（同进程两份真值）：%s' % payload
    assert '已在开始时把目标定成本地镜像' in result.stdout, \
        '复用了镜像却一个字都没说 ⇒ 报告里的数没人知道出自哪个库：%s' % result.stdout


def test_the_mirror_engine_refuses_writes_but_a_plain_path_url_does_not(tmp_path):
    """只读必须是**引擎**的事实：`mode=ro` 的 URI 形状写不进去，普通 `sqlite:///路径` 写得进。

    为什么留第二条对照：`_sqlite_ro_url` 一旦拼错（比如忘了 `uri=true`），
    只测"写被拒"的那条会直接变红而不是静默通过 —— 但若哪天有人"顺手简化"成普通 URL，
    这行对照就把它抓住（它自己就是"只读不成立"的形状）。
    """
    path = _temp_db(tmp_path, 'mirror_ro.db')
    result = _run(tmp_path, (
        'import sqlalchemy as sa\n'
        'from sqlalchemy import text\n'
        'out = {}\n'
        'eng = sa.create_engine(_db_guard._sqlite_ro_url(%r), connect_args={"uri": True})\n'
        'with eng.connect() as c:\n'
        '    out["read"] = c.execute(text("select count(*) from t")).scalar()\n'
        '    try:\n'
        '        c.execute(text("update t set x = 2"))\n'
        '        out["write"] = "accepted"\n'
        '    except Exception as exc:\n'
        '        out["write"] = "refused:" + str(exc).splitlines()[0][:40]\n'
        'eng2 = sa.create_engine("sqlite:///" + %r)\n'
        'with eng2.connect() as c2:\n'
        '    try:\n'
        '        c2.execute(text("update t set x = 3")); c2.commit()\n'
        '        out["plain"] = "accepted"\n'
        '    except Exception as exc:\n'
        '        out["plain"] = "refused"\n'
        'print(json.dumps(out))\n'
    ) % (path.as_posix(), path.as_posix()))
    payload = _payload(result)
    assert payload['read'] == 1, '只读引擎连数据都读不到（URL 拼错了？）：%s' % payload
    assert str(payload['write']).startswith('refused'), \
        'mode=ro 的引擎竟然收下了 UPDATE ⇒ "只读"是假话：%s' % payload['write']
    assert payload['plain'] == 'accepted', \
        '对照组失效：普通 URL 也拒写了，那上一条永远不可能红（判据自证）'


def test_write_probe_fails_closed_when_the_connection_is_writable(tmp_path):
    """探针不通就不许继续：`_write_probe` 在能写的连接上必须返回原因字符串。

    这条是"检测型守卫"的反面检查 —— 只测"只读连接上探针返回 None"是好消息，
    但那只证明它不响；必须同时证明它**会**响（第 19 轮那条规矩：判据要能红）。
    """
    path = _temp_db(tmp_path, 'writable.db')
    result = _run(tmp_path, (
        'import sqlalchemy as sa\n'
        'eng = sa.create_engine("sqlite:///" + %r)\n'
        'why = _db_guard._write_probe(eng, ["CREATE TABLE _probe(x int)", "DROP TABLE _probe"])\n'
        'print(json.dumps({"why": why}))\n'
    ) % path.as_posix())
    why = _payload(result)['why']
    assert why, '在可写的连接上探针说"只读成立" ⇒ 这道闸恒真'
    assert '不是只读' in why or '只读' in why, '探针的报错没说明白：' + str(why)


def test_the_three_l3_l1_scripts_now_go_through_the_door(tmp_path):
    """三个肇事脚本必须改用统一连库口，不许再自己 `create_engine(os.getenv(...))`。

    判据是 AST 形状而不是注释：把 `read_only_connect()` 换回 `create_engine(url)`
    会立刻红（`_builds_engine_from_env` 那条扫描规则也盯着同一个形状）。
    """
    import ast
    for name in ('audit_l3_clear_labels.py', 'estimate_l3_vague_labels.py',
                 'backtest_l1_weighting.py'):
        src = (ROOT / 'scripts' / name).read_text(encoding='utf-8')
        tree = ast.parse(src, filename=name)
        calls = {((n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id)
                  if isinstance(n.func, ast.Name) or isinstance(n.func, ast.Attribute) else '')
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert 'read_only_connect' in calls, '%s 没走统一只读门（调用点：%s）' % (name, sorted(calls)[:8])
        assert 'create_engine' not in calls, '%s 又自己建 engine 了' % name


def _hardcoded_report_dates(src):
    """模块里**除 docstring 之外**写死的 `YYYY-MM-DD`（docstring 是在解释，不是在被渲染）。"""
    import ast
    import re
    tree = ast.parse(src)
    prose = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, 'body', None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                prose.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in prose and re.search(r'\b20\d{2}-\d{2}-\d{2}\b', n.value)]


def test_the_l3_report_date_is_computed_and_not_copied():
    """报告上那个日期得**现算**，而且这件事得有两条腿钉着（第 44 轮 A：这条改完零覆盖）。

    第 43 轮 B-MINOR-2 把模板字面量 `"2026-07-29"` 换成 `_today_beijing()` —— 换完没有任何用例
    盯着它，把那句改回字面量，全套件仍然全绿。两半各钉一条：
    ① 行为：子进程里真问一次 `_today_beijing()`，它必须等于**今天的北京日期**
       （老字面量冻在 2026-07-29，这一支必红）；
    ② 形状：脚本里除 docstring 外不许再出现写死的报告日期，并**当场造一处违规**证明尺子会响
       （只测仓库现状的判据，在有人把日期抄回模板那天就是空判）。
    """
    from datetime import datetime, timedelta, timezone
    expected = (datetime.now(timezone.utc) + timedelta(hours=8)).date().isoformat()
    code = ('import importlib.util, sys\n'
            'spec = importlib.util.spec_from_file_location("m", sys.argv[1])\n'
            'm = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(m)\n'
            'print(m._today_beijing().isoformat())\n')
    for name in ('audit_l3_clear_labels.py', 'estimate_l3_vague_labels.py'):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        out = subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts' / name)],
                             cwd=str(ROOT), env=env, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=300)
        assert out.returncode == 0, '%s 的 `_today_beijing()` 跑不起来：%s' % (
            name, (out.stdout + out.stderr)[-300:])
        got = out.stdout.strip().splitlines()[-1]
        assert got == expected, '%s 报的日期是 %s，今天北京是 %s ⇒ 日期不是现算的' % (
            name, got, expected)

    src = (ROOT / 'scripts' / 'estimate_l3_vague_labels.py').read_text(encoding='utf-8')
    frozen_in_repo = _hardcoded_report_dates(src)
    assert not frozen_in_repo, '脚本里又出现写死的报告日期：%s' % frozen_in_repo
    frozen = src.replace('report_date = _today_beijing().isoformat()', 'report_date = "2026-07-29"')
    assert frozen != src, '替换没生效（那句的形状变了）⇒ 下面这条控制断言是空判'
    assert _hardcoded_report_dates(frozen), '把日期抄回字面量，尺子却没响 ⇒ 这条判据是死的'


def test_the_read_only_door_also_blocks_writes_that_go_through_attach(tmp_path):
    """`mode=ro` 只锁**主库** —— 侧门是 `ATTACH`（第 44 轮 B-m1）。

    这道门的承诺是"引擎级只读"，而 SQLite 的 `mode=ro` 管的是主库那一个文件：
    从同一条 ro 连接 `ATTACH` 一个普通可写文件、再往**那个库**建表，SQLite 是答应的。
    于是"只读"这句话留着一个能把写带出去的出口 —— 而读侧脚本拿到的是整条连接。
    补的是 `PRAGMA query_only=ON`（管整条连接、含 attach 进来的库）。

    对照组是这条判据的第二半：**不用门的普通连接**必须真的写成功，
    否则"写不成功"可能只是因为 SQLite 压根不让 ATTACH，我的锁就成了看不见的空话。
    """
    mirror = _temp_db(tmp_path)
    side = _temp_db(tmp_path, name='side.db').as_posix()
    mirror_posix = str(mirror).replace('\\', '/')
    # 两个路径经环境变量传进子进程：在这里手拼 SQL 字面量正是要避免的事（引号、反斜杠一转就错）
    body = (
        'import sqlalchemy as sa\n'
        'engine, session, label = _db_guard.read_only_connect([])\n'
        'out = {"pragma": None, "door": None, "plain": None}\n'
        'with engine.connect() as conn:\n'
        '    out["pragma"] = conn.exec_driver_sql("PRAGMA query_only").scalar()\n'
        '    try:\n'
        '        conn.exec_driver_sql("ATTACH DATABASE %r AS side" % os.environ["SIDE"])\n'
        '        conn.exec_driver_sql("CREATE TABLE side.t_from_door(x int)")\n'
        '        out["door"] = "succeeded"\n'
        '    except Exception as exc:\n'
        '        out["door"] = "refused: " + str(exc)[:60]\n'
        'plain = sa.create_engine("sqlite:///" + os.environ["MIRROR"])\n'
        'with plain.connect() as conn:\n'
        '    try:\n'
        '        conn.exec_driver_sql("ATTACH DATABASE %r AS side2" % os.environ["SIDE"])\n'
        '        conn.exec_driver_sql("CREATE TABLE side2.t_plain(x int)")\n'
        '        conn.commit()\n'
        '        out["plain"] = "succeeded"\n'
        '    except Exception as exc:\n'
        '        out["plain"] = "refused: " + str(exc)[:60]\n'
        'out["label"] = label\n'
        'print(json.dumps(out, ensure_ascii=False))\n'
    )
    payload = _payload(_run(tmp_path, body, LOCAL_DB_URL=str(mirror), SIDE=side,
                            MIRROR=mirror_posix))

    assert payload['door'].startswith('refused'), \
        '通过只读门的连接仍然能写 ATTACH 进来的库 ⇒ "引擎级只读"这句话有出口：%s' % payload['door']
    assert str(payload['pragma']) in ('1', 'True'), 'pragma query_only 没打开：%s' % payload['pragma']
    assert payload['plain'] == 'succeeded', (
        '对照组（普通连接）也没写成功 ⇒ ATTACH 那一路压根没被打开过，'
        '上面那条"被拒绝"证明不了任何事：%s' % payload['plain'])


def _doc_snapshot():
    import hashlib
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / 'docs').glob('*.md')}


def test_help_and_bad_flags_never_run_the_analysis(tmp_path):
    """`--help` 与打错的开关必须**一笔都不跑**：这三个脚本会往 `docs/` 写报告。

    为什么单独立一条（第 42 轮 B-MINOR-5）：第 39 轮 `mutation_proof_frontend.py` 就是
    "顶部 `import argparse` 却从不调用" ⇒ `--help` 让它整套跑完并就地改写 `web/`。
    同一个形状在这三个只读分析脚本上更狠：`--help` 会真连库、算完、然后把一节报告追加进
    `docs/L3_VAGUE_LABEL_ESTIMATE.md`。所以判据不是"帮助里有 usage"，
    而是**跑完帮助之后 `docs/` 一个字节都没变**。
    """
    for name in ('audit_l3_clear_labels.py', 'estimate_l3_vague_labels.py',
                 'backtest_l1_weighting.py'):
        before = _doc_snapshot()
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        helpout = subprocess.run([sys.executable, str(ROOT / 'scripts' / name), '--help'],
                                 cwd=str(ROOT), env=env, capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=300)
        assert helpout.returncode == 0, '%s --help 退 %s：%s' % (
            name, helpout.returncode, (helpout.stdout + helpout.stderr)[-400:])
        assert 'usage' in helpout.stdout.lower(), '%s 的 --help 没印 usage（argparse 没接上）' % name
        assert _doc_snapshot() == before, '%s 的 --help 改写了 docs/ 里的报告' % name

        bad = subprocess.run([sys.executable, str(ROOT / 'scripts' / name), '--no-such-flag'],
                             cwd=str(ROOT), env=env, capture_output=True, text=True,
                             encoding='utf-8', errors='replace', timeout=300)
        assert bad.returncode == 2, '%s 收了不存在的开关还退 %s ⇒ 它在往下跑：%s' % (
            name, bad.returncode, (bad.stdout + bad.stderr)[-300:])
        assert _doc_snapshot() == before, '%s 的坏旗子路径动了 docs/' % name


def _sqlite_tables(path):
    import sqlite3
    conn = sqlite3.connect(str(path))
    try:
        return sorted(r[0] for r in conn.execute(
            "select name from sqlite_master where type='table'"))
    finally:
        conn.close()


def test_the_probe_leaves_nothing_behind_in_the_db_it_checks(tmp_path):
    """**探针自己不许写脏它正在检查的库**（第 41 轮 B-MAJOR-2，端到端复现过）。

    上一版 sqlite 腿只发一条 `CREATE TABLE`，而 pysqlite 对 DDL 是隐式提交 ⇒
    "只读没成立"那种情况（正是要报警的那种）反而是探针把表建进了那个库再报错
    （实测跑完 `sqlite_master` 里多出 `_db_guard_probe`）。
    `scripts/q.py` 早就有同一条教训的用例（不回滚就把连的库弄脏），新门没把它带过来。
    """
    path = _temp_db(tmp_path, 'residue.db')
    before = _sqlite_tables(path)
    result = _run(tmp_path, (
        'import sqlalchemy as sa\n'
        'eng = sa.create_engine("sqlite:///" + %r)\n'
        'why = _db_guard._write_probe(eng, ["CREATE TABLE _db_guard_probe(x int)",'
        ' "DROP TABLE _db_guard_probe"])\n'
        'print(json.dumps({"why": why}))\n'
    ) % path.as_posix())
    payload = _payload(result)
    assert '不是只读' in (payload['why'] or ''), payload
    assert _sqlite_tables(path) == before, \
        '探针报完警还在库里留了东西：%s → %s' % (before, _sqlite_tables(path))


def test_a_failed_cleanup_says_so_instead_of_silently_leaving_residue(tmp_path):
    """建得起来却删不掉 ⇒ 必须把"可能有残渣、要人工核对"说出来。

    单独一条的理由：修复本身是"建完就删"，那**删失败**是新代码独有的失败模式；
    不钉这条，探针就又变回"写完东西不认账"。
    """
    path = _temp_db(tmp_path, 'residue2.db')
    result = _run(tmp_path, (
        'import sqlalchemy as sa\n'
        'eng = sa.create_engine("sqlite:///" + %r)\n'
        'why = _db_guard._write_probe(eng, ["CREATE TABLE _db_guard_probe(x int)",'
        ' "DROP TABLE 这张表不存在"])\n'
        'print(json.dumps({"why": why}))\n'
    ) % path.as_posix())
    why = _payload(result)['why']
    assert '残渣' in why, '清理失败却被说成一句普通报警：%s' % why


def test_read_only_connect_names_the_target_before_it_touches_it(tmp_path):
    """连不上时也要先说"我想连谁"（第 41 轮 B-MINOR-2 / A42 MINOR-2）。

    上一版 `[库]` 排在探针**之后**：主机名解析不了时 SQLAlchemy 直接抛
    `OperationalError`，那条路一个字都没自报 —— 而"自报连的是哪个库"这句话的全部意义
    就是"出事时知道在动谁"。顺带钉 B-MINOR-1：非生产分支不再调第二次 pin，
    所以 stdout 只许有**一行** `[env]`。
    """
    mirror = _temp_db(tmp_path, 'order.db')
    result = _run(tmp_path, (
        'engine, session, label = _db_guard.read_only_connect(argv=[])\n'
        'print(json.dumps({"label": label}))\n'
    ), LOCAL_DB_URL=str(mirror))
    lines = [ln for ln in (result.stdout or '').splitlines()]
    env_lines = [ln for ln in lines if ln.startswith('[env]')]
    assert len(env_lines) == 1, '镜像这条印了 %d 行 [env]（memo 没起作用）：%s' % (
        len(env_lines), env_lines)
    names = [i for i, ln in enumerate(lines) if ln.startswith('[库]')]
    assert len(names) >= 2 and '准备' in lines[names[0]], \
        '没有"动手之前先报名"那一行：%s' % [lines[i] for i in names]

    bad = _run(tmp_path, (
        'engine, session, label = _db_guard.read_only_connect(argv=["--production"])\n'
        'print(json.dumps({"label": label}))\n'
    ), DATABASE_URL='postgresql://u:S3cr3tPW@host-does-not-exist.invalid/proddb')
    blob = (bad.stdout or '') + (bad.stderr or '')
    assert bad.returncode != 0, '连不上线上库却当成功返回'
    assert '[abort]' in blob and 'host-does-not-exist.invalid' in blob, blob[-400:]
    assert 'S3cr3tPW' not in blob, '连接失败的报错里带出了口令'


def test_machine_name_prints_a_path_you_can_actually_open(tmp_path):
    """自报的路径要照着"打得开的那个"报（第 41 轮 B-MINOR-3）。

    sqlite URL 里前导斜杠的个数有意义：三斜杠是相对路径、四斜杠才是 POSIX 绝对路径，
    Windows 两种写法都存在。一律 `lstrip('/')` 会把 `/home/x.db` 说成 `home/x.db` ——
    比原来多一个斜杠更误导人，所以两侧都要钉。
    """
    result = _run(tmp_path, (
        'print(json.dumps({'
        '"win4": _db_guard.machine_name("sqlite:////E:/AI Agent/data/f.db"),'
        '"win3": _db_guard.machine_name("sqlite:///E:/data/f.db"),'
        '"posix": _db_guard.machine_name("sqlite:////home/me/f.db"),'
        '"relative": _db_guard.machine_name("sqlite:///data/fund_insight.db"),'
        '"remote": _db_guard.machine_name("postgresql://u:S3cr3tPW@h.example:5432/prod")'
        '}))\n'))
    got = _payload(result)
    assert got['win4'] == 'E:/AI Agent/data/f.db', got
    assert got['win3'] == 'E:/data/f.db', got
    assert got['posix'] == '/home/me/f.db', got              # POSIX 绝对不许被剥成相对
    assert got['relative'] == 'data/fund_insight.db', got    # 本来就相对的不许加斜杠
    assert got['remote'] == 'postgresql://h.example:5432/prod', got
