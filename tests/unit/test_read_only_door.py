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
    ) % (str(path), str(path).replace('\\', '/')))
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
        'why = _db_guard._write_probe(eng, "CREATE TABLE _probe(x int)")\n'
        'print(json.dumps({"why": why}))\n'
    ) % str(path).replace('\\', '/'))
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
