# -*- coding: utf-8 -*-
"""裸 `alembic` 命令不许把目标悄悄翻成远程库（第 38 轮 B 席 BLOCKER）。

`alembic/env.py` 第 7 行 `from src.models.database import Base` 会拉起 `src/__init__.py`
→ `src/core/config.load_dotenv()` ⇒ `.env` 里那条**生产** `DATABASE_URL` 在 env.py 读环境变量之前
就已经在进程里了。旧写法是"没给 `ALEMBIC_DATABASE_URL` 就用 `DATABASE_URL`"，于是
`AGENTS.md` / `DEPLOYMENT.md` 推荐的裸 `alembic upgrade head` 与 `alembic downgrade …`
在本地一跑就是**对生产 Supabase 发 DDL**（`20260722_0002` 的 downgrade 是 `drop_table("prediction_change_logs")`，
即审计台账本体），全程没有 `[库]` 自报、没有确认头。

每种形状都在这里钉住；最后一种是**反向护栏** —— 修这个 bug 不许把 `render.yaml:11` 的启动路径弄坏。
条数不抄文字版：`pytest tests/unit/test_alembic_target_direction.py --collect-only -q`。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE = 'postgresql://u:S3cr3tPW@evil.invalid/proddb'


def _child_env(**overrides):
    env = os.environ.copy()
    # 子进程一律显式 utf-8：本机控制台默认 cp936，按 utf-8 读它的中文输出会解成替换符（假红）。
    env['PYTHONIOENCODING'] = 'utf-8'
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _run_alembic_offline(**overrides):
    """`--sql` 是离线模式：不连任何库，只用解析出来的 URL 选方言 ⇒ 方言就是它认定的目标。"""
    result = subprocess.run(
        [sys.executable, '-m', 'alembic', 'upgrade', 'head', '--sql'],
        cwd=str(ROOT), env=_child_env(**overrides),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300,
    )
    return result.returncode, result.stdout + result.stderr


def test_a_remote_database_url_stops_a_bare_alembic_command():
    rc, blob = _run_alembic_offline(DATABASE_URL=REMOTE, ALEMBIC_DATABASE_URL=None,
                                    LOCAL_DB_URL=None)
    assert rc != 0, '远程 DATABASE_URL + 裸 alembic 竟然继续往下走 ⇒ 方向翻转又活了（退码 %s）' % rc
    assert '[abort]' in blob, blob[-600:]
    # 拒跑的话必须指路，而不是只说不行
    assert 'run_migrations.py' in blob or 'ALEMBIC_DATABASE_URL' in blob, \
        '拒跑消息没告诉人该走哪条安全路：%s' % blob[-400:]
    assert 'S3cr3tPW' not in blob, '拒跑消息把连接串里的口令打出来了'
    assert 'Context impl Postgresql' not in blob, '都没拒成功：已经在给远程生成 DDL 了'


def test_a_local_database_url_is_still_inherited():
    """本地镜像那条便利不许因为修 bug 而消失，否则人人都会去把闸拆掉。"""
    rc, blob = _run_alembic_offline(DATABASE_URL='sqlite:///data/fund_insight.db',
                                    ALEMBIC_DATABASE_URL=None, LOCAL_DB_URL=None)
    assert rc == 0, blob[-600:]
    assert 'Context impl SQLiteImpl' in blob, blob[-600:]
    # 第 40 轮 A-M4：以前"每条往下走的分支都报名"只用 `src.count('[库]') >= 2` 撑着，
    # 而 abort 文案里也有一句 `[库]` ⇒ 把本地这条自报整段删掉，计数仍然 >= 2、用例全绿。
    assert '[库]' in blob, '本地这条分支没自报库名 ⇒ "每条路都报名"是假话'
    assert 'alembic 目标是本地 sqlite 文件' in blob, '本地分支报的不是库名：%s' % blob[-300:]


def test_the_second_flag_alone_does_not_unlock_an_inherited_remote():
    """第 40 轮 B 的 M-1：仓库的**默认状态**就是"`.env` 里躺着生产串 + 没人设
    `ALEMBIC_DATABASE_URL`"。上一版在这种状态下，只要 shell 里有过一个
    `ALEMBIC_ALLOW_REMOTE=1`（比如谁把它写进了 profile 或 CI env），就直接给生产生成 DDL
    ——而我自己的 abort 文案与 AGENTS/DEPLOYMENT 都写着"要两道旗子"。
    放行远程的前提改成：那条远程串得由人**亲口交给** `ALEMBIC_DATABASE_URL`。"""
    rc, blob = _run_alembic_offline(DATABASE_URL=REMOTE, ALEMBIC_DATABASE_URL=None,
                                    ALEMBIC_ALLOW_REMOTE='1', LOCAL_DB_URL=None)
    assert rc != 0, '只给一道旗子就继承了 .env 的生产串并放行 DDL ⇒ 第 38 轮那条 BLOCKER 复活'
    assert '[abort]' in blob and 'Context impl Postgresql' not in blob, blob[-400:]
    assert 'S3cr3tPW' not in blob, '拒跑消息里带出了连接串口令'
    # 反向对照：同一目标由人交给 ALEMBIC_DATABASE_URL + 两道旗子 ⇒ 该放行，且报名要报到 host
    rc2, blob2 = _run_alembic_offline(DATABASE_URL='sqlite:///data/fund_insight.db',
                                      ALEMBIC_DATABASE_URL=REMOTE, ALEMBIC_ALLOW_REMOTE='1',
                                      LOCAL_DB_URL=None)
    assert rc2 == 0, blob2[-400:]
    assert 'evil.invalid/proddb' in blob2, '自报只报了 scheme，没报到是哪台机器'
    assert 'S3cr3tPW' not in blob2, '自报把口令打出来了'




def test_an_explicit_alembic_url_overrides_a_remote_one():
    rc, blob = _run_alembic_offline(DATABASE_URL=REMOTE,
                                    ALEMBIC_DATABASE_URL='sqlite:///data/fund_insight.db',
                                    LOCAL_DB_URL=None)
    assert rc == 0, blob[-600:]
    assert 'Context impl SQLiteImpl' in blob and 'Context impl Postgresql' not in blob, blob[-600:]


def test_a_supplied_connection_still_runs(tmp_path):
    """`scripts/run_migrations.py`（= Render 的 startCommand）自己建好连接再交给 alembic：
    这条路**必须照跑**，否则这条闸门一上线就把生产启动弄挂。"""
    db = tmp_path / 'boot.db'
    script = tmp_path / 'boot_path.py'
    script.write_text(
        'from alembic import command\n'
        'from alembic.config import Config\n'
        'from sqlalchemy import create_engine\n'
        'cfg = Config(%r)\n'
        'eng = create_engine("sqlite:///%s")\n'
        'with eng.connect() as c:\n'
        '    cfg.attributes["connection"] = c\n'
        '    command.upgrade(cfg, "head")\n'
        'print("BOOT-PATH-OK")\n' % (str(ROOT / 'alembic.ini'), db.as_posix()),
        encoding='utf-8')
    result = subprocess.run(
        [sys.executable, str(script)], cwd=str(ROOT),
        env=_child_env(DATABASE_URL=REMOTE, ALEMBIC_DATABASE_URL=None, ALEMBIC_ALLOW_REMOTE=None,
                       LOCAL_DB_URL=None),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    assert 'BOOT-PATH-OK' in result.stdout, \
        '交了连接还被拒 ⇒ 生产启动会被这条闸门挡住（stderr 尾部：%s）' % result.stderr[-600:]
    # 第 41 轮 A-M1：这条路放行 DDL 的依据是"连接由调用方交进来"，而两道旗子一面都没立。
    # 上一版自报行不分叉，在这里照样印"由 ALEMBIC_DATABASE_URL 说出 + ALEMBIC_ALLOW_REMOTE=1 放行"
    # ⇒ 终端上凭空多出一次从没发生过的授权。现在报的内容必须与放行依据一致。
    line = [ln for ln in result.stderr.splitlines() if '[库]' in ln]
    assert line, '交了连接这条分支没自报（"每条路都报名"是假话）'
    assert '调用方交进来的连接' in line[0], '自报没说 DDL 其实走的是那条连接：%s' % line[0]
    assert 'ALEMBIC_ALLOW_REMOTE=1 放行' not in line[0], \
        '自报行伪造了一次从没发生过的旗子授权：%s' % line[0]
    assert 'S3cr3tPW' not in result.stderr


def _second_ini(tmp_path, url):
    """`-c 另一个.ini`：第 39 轮 B 的 MAJOR-1 —— 上一版只在"ini 还等于默认镜像串"时查方向，
    换一份 ini 就让整道闸（连同自报）静默失效。"""
    src = (ROOT / 'alembic.ini').read_text(encoding='utf-8')
    import re as _re
    src = _re.sub(r'sqlalchemy\.url\s*=.*', 'sqlalchemy.url = ' + url, src)
    src = _re.sub(r'script_location\s*=.*', 'script_location = '
                  + (ROOT / 'alembic').as_posix(), src)
    path = tmp_path / 'alembic_second.ini'
    path.write_text(src, encoding='utf-8')
    return str(path)


def test_a_second_ini_pointing_at_a_remote_is_refused_too(tmp_path):
    rc, blob = _run_alembic_offline(DATABASE_URL='sqlite:///data/fund_insight.db',
                                    ALEMBIC_DATABASE_URL=None, ALEMBIC_ALLOW_REMOTE=None,
                                    LOCAL_DB_URL=None)
    assert rc == 0 and 'Context impl SQLiteImpl' in blob     # 对照组：默认 ini + 本地库照常
    ini = _second_ini(tmp_path, REMOTE)
    result = subprocess.run(
        [sys.executable, '-m', 'alembic', '-c', ini, 'upgrade', 'head', '--sql'],
        cwd=str(ROOT), env=_child_env(DATABASE_URL='sqlite:///data/fund_insight.db',
                                      ALEMBIC_DATABASE_URL=None, ALEMBIC_ALLOW_REMOTE=None,
                                      LOCAL_DB_URL=None),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    blob = result.stdout + result.stderr
    assert result.returncode != 0 and '[abort]' in blob, \
        '换一份 ini 指远程就绕过了方向闸（判据又建在"配置是否等于我记得的默认串"上）：%s' % blob[-400:]
    assert 'S3cr3tPW' not in blob


def test_one_environment_variable_alone_does_not_unlock_a_remote(tmp_path):
    """第 39 轮 B：显式远程那条"被批准的路"曾经 0 自报、0 确认 ⇒ 现在要两道旗子。"""
    result = subprocess.run(
        [sys.executable, '-m', 'alembic', 'upgrade', 'head', '--sql'], cwd=str(ROOT),
        env=_child_env(DATABASE_URL='sqlite:///data/fund_insight.db',
                       ALEMBIC_DATABASE_URL=REMOTE, ALEMBIC_ALLOW_REMOTE=None, LOCAL_DB_URL=None),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    blob = result.stdout + result.stderr
    assert result.returncode != 0 and '[abort]' in blob and 'ALEMBIC_ALLOW_REMOTE' in blob, \
        '只设一个环境变量就能对远程发 DDL：%s' % blob[-300:]


def test_the_unlocked_remote_path_says_so_out_loud(tmp_path):
    """两道旗子都给了 ⇒ 放行，但**必须自报**；而且自报要进 stderr，
    不许污染 `--sql` 那份要存成文件的 SQL。"""
    result = subprocess.run(
        [sys.executable, '-m', 'alembic', 'upgrade', 'head', '--sql'], cwd=str(ROOT),
        env=_child_env(DATABASE_URL='sqlite:///data/fund_insight.db',
                       ALEMBIC_DATABASE_URL=REMOTE, ALEMBIC_ALLOW_REMOTE='1', LOCAL_DB_URL=None),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    assert result.returncode == 0, result.stderr[-400:]
    assert 'Context impl PostgresqlImpl' in result.stderr + result.stdout
    assert '[库]' in result.stderr, '放行远程却没自报（第 39 轮 B：这条路以前一声不吭）'
    assert '[库]' not in result.stdout, '自报混进了 stdout —— `--sql` 的产物被解释行脏了'
    assert 'S3cr3tPW' not in result.stdout + result.stderr
    # 反向对照（第 41 轮 A-m1：整批判据没有一条做过"把条件反过来"的变异）：
    # 旗子放行这条路的措辞不许漂到"连接交进来"那一支去，否则两支自报就成了同一句废话。
    line = [ln for ln in result.stderr.splitlines() if '[库]' in ln][0]
    assert 'ALEMBIC_ALLOW_REMOTE=1 放行' in line, '旗子放行这条路没说是两道旗子放开的：%s' % line
    assert '调用方交进来的连接' not in line, '把两条放行依据混成一句：%s' % line


def test_every_self_report_branch_is_a_real_print_statement():
    """仓库规矩（第 37 轮立的）："能动结构的东西必须第一行说清连哪个库"。
    三条往下走的分支（交连接 / 旗子放行 / 本地 sqlite）各要有一条真的 `print("[库] …")`。

    第 41 轮 A-M3：上一版这条写的是 `src.count('[库]') >= 2`，而 `[库]` 这个字面串也出现在
    abort 文案（"…动手前先自报 [库]"）里 ⇒ 删掉一整条自报分支，计数照样 >= 2、用例全绿。
    现在只数**语句**：`print("[库]` 少一条就红，并且逐条按内容点名，防止三句合成一句。
    """
    src = (ROOT / 'alembic' / 'env.py').read_text(encoding='utf-8')
    assert 'file=sys.stderr' in src
    assert src.count('print("[库]') >= 3, \
        'env.py 的自报语句少于 3 条（交连接 / 旗子放行 / 本地 sqlite 各一条），' \
        '实测 %s 条' % src.count('print("[库]')
    for marker in ('print("[库] alembic 本次 DDL 走**调用方交进来的连接**',
                   'print("[库] alembic 目标 %s（远程；由 ALEMBIC_DATABASE_URL 说出',
                   'print("[库] alembic 目标是本地 sqlite 文件'):
        assert marker in src, '少了这一条自报：%s' % marker


def test_the_self_report_ruler_is_the_shared_one_not_a_local_copy():
    """`env.py` 报目标时必须用那把共用的尺子，不是自己再搓一遍（第 47 轮 B-2）。

    这里原来躺着这仓库的**第三把**手搓剥口令尺子：`rest.split("@")[-1]` 只处理了
    `user:pw@host` 那一种位置，而 SQLAlchemy 允许把参数写进 query ——
    `postgresql://host:5432/db?password=S3cr3tPW` 被**原样**印进 `[abort]` 与 `[库]`
    那两行（后者走 stderr ⇒ 直接进 Render 日志）。而"不许再手搓剥口令"的那道棘轮只扫
    `scripts/` 与 `src/` ⇒ 对这里**结构上看不见**（同一条不变式写在 AGENTS 里，
    管它的两条尺子却不知道自己存在）。

    判据不 import `env.py`（一 import 就拉起 alembic 上下文、还要读 `.env`）：
    按 AST 把那个函数取出来自己执行一次，喂四种真会出现的连接串。
    控制：把 `_redact` 换回上一版那种写法，这四格里至少两格必须红（下面最后一段）。
    """
    import ast
    import sys
    src = (ROOT / 'alembic' / 'env.py').read_text(encoding='utf-8')
    tree = ast.parse(src)
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_redact'),
              None)
    assert fn is not None, 'env.py 里找不到 `_redact` ⇒ 自报换实现人了？那这条判据要跟着改'
    scripts = str(ROOT / 'scripts')
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    ns = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), '<env._redact>', 'exec'), ns)
    redact = ns['_redact']
    cases = (
        ('postgresql://aws-1-ap-south-1.pooler.supabase.co:6543/postgres?password=S3cr3tPW'
         '&sslmode=require', 'aws-1-ap-south-1.pooler.supabase.co'),
        ('postgresql:///postgres?host=aws-1-ap-south-1.pooler.supabase.co&password=S3cr3tPW',
         'aws-1-ap-south-1.pooler.supabase.co'),
        ('postgresql://u:S3cr3tPW@aws-1-ap-south-1.pooler.supabase.co:5432/postgres',
         'aws-1-ap-south-1.pooler.supabase.co'),
        ('postgres://someone:anotherS3cr3t@db.internal.example:5432/app', 'db.internal.example'),
    )
    for url, host in cases:
        got = redact(url)
        assert 'S3cr3t' not in got and 'anotherS3cr3t' not in got, \
            '自报行把口令带出来了：%s ← %s' % (got, url)
        assert host in got, \
            '口令是藏住了，可"是哪一台"也没了（%s）⇒ 报成"认不出"等于没报' % got
    # 反向：旧写法（`split('@')[-1]` 就完事）必须被这四格抓着 —— 否则上面那段是空判
    old = 'def _redact(url):\n    if "://" not in url:\n        return url\n' \
          '    scheme, rest = url.split("://", 1)\n    return "%s://%s" % (scheme, ' \
          'rest.split("@")[-1])\n'
    legacy_ns = {}
    exec(compile(old, '<legacy>', 'exec'), legacy_ns)
    leaked = [u for u, _h in cases if 'S3cr3t' in legacy_ns['_redact'](u)]
    assert leaked, '控制失效：上一版那种写法在这四格里一格都没泄露 ⇒ 样品是编的'
