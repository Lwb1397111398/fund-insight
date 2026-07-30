"""删除后回收磁盘空间：SQLite VACUUM 缩库、表名白名单、开关。"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, FundHistory
from src.services.db_space import (
    format_bytes,
    full_vacuum_max_rows,
    reclaim_space,
    space_reclaim_enabled,
)


def _file_db(tmp_path, rows: int = 4000):
    path = tmp_path / "space.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    from datetime import date, timedelta

    base = date(2026, 1, 1)
    session.bulk_save_objects([
        FundHistory(
            fund_code=f"F{i % 20:03d}",
            fund_name="空间测试基金",
            nav_date=base + timedelta(days=i // 20),
            nav=1.0 + i / 10000,
        )
        for i in range(rows)
    ])
    session.commit()
    return engine, session, path


def _logical_size(session) -> int:
    pages = session.execute(text("PRAGMA page_count")).scalar()
    page_size = session.execute(text("PRAGMA page_size")).scalar()
    return int(pages) * int(page_size)


def test_sqlite_vacuum_shrinks_database_after_delete(tmp_path):
    engine, session, path = _file_db(tmp_path)
    try:
        before = _logical_size(session)
        session.query(FundHistory).filter(FundHistory.nav_date < "2026-06-01").delete(
            synchronize_session=False
        )
        session.commit()
        # 删除本身不缩库
        assert _logical_size(session) == before

        result = reclaim_space(session, ["fund_history"])

        assert result["success"] is True
        assert result["dialect"] == "sqlite"
        assert result["bytes_freed"] > 0
        assert result["bytes_after"] < result["bytes_before"]
        assert _logical_size(session) < before
    finally:
        session.close()
        engine.dispose()


def test_reclaim_space_rejects_unsafe_table_names(tmp_path):
    engine, session, path = _file_db(tmp_path, rows=10)
    try:
        with pytest.raises(ValueError) as excinfo:
            reclaim_space(session, ['fund_history"; DROP TABLE predictions; --'])
        assert "unsafe table names" in str(excinfo.value)
    finally:
        session.close()
        engine.dispose()


def test_reclaim_space_can_be_disabled_by_env(tmp_path, monkeypatch):
    engine, session, path = _file_db(tmp_path, rows=10)
    monkeypatch.setenv("ENABLE_SPACE_RECLAIM", "false")
    try:
        result = reclaim_space(session, ["fund_history"])
        assert result["skipped"] is True
        assert result["reason"] == "ENABLE_SPACE_RECLAIM=false"
    finally:
        session.close()
        engine.dispose()


def test_reclaim_space_noop_without_tables(tmp_path):
    engine, session, path = _file_db(tmp_path, rows=10)
    try:
        assert reclaim_space(session, [])["reason"] == "no_tables"
    finally:
        session.close()
        engine.dispose()


def test_in_memory_database_is_skipped(test_db):
    result = reclaim_space(test_db, ["fund_history"])
    assert result["skipped"] is True
    assert result["reason"] == "in_memory_database"


def test_space_reclaim_switch_defaults_on(monkeypatch):
    monkeypatch.delenv("ENABLE_SPACE_RECLAIM", raising=False)
    assert space_reclaim_enabled() is True


def test_full_vacuum_threshold_reads_env(monkeypatch):
    monkeypatch.setenv("VACUUM_FULL_MAX_ROWS", "12345")
    assert full_vacuum_max_rows() == 12345
    monkeypatch.setenv("VACUUM_FULL_MAX_ROWS", "not-a-number")
    assert full_vacuum_max_rows() == 500_000


def test_format_bytes_is_human_readable():
    assert format_bytes(0) == "0 B"
    assert format_bytes(None) == "0 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(5 * 1024 * 1024) == "5.0 MB"
