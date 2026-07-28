"""Run all Alembic migrations against the application's configured database."""
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


def run_migrations() -> None:
    _prepare_database_url()
    from src.models.database import DB_TYPE, engine

    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
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


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        run_migrations()
    except Exception as exc:
        logger.error("Database migration failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
