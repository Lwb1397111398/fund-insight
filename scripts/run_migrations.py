"""Run all Alembic migrations against the application's configured database."""
import argparse
import logging
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger(__name__)
MIGRATION_LOCK_ID = 724_601_937


def _prepare_database_url() -> None:
    explicit_url = os.getenv("ALEMBIC_DATABASE_URL")
    if explicit_url:
        os.environ["DATABASE_URL"] = explicit_url
    elif os.getenv("APP_ENV", "").lower() == "production" and not os.getenv("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is required when running migrations in production")


def _parser():
    """**这个脚本一执行就是发 DDL**，所以 `--help` 与坏旗子必须什么都还没干就退出。

    第 43 轮 B 席在评审里真实踩中了这条：它为了"看这个脚本有什么参数"跑了
    `python scripts/run_migrations.py --help` 与 `--totally-bogus-flag` —— 当时没有任何参数层，
    两句都直接进 `run_migrations()`，按 `.env`（＝生产 Supabase）连线并执行了 `upgrade head`。
    事后只读核对：生产 `alembic_version` 本来就等于仓库唯一 head
    `add_sector_mapping_keywords`，所以是一次 no-op（`q.py --production "select version_num from
    alembic_version"` 复核过），但它**确实取过并放掉了那把 advisory lock**。
    这是同一个缺陷类的第三次（第 39 轮 `mutation_proof_frontend.py`、第 42 轮两个 L3 脚本），
    而这次落在后果最重的那个文件上。

    为什么裸跑仍然照旧执行迁移：`render.yaml:11` 的 startCommand 就是
    `python scripts/run_migrations.py`（无参数）—— 把默认改成"必须给旗子"要同时改部署配置，
    那是老板的决定项（任务 #57）。这条 argparse 只保证一件事：**看帮助、打错字不发 DDL**。
    """
    ap = argparse.ArgumentParser(
        description='把 alembic 迁移跑到 head（Render 每次启动也跑它）',
        epilog='目标库取 ALEMBIC_DATABASE_URL；没给就沿用进程/.env 的 DATABASE_URL'
               '（本地 `.env` 那条＝生产 Supabase）。只想看会动谁，用 --dry-run。')
    ap.add_argument('--dry-run', action='store_true',
                    help='只报"会往哪个库发 upgrade head"然后退 2 —— 不连线、不发 DDL')
    return ap


def _report_target() -> str:
    """不连线地报出目标库：`create_engine` 只把 URL 焊进对象，不发任何请求。"""
    from src.models.database import DB_TYPE, engine
    from src.services.verdict_evidence import database_label

    return '[库] %s —— alembic upgrade head 会向它发 DDL（方言 %s）' % (
        database_label(engine), DB_TYPE)


def run_migrations() -> None:
    _prepare_database_url()
    from src.models.database import DB_TYPE, engine
    from src.services.verdict_evidence import database_label

    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    # 报名字要**排在连线与 DDL 之前**并且当场冲刷：stdout 进管道时是块缓冲，
    # 而 alembic 走 logging（stderr）⇒ 不 flush 的话，出事时最想知道的那一行
    # 会排在它承诺领先的那件事后面（第 43 轮 B-M4）。
    print(_report_target(), flush=True)
    with engine.connect() as connection:
        locked = False
        try:
            if DB_TYPE == "postgresql":
                connection.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID})
                locked = True
            alembic_config.attributes["connection"] = connection
            command.upgrade(alembic_config, "head")
        finally:
            if locked:
                connection.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": MIGRATION_LOCK_ID})

    logger.info("Database migrations completed (%s)", DB_TYPE)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    if args.dry_run:
        _prepare_database_url()
        print(_report_target(), flush=True)
        print('[dry-run] 只报目标，没连线、没发 DDL（要真跑就去掉 --dry-run；'
              'Render 的 startCommand 就是无参数那一支）')
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        run_migrations()
    except Exception as exc:
        logger.error("Database migration failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
