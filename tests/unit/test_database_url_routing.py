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
    # 子进程 stdout 必须显式 UTF-8：中文用户名机器上默认按 GBK 编码，父进程按 UTF-8
    # 解码就会拿到替换字符（详见 S0 修复记录）。
    env["PYTHONIOENCODING"] = "utf-8"
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
        encoding="utf-8",
        errors="replace",
        check=True,
    )

    assert target.as_posix() in result.stdout.replace("\\", "/")
    assert result.stdout.rstrip().endswith("True")


def test_explicit_postgresql_url_fails_closed_when_driver_missing():
    """A production PostgreSQL URL must not silently fall back to local SQLite."""
    env = os.environ.copy()
    # 子进程 stdout 必须显式 UTF-8：中文用户名机器上默认按 GBK 编码，父进程按 UTF-8
    # 解码就会拿到替换字符（详见 S0 修复记录）。
    env["PYTHONIOENCODING"] = "utf-8"
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
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "psycopg2" in result.stdout


def test_unknown_database_url_scheme_is_rejected():
    """Unexpected DATABASE_URL schemes should fail closed instead of using SQLite."""
    env = os.environ.copy()
    # 子进程 stdout 必须显式 UTF-8：中文用户名机器上默认按 GBK 编码，父进程按 UTF-8
    # 解码就会拿到替换字符（详见 S0 修复记录）。
    env["PYTHONIOENCODING"] = "utf-8"
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
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0
    assert "DATABASE_URL" in result.stdout


def test_a_broken_postgres_driver_says_what_is_actually_broken(tmp_path):
    """驱动"装了但一导入就抛"时，那句话必须把**真实原因与解释器版本**印出来。

    起因（2026-09-25 的部署失败）：Render 上应用一起来就死在 `src/models/database.py`，
    而它报的是 "psycopg2 is not installed" —— 同一个 `try` 里还圈着 `create_engine` 与
    `logger.info`，任何深处抛的 ImportError 都由这句话顶包。日志被截断之后，这句谎话
    会把人整整一源地往"依赖没装"上带。现在只让 `import psycopg2` 待在那个 try 里，
    并且把原始异常与 `sys.version` 一起写进那句话。

    样品是一个**存在、但一导入就抛 ImportError** 的假 `psycopg2`（Windows 上也能造，
    不必真的把驱动卸掉）：撤掉"报真实原因"这半句，这条立刻红。
    """
    fake = tmp_path / "fakepkgs"
    fake.mkdir()
    (fake / "psycopg2.py").write_text(
        'raise ImportError("libpq.so.5: cannot open shared object file: No such file or directory")\n',
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["DATABASE_URL"] = "postgresql://u:p@db.invalid/proddb"
    env["PYTHONPATH"] = os.pathsep.join([str(fake), os.getcwd()])
    code = """
try:
    import src.models.database as m
    print("NO-RAISE DB_TYPE=%s" % m.DB_TYPE)
except RuntimeError as exc:
    print("RAISED::%s" % exc)
    print("CAUSE::%r" % (exc.__cause__,))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = result.stdout + result.stderr
    assert "RAISED::" in out, "没走到那句报错：%s" % out[-500:]
    assert "libpq.so.5" in out, "报错没带上真实原因（深处抛的 ImportError 又被顶包了）：%s" % out[-500:]
    assert "python %s" % sys.version.split()[0] in out, "报错没带解释器版本：%s" % out[-400:]
    assert "psycopg2 is not installed" not in out, "那句会把人往错方向带的旧话又回来了：%s" % out[-400:]


def test_a_plain_postgres_url_names_its_driver_instead_of_trusting_the_default(tmp_path):
    """`postgresql://`（没写 `+driver`）必须被钉成 `postgresql+psycopg2`，不交给 SQLAlchemy 的默认值。

    起因（2026-09-25 第二次部署失败）：默认值会随版本翻 —— 2.0.x 默认 psycopg2，2.1 起默认
    改成 psycopg(v3)，而 requirements 里只有 `psycopg2-binary` ⇒ 线上应用一起来就
    `ModuleNotFoundError: No module named 'psycopg'`（`create_engine` 阶段，连都没连）。
    判据钉的是**交给 create_engine 的那个 URL**，不是"能不能连上"：删掉那两行 `url.set(...)`，
    `engine.url.drivername` 立刻退回 `postgresql` ⇒ 这条红（在当前 2.0.48 上默认值也是
    psycopg2，所以只判"方言用的是谁"会结构性看不见这个缺陷）。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["DATABASE_URL"] = "postgresql://u:p@db.invalid/proddb"
    code = """
import src.models.database as m
print("URL=%s" % m.engine.url.drivername)
print("DIALECT=%s" % m.engine.dialect.driver)
print("TYPE=%s" % m.DB_TYPE)
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = result.stdout + result.stderr
    assert "URL=postgresql+psycopg2" in out, "驱动名没被钉住，交给默认值了：%s" % out[-400:]
    assert "DIALECT=psycopg2" in out, "方言不是 psycopg2：%s" % out[-400:]
    assert "TYPE=postgresql" in out, "DB_TYPE 没说清是 pg：%s" % out[-400:]


def test_requirements_keep_the_engine_version_that_matches_the_tests():
    """`sqlalchemy` 必须有上界：没上界 ⇒ 构建时解析到哪一版，"用哪个驱动"就跟着变。

    这条不是形式主义：本机 1082 条用例跑的是 2.0.48，而 2026-09-25 那次部署装到的那一版
    把 `postgresql://` 的默认驱动换成了 psycopg(v3) —— 应用没起来。写死驱动（上一条）之外，
    版本也得钉住，否则下一次"默默漂移"还是会从构建那天开始。
    """
    import re
    req = os.path.join(os.getcwd(), "requirements.txt")
    with open(req, encoding="utf-8") as handle:
        lines = [ln.strip() for ln in handle if ln.strip() and not ln.strip().startswith("#")]
    sa = [ln for ln in lines if re.match(r"^sqlalchemy(\b|[^A-Za-z])", ln, re.I)]
    assert sa, "requirements.txt 里没有 sqlalchemy 这一行（那更该问：引擎从哪来的）"
    assert any("<" in ln for ln in sa), \
        "sqlalchemy 没有上界（%s）⇒ 构建时解析到哪一版没人知道，驱动的默认值就会跟着变" % sa
    assert any("psycopg2" in ln.lower() for ln in lines), \
        "钉了 psycopg2 却要把 psycopg2 从依赖里删掉的话，得同时改 src/models/database.py 那两行"
