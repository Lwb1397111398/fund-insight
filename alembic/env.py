from logging.config import fileConfig
import os
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool

from src.models.database import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

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
# 现在判在**最终解析出来的那个值**上：远程要么由调用方把连接交进来
# （`scripts/run_migrations.py` = Render startCommand 走这条），要么显式给第二道旗子
# `ALEMBIC_ALLOW_REMOTE=1`。
inheriting = (not explicit_url) and ini_url == "sqlite:///data/fund_insight.db" and bool(env_url)
target_url = explicit_url or (env_url if inheriting else "")
resolved = target_url or ini_url          # alembic 真正拿去选方言/建连的就是它
if resolved and not resolved.startswith("sqlite"):
    if config.attributes.get("connection") is None and os.getenv("ALEMBIC_ALLOW_REMOTE") != "1":
        raise SystemExit(
            "[abort] alembic 这次解析出的目标是远程库（scheme=%s），不许由命令行直接发 DDL。"
            " 真要动远程库：设 ALEMBIC_DATABASE_URL 并且再设 ALEMBIC_ALLOW_REMOTE=1"
            "（两道旗子：只靠一个环境变量就放行太松，第 39 轮 B），"
            "或走 scripts/run_migrations.py（它自己建连接、动手前先自报 [库]）。"
            % resolved.split("://", 1)[0]
        )
    # 自报走 stderr：`--sql` 的 stdout 是要人存成脚本文件的，不许混进解释行。
    print("[库] alembic 目标 scheme=%s（远程）: 本次 DDL 会发到这里"
          % resolved.split("://", 1)[0], file=sys.stderr)
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
