from logging.config import fileConfig
import os

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
# 而上面那句 `from src.models.database import Base` 已经把 `.env` 读进环境了 ⇒
# 文档推荐的裸 `alembic upgrade head` / `alembic downgrade …` 本地一跑就是**对生产发 DDL**
# （`downgrade` 那支还会 drop 审计台账表）。现在：只有 ini 还停在默认镜像时才继承 `DATABASE_URL`，
# 而继承到一个**看起来是远程**的目标就拒跑 —— 除非调用方已经把连接交进来
# （`scripts/run_migrations.py` 与 Render 的 startCommand 走的就是这条）或显式给了
# `ALEMBIC_DATABASE_URL`。
inheriting = (not explicit_url) and ini_url == "sqlite:///data/fund_insight.db" and bool(env_url)
if inheriting and not env_url.startswith("sqlite") and config.attributes.get("connection") is None:
    raise SystemExit(
        "[abort] DATABASE_URL 看起来是远程库（scheme=%s），裸 alembic 命令不许对它发 DDL。"
        " 要真的动远程库：显式设 ALEMBIC_DATABASE_URL，"
        "或走 scripts/run_migrations.py（它会先自报 [库] 再发）。"
        % env_url.split("://", 1)[0]
    )
target_url = explicit_url or (env_url if inheriting else "")
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
