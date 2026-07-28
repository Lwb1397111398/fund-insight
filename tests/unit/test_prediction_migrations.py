from pathlib import Path
from io import StringIO
import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text


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
    result = subprocess.run(
        [sys.executable, "scripts/run_migrations.py"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    columns = {column["name"] for column in inspect(engine).get_columns("bloggers")}
    assert "archived_verified_count" in columns


def test_migration_runner_fails_closed_in_production_without_database_url(tmp_path):
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env.pop("ALEMBIC_DATABASE_URL", None)
    env["APP_ENV"] = "production"
    result = subprocess.run(
        [sys.executable, "scripts/run_migrations.py"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "DATABASE_URL" in result.stderr
