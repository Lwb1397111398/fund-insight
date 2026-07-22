from pathlib import Path
from io import StringIO

from sqlalchemy import create_engine, inspect, text


def test_alembic_adds_and_removes_prediction_change_log_on_existing_database(tmp_path):
    from alembic import command
    from alembic.config import Config

    database_path = tmp_path / "existing.db"
    database_url = f"sqlite:///{database_path.as_posix()}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE predictions (id INTEGER PRIMARY KEY)"))

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
