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


def _report_target(dry_run=False) -> str:
    """不连线地报出目标库：`create_engine` 只把 URL 焊进对象，不发任何请求。

    `--dry-run` 那一支要说的是"本来会发给谁"，不是"我正在发给谁" ——
    同一句"会向它发 DDL"印在一次什么都不做的运行里，就是把措辞写得比动作更危险
    （本仓为这类话已经被抓过三次：`--base` 指到本机却自称线上生产库、
    `ok=True` 却印"已写入"、失败分支报"已还原 N 行"）。
    """
    from src.models.database import DB_TYPE, engine
    from src.services.verdict_evidence import database_label

    verb = '本来会是这个目标（本次 --dry-run：不发 DDL）' if dry_run else 'alembic upgrade head 会向它发 DDL'
    return '[库] %s —— %s（方言 %s）' % (database_label(engine), verb, DB_TYPE)


def _migration_gate(strict=True, versions_dir=None, allowlist=None) -> None:
    """**发 DDL 之前**先核一遍迁移文件本身（第 45 轮 B-M-2 / A-M-8 的最后一环）。

    上一轮我把"upgrade 那一支不许偷偷删结构"写成了 pytest 里的一条判据，而每次真正
    apply 迁移的是这个脚本（`render.yaml:11` 的 startCommand，Render 每次启动都跑）——
    它从不跑测试，也就不读那张名单 ⇒ 那句话只在"有人记得跑 pytest"时成立。
    现在判据搬到 `scripts/migration_policy.py`（**一份实现，两边共用**），这里在连线之前调用它：
    有未登记的丢数据迁移就退 4，一句 DDL 都不发。`--dry-run` 那一支不连线，所以只报不拦。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from migration_policy import REL_ALLOWLIST, audit

    problems, seen = audit(versions_dir, allowlist)
    print('[迁移] 已核对 %d 支：其中 %d 处未登记的危险动作' % (seen, len(problems)), flush=True)
    for line in problems:
        print('[abort] %s' % line if strict else '[警告] %s' % line, flush=True)
    if problems and strict:
        # 印的是仓库相对路径而不是 `os.path.relpath(绝对路径, 仓库根)`：
        # 后者在 Windows 上跨盘符会直接 ValueError（第 46 轮 B 的用例把名单指到 C: 的
        # 临时目录就撞上了）—— 一句"提示该怎么登记"的话，不许把闸门本身弄崩。
        print('[提示] 真要上这一支：把它连同"为什么非丢不可"写进 %s（结构变更要先问老板）'
              % REL_ALLOWLIST, flush=True)
        raise SystemExit(4)


def run_migrations() -> None:
    # 顺序是硬的：**先核对迁移文件，再 import ORM**。`src.models.database` 顶层就
    # `create_engine(DATABASE_URL)`，而在本仓 `from src.anything import …` 会先执行
    # `src/__init__.py`（它 import 了 `src.fund`）⇒ 引擎按 `.env`（＝生产）当场焊死。
    # 所以这道闸必须排在它前面，否则"核对完再决定要不要动手"就成了空话。
    _migration_gate()
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
        _migration_gate(strict=False)       # 只报不拦：这一支压根不连线
        _prepare_database_url()
        print(_report_target(dry_run=True), flush=True)
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
