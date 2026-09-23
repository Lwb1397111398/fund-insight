# -*- coding: utf-8 -*-
"""裸 `alembic` 命令不许把目标悄悄翻成远程库（第 38 轮 B 席 BLOCKER）。

`alembic/env.py` 第 7 行 `from src.models.database import Base` 会拉起 `src/__init__.py`
→ `src/core/config.load_dotenv()` ⇒ `.env` 里那条**生产** `DATABASE_URL` 在 env.py 读环境变量之前
就已经在进程里了。旧写法是"没给 `ALEMBIC_DATABASE_URL` 就用 `DATABASE_URL`"，于是
`AGENTS.md` / `DEPLOYMENT.md` 推荐的裸 `alembic upgrade head` 与 `alembic downgrade …`
在本地一跑就是**对生产 Supabase 发 DDL**（`20260722_0002` 的 downgrade 是 `drop_table("prediction_change_logs")`，
即审计台账本体），全程没有 `[库]` 自报、没有确认头。

四种形状都在这里钉住；第 4 种是**反向护栏** —— 修这个 bug 不许把 `render.yaml:11` 的启动路径弄坏。
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
        env=_child_env(DATABASE_URL=REMOTE, ALEMBIC_DATABASE_URL=None, LOCAL_DB_URL=None),
        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
    assert 'BOOT-PATH-OK' in result.stdout, \
        '交了连接还被拒 ⇒ 生产启动会被这条闸门挡住（stderr 尾部：%s）' % result.stderr[-600:]
