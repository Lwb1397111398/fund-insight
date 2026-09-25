from logging.config import fileConfig
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.models.database import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `_db_guard` 住在 `scripts/` 下（那里不是包），而 alembic 只会把仓库根放进 sys.path。
# 显式按 `__file__` 找过去：报"哪个库"的尺子必须与守卫、与 `q.py` 是**同一把**（第 47 轮 B-2
# 的根因就是这里自己搓了第二份，query 串里的口令没剥）。
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


def _redact(url):
    """自报目标时只留 scheme + host/db，**连接串里的口令一个字符都不许出现在终端上**。

    第 47 轮 B-2：这里曾经是这仓库里的**第三把**手搓剥口令尺子 —— `rest.split("@")[-1]`
    只处理了 `user:pw@host` 那种位置，`postgresql://host:5432/db?password=S3cr3tPW`
    （SQLAlchemy 允许把参数写在 query 里）与 `postgresql:///db?host=…&password=…`
    都会被原样印进 `[abort]` / `[库]` 那两行，而后者走 stderr、直接进 Render 日志。
    更糟的是它**永远不会被那条棘轮抓到**：棘轮扫的是 `scripts/` 与 `src/`，这里是 `alembic/`。
    所以现在直接用 `_db_guard` 那把尺子；拿不到时只报 scheme —— **宁可不报名字，
    也不把原串印出去**（回退到"自己再搓一遍"正是这一族的病根）。
    """
    try:
        from _db_guard import machine_name
    except ImportError:
        return "%s://（认不出目标：守卫没加载成功）" % url.split("://", 1)[0]
    return machine_name(url)

explicit_url = os.getenv("ALEMBIC_DATABASE_URL")
ini_url = config.get_main_option("sqlalchemy.url") or ""
env_url = os.getenv("DATABASE_URL") or ""
# 第 38 轮 B 席的 BLOCKER：以前这里"没给 ALEMBIC_DATABASE_URL 就拿 DATABASE_URL 顶上"，
# 而上面那句 `from src.models.database import Base` 已经把 `.env` 读进环境了，于是文档推荐的
# 裸 `alembic upgrade head` / `alembic downgrade ...` 本地一跑就是**对生产发 DDL**
# （`downgrade` 那支会 drop 审计台账表）。
# 第 39 轮 B 又指出两半没堵严：(1) 上一版只在"ini 还等于那串默认镜像"时才检查方向，
# 改一下 `alembic.ini` 或用 `-c` 指第二个 ini，整道闸连同自报一起静默失效；
# (2) 被批准的显式远程路径 0 自报、0 确认，而同仓 `sync_db_columns.py` 动远程要三道旗子。
# 现在判在**最终解析出来的那个值**上。第 40 轮 B 的 M-1 又抓出第三半：上一版只要
# `ALEMBIC_ALLOW_REMOTE=1` 一道旗子就放行，而**目标可以完全来自 `.env`**（操作者从没说过
# 是哪个库）——那正是第 38 轮那条 BLOCKER 的机制本身，与我自己在 abort 文案里写的
# "两道旗子"和 AGENTS/DEPLOYMENT 两处文字都相反。
# 所以放行远程的前提是"目标由人**说出过**"：`ALEMBIC_DATABASE_URL` 本身就是那条远程串，
# 且 `ALEMBIC_ALLOW_REMOTE=1`；从 `.env` 继承来的远程一律拒（要动生产走 run_migrations.py）。
inheriting = (not explicit_url) and ini_url == "sqlite:///data/fund_insight.db" and bool(env_url)
target_url = explicit_url or (env_url if inheriting else "")
resolved = target_url or ini_url          # alembic 配置文件里解析到的那个目标
supplied = config.attributes.get("connection") is not None
if resolved and not resolved.startswith("sqlite"):
    stated = bool(explicit_url) and not explicit_url.startswith("sqlite")
    if not supplied and not (stated and os.getenv("ALEMBIC_ALLOW_REMOTE") == "1"):
        raise SystemExit(
            "[abort] alembic 这次解析出的目标是远程库（%s），不许由命令行直接发 DDL。"
            " 这个目标来自 %s。真要动远程库：把那条连接串**亲口交给** ALEMBIC_DATABASE_URL，"
            "并且再设 ALEMBIC_ALLOW_REMOTE=1（第 40 轮 B：只靠一道旗子、目标还来自 .env，"
            "等于把第 38 轮那条 BLOCKER 重新打开）；或走 scripts/run_migrations.py"
            "（它自己建连接、动手前先自报 [库]）。"
            % (_redact(resolved), 'ALEMBIC_DATABASE_URL' if explicit_url else '.env 的 DATABASE_URL')
        )
    # 自报走 stderr：`--sql` 的 stdout 是要人存成脚本文件的，不许混进解释行。
    # 第 41 轮 A-M1：这一句以前不分叉 —— 走"调用方交进来的连接"那条（= Render startCommand）
    # 时照样印"由 ALEMBIC_DATABASE_URL 说出 + ALEMBIC_ALLOW_REMOTE=1 放行"，可那条路上
    # 两道旗子**一面都没立**（放行依据是连接，不是旗子）。于是自报行凭空替人伪造了一次授权，
    # 而旧用例只看 stdout 里的 BOOT-PATH-OK，从不读这行的内容 ⇒ 说谎没人管。
    if supplied:
        print("[库] alembic 本次 DDL 走**调用方交进来的连接**（放行依据=那条连接，"
              "与 ALEMBIC_DATABASE_URL / ALEMBIC_ALLOW_REMOTE 两道旗子无关）；"
              "配置文件里解析到的目标是 %s —— 两者可以不是同一个库，以连接为准"
              % _redact(resolved), file=sys.stderr)
    else:
        print("[库] alembic 目标 %s（远程；由 ALEMBIC_DATABASE_URL 说出"
              "+ ALEMBIC_ALLOW_REMOTE=1 放行）" % _redact(resolved), file=sys.stderr)
elif resolved:
    print("[库] alembic 目标是本地 sqlite 文件：%s" % resolved.split(":///", 1)[-1],
          file=sys.stderr)
if target_url:
    config.set_main_option("sqlalchemy.url", target_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        context.configure(
            connection=provided_connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
