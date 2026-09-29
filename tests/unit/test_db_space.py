"""删除后回收磁盘空间：SQLite VACUUM 缩库、表名白名单、开关。"""
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.models.database import Base, FundHistory
from src.services.db_space import (
    _vacuum_postgres,
    failure_detail,
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


def test_a_per_table_failure_puts_its_reason_where_the_page_reads_it(tmp_path):
    """按表 VACUUM 失败时，原因必须同时出现在**顶层**——页面上那一栏读的是顶层 error。

    PostgreSQL 那条路只能按表跑（VACUUM 不能进事务块，也没有整库 VACUUM），所以它的
    失败逐条落在 `tables[表].error` 里。上一版顶层只有 `success: False`，于是
    "数据库明明答了、页面上问不出来"：那一栏会老实说"数据库没给出原因"，
    而生产（PostgreSQL）恰好就是这一路——本地 sqlite 反而看不见。

    这里拿 sqlite 引擎喂 `_vacuum_postgres`：那些 `pg_*` / `VACUUM (FULL, ANALYZE)`
    语句在 sqlite 上必然被**数据库自己**拒掉 ⇒ 走的正是"每张表各自失败"那一档，
    原因是驱动给的，不是我编的。成功那一档本仓两个库都跑不出（今天两处都没有 PG 实例），
    所以这一格只判"失败的原因有没有递到页面上"，不替它作保。
    """
    engine, session, path = _file_db(tmp_path, rows=20)
    try:
        result = _vacuum_postgres(engine, ["fund_history", "posts"], force_full=None)
        assert result["success"] is False
        assert result["mode"] == "per-table"
        failing = [name for name, e in result["tables"].items() if not e["success"]]
        assert failing == ["fund_history", "posts"]
        assert result["error"], "顶层没有 error ⇒ 页面那一栏只能报'数据库没给出原因'"
        assert "数据库没给出原因" not in result["error"]
        for name in failing:
            assert name in result["error"]
        # 顶层那句与那把尺子的答案必须是同一句（两处消费方读同一份原因）
        assert failure_detail(result) == result["error"]
    finally:
        session.close()
        engine.dispose()


def test_the_failure_reason_has_one_home_and_prefers_what_the_database_gave():
    """「这次没跑成的原因是什么」全仓只许一处回答，且三档各有各的话。"""
    # ① 顶层已经写了（sqlite 整库 VACUUM 那一路）⇒ 原样带出，不另拼一句
    assert failure_detail({"error": "database is locked"}) == "database is locked"
    # ② 只有按表的失败（PostgreSQL 那一路）⇒ 逐表点名，成功的表不进这句话
    per_table = {
        "tables": {
            "fund_history": {"success": False, "error": "cannot VACUUM inside a transaction"},
            "posts": {"success": True},
        }
    }
    assert failure_detail(per_table) == "fund_history：cannot VACUUM inside a transaction"
    # ③ 两边都拿不到 ⇒ 明说拿不到，不许编一句原因，也不许退化成"完成"
    assert failure_detail({"tables": {}}) == "数据库没给出原因"

