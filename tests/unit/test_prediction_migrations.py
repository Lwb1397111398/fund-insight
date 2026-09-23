from pathlib import Path
from io import StringIO
import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[2]

# 本机实测（2026-09-23）：Windows 在内存/CPU 吃紧时会把刚启动的 python 解释器直接打死，
# 退码是 0xC0000xxx 那一族（实测 0xC0000374 = STATUS_HEAP_CORRUPTION），
# 而且 **stdout/stderr 全空**——它压根没跑到 `run_migrations.py` 的第一行。
# 满负载 A/B：连"自报库名"那两行都删掉，同一个用例 3 次仍崩 1 次 ⇒ 环境级 flake，不是代码回归。
# 因此只对"像被系统打死"的退码重跑，真跑完返回非 0 的一次都不许多试。
def killed_by_the_os(rc) -> bool:
    return rc is not None and (rc < 0 or rc >= 0xC0000000)


def _run_the_migration_script(env, attempts=3):
    """跑 `scripts/run_migrations.py`；被系统打死就重跑，最多 `attempts` 次。

    子进程一律显式 `PYTHONIOENCODING=utf-8`：本仓库的控制台默认是 cp936，而下面按 utf-8 解码
    读它的输出 —— 不设的话"自报库名"那行会解成一串替换符，判据就在假红（2026-09-23 实测）。
    """
    env = dict(env)
    env.setdefault('PYTHONIOENCODING', 'utf-8')
    result = None
    for _ in range(attempts):
        result = subprocess.run(
            [sys.executable, "scripts/run_migrations.py"],
            cwd=ROOT, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        if not killed_by_the_os(result.returncode):
            return result
    return result


def test_alembic_adds_and_removes_prediction_change_log_on_existing_database(tmp_path):
    from alembic import command
    from alembic.config import Config

    database_path = tmp_path / "existing.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE predictions (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE bloggers (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE fund_history (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE sector_fund_mapping (id INTEGER PRIMARY KEY)"))

    config = Config(str(Path("alembic.ini").resolve()))
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head")

    inspector = inspect(engine)
    assert "prediction_change_logs" in inspector.get_table_names()
    columns = {column["name"] for column in inspector.get_columns("prediction_change_logs")}
    assert {
        "id", "prediction_id", "action", "source", "changed_fields",
        "before_state", "after_state", "created_at",
    } <= columns

    blogger_columns = {
        column["name"] for column in inspector.get_columns("bloggers")
    }
    assert {
        "archived_verified_count",
        "archived_correct_count",
        "archived_verify_score",
    } <= blogger_columns
    fund_history_columns = {
        column["name"] for column in inspector.get_columns("fund_history")
    }
    assert {"data_quality", "quality_note"} <= fund_history_columns
    mapping_columns = {
        column["name"] for column in inspector.get_columns("sector_fund_mapping")
    }
    assert {"reviewed", "created_at", "updated_at"} <= mapping_columns

    command.downgrade(config, "prediction_schema_baseline")

    assert "prediction_change_logs" not in inspect(engine).get_table_names()


def test_alembic_can_render_offline_sql(tmp_path):
    from alembic import command
    from alembic.config import Config

    output = StringIO()
    database_url = f"sqlite:///{(tmp_path / 'offline.db').as_posix()}"
    config = Config(str(Path("alembic.ini").resolve()), output_buffer=output)
    config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(config, "head", sql=True)

    rendered = output.getvalue().lower()
    assert "create table prediction_change_logs" in rendered
    assert "archived_verified_count" in rendered
    assert "archived_correct_count" in rendered
    assert "archived_verify_score" in rendered


def test_migration_runner_upgrades_explicit_sqlite_database(tmp_path):
    database_path = tmp_path / "runner.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE bloggers (id INTEGER PRIMARY KEY)"))

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["APP_ENV"] = "test"
    result = _run_the_migration_script(env)

    assert result.returncode == 0, '退码 %s｜stdout=%s｜stderr=%s' % (
        result.returncode, result.stdout, result.stderr)
    assert '[库] 本地镜像库' in result.stdout, \
        '动手前没自报"往哪个库发 DDL"（.env 默认指向生产 ⇒ 这行是唯一的方向提示）'
    columns = {column["name"] for column in inspect(engine).get_columns("bloggers")}
    assert "archived_verified_count" in columns


def test_migration_runner_fails_closed_in_production_without_database_url(tmp_path):
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env.pop("ALEMBIC_DATABASE_URL", None)
    env["APP_ENV"] = "production"
    result = _run_the_migration_script(env)

    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr


def test_only_a_process_the_os_killed_gets_a_second_attempt(monkeypatch):
    """重跑只留给"没跑到第一行就被打死"的形状；真失败（退码 1）多试一次就是在藏回归。"""
    calls = []

    class _Result:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, '', ''

    def fake_run(*args, **kwargs):
        calls.append(kwargs['env']['APP_ENV'])
        return _Result(SEQUENCE[min(len(calls), len(SEQUENCE)) - 1])

    monkeypatch.setattr(subprocess, 'run', fake_run)
    SEQUENCE = [3221227274, 0]
    result = _run_the_migration_script({'APP_ENV': 'test'})
    assert len(calls) == 2, '崩了一次就该重跑，实际跑了 %d 次' % len(calls)
    assert result.returncode == 0

    calls[:] = []
    SEQUENCE[:] = [1]
    result = _run_the_migration_script({'APP_ENV': 'test'})
    assert len(calls) == 1, '脚本自己返回 1 却被重试了 %d 次 ⇒ 闸门在藏真失败' % len(calls)
    assert result.returncode == 1


def test_crash_like_exit_codes_are_recognised():
    assert killed_by_the_os(3221227274)          # 0xC0000374 STATUS_HEAP_CORRUPTION
    assert killed_by_the_os(-6)                  # 类 Unix 上信号杀掉的形状
    assert not killed_by_the_os(0)
    assert not killed_by_the_os(1)               # run_migrations.main() 自己报的失败
