"""数据库连接串隔离测试。"""
import os
import subprocess
import sys


def test_pytest_session_uses_a_temporary_sqlite_database():
    """测试启动不能继承开发机或生产环境的 DATABASE_URL。"""
    from src.models.database import engine

    assert engine.url.drivername == "sqlite"
    assert "fund-insight-pytest-" in str(engine.url)


def test_explicit_sqlite_database_url_is_honored(tmp_path):
    """临时恢复和测试不能回退到默认本地数据库。"""
    target = tmp_path / "isolated.db"
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{target.as_posix()}"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sqlalchemy import inspect; from src.models.database import engine, init_db; init_db(); print(engine.url); print(inspect(engine).has_table('bloggers'))",
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert target.as_posix() in result.stdout.replace("\\", "/")
    assert result.stdout.rstrip().endswith("True")


def test_explicit_postgresql_url_fails_closed_when_driver_missing():
    """A production PostgreSQL URL must not silently fall back to local SQLite."""
    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql://user:pass@example.invalid/fund_insight"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import builtins

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "psycopg2":
        raise ImportError("blocked psycopg2")
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import

try:
    import src.models.database
except RuntimeError as exc:
    print(str(exc))
    raise SystemExit(0)

raise SystemExit(1)
""",
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "psycopg2" in result.stdout


def test_unknown_database_url_scheme_is_rejected():
    """Unexpected DATABASE_URL schemes should fail closed instead of using SQLite."""
    env = os.environ.copy()
    env["DATABASE_URL"] = "mysql://user:pass@example.invalid/fund_insight"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
try:
    import src.models.database
except RuntimeError as exc:
    print(str(exc))
    raise SystemExit(0)

raise SystemExit(1)
""",
        ],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "DATABASE_URL" in result.stdout
