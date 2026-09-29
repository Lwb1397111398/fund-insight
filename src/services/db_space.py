"""删除后回收磁盘空间。

PostgreSQL 删行只是给行盖「已死」戳（MVCC），文件不变小；autovacuum 把空间
还给表内部而非磁盘。要真正缩小文件必须 VACUUM FULL（重写表 + 释放旧文件），
代价是 ACCESS EXCLUSIVE 锁 + 过程中双份空间。

因此策略是：小表直接 VACUUM FULL；超过阈值降级为普通 VACUUM（不锁写、不缩文件，
但阻止继续膨胀）。SQLite 的 VACUUM 本身就会重建文件并缩小，不区分。

VACUUM 不能在事务里跑，必须拿 autocommit 连接。
"""
from __future__ import annotations

import logging
import os
import re
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 超过这个行数不做 VACUUM FULL，避免长时间锁表
DEFAULT_FULL_VACUUM_MAX_ROWS = 500_000

_SAFE_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def full_vacuum_max_rows() -> int:
    try:
        return int(os.getenv("VACUUM_FULL_MAX_ROWS", DEFAULT_FULL_VACUUM_MAX_ROWS))
    except ValueError:
        return DEFAULT_FULL_VACUUM_MAX_ROWS


def space_reclaim_enabled() -> bool:
    """默认开启；设 ENABLE_SPACE_RECLAIM=false 可关闭。"""
    return os.getenv("ENABLE_SPACE_RECLAIM", "true").lower() != "false"


def failure_detail(result: Dict) -> str:
    """「没跑成」时原因出自哪里，只有一个答案。

    SQLite 整库 VACUUM 把原因写在顶层 error；Postgres 是按表 VACUUM 的，
    原因只在 tables[表].error 里。两处消费方（路由那句回执、页面上那一栏）
    必须读到同一句话，否则同一件事有两个口径。
    """
    error = result.get("error")
    if error:
        return str(error)
    failing = [
        f"{name}：{entry.get('error')}"
        for name, entry in (result.get("tables") or {}).items()
        if isinstance(entry, dict) and not entry.get("success")
    ]
    return "; ".join(failing) or "数据库没给出原因"


# 「这次没跑」那句人话也只有一个家，和上面那把尺子并排放。以前它写在路由里
# （`_RECLAIM_SKIP_SENTENCES`），于是同一个 `no_tables` 在独立按钮那一路是人话、
# 在清理任务那一路被原样搬上屏幕 —— 同一件事两个口径。
_SKIP_SENTENCES = {
    "ENABLE_SPACE_RECLAIM=false": "空间回收没跑：回收开关（ENABLE_SPACE_RECLAIM）是关着的",
    "no_tables": "空间回收没跑：这次没有要回收的表",
    "in_memory_database": "空间回收没跑：内存数据库没有磁盘文件可回收",
}


def skip_detail(result: Dict) -> str:
    """「没跑」时页面上要出现的那句话，全仓只在这里拼一次。"""
    reason = str(result.get("reason") or "")
    if reason.startswith("unsupported_dialect:"):
        return f"空间回收没跑：{reason.split(':', 1)[1]} 这种数据库暂不支持回收"
    return _SKIP_SENTENCES.get(reason, f"空间回收没跑：{reason}")


def _skipped(reason: str, tables: Optional[Dict] = None, **extra) -> Dict:
    """所有「没跑」的出口都从这一处出 —— 那句人话因此不可能漏。

    `reason` 是给机器看的键，`reason_text` 是给页面看的话：两格都在。
    但两个消费方拿的不是同一格 —— 清理任务那一路把 `reason_text` 整份带给页面，
    独立按钮那一路（路由）现问 `skip_detail(result)`。所以"翻译"只有一处实现，
    而不是"只有一格字"：谁也不许自己再拼一遍那句话。
    """
    result: Dict = {"skipped": True, "reason": reason, "tables": tables or {}, **extra}
    result["reason_text"] = skip_detail(result)
    return result


def reclaim_space(
    db: Session,
    tables: Sequence[str],
    *,
    force_full: Optional[bool] = None,
) -> Dict:
    """对指定表回收空间，返回每张表的处理方式。

    Args:
        db: 只用来取 bind / dialect，实际 VACUUM 走独立 autocommit 连接
        tables: 表名（白名单校验，拒绝非法标识符）
        force_full: None=按行数自动决定；True/False=强制
    """
    if not space_reclaim_enabled():
        return _skipped("ENABLE_SPACE_RECLAIM=false")

    unique_tables = [t for t in dict.fromkeys(tables) if t]
    unsafe = [t for t in unique_tables if not _SAFE_TABLE.match(t)]
    if unsafe:
        raise ValueError(f"unsafe table names for vacuum: {unsafe}")
    if not unique_tables:
        return _skipped("no_tables")

    bind = db.get_bind()
    dialect = bind.dialect.name
    # VACUUM 不能在事务里跑：先释放调用方 session 持有的事务
    try:
        db.commit()
    except Exception:
        db.rollback()
    if dialect == "sqlite":
        return _vacuum_sqlite(bind, unique_tables)
    if dialect in ("postgresql", "postgres"):
        return _vacuum_postgres(bind, unique_tables, force_full=force_full)
    return _skipped(f"unsupported_dialect:{dialect}")


def _vacuum_sqlite(bind, tables: List[str]) -> Dict:
    """SQLite 只能整库 VACUUM，会重建文件并释放页。

    注意：VACUUM 完成后，操作系统上的文件大小要等**所有**连接关闭才收缩
    （尤其 Windows），所以用 `pragma page_count * page_size` 度量逻辑大小——
    这个值 VACUUM 后立即生效，是真实的「已释放」口径。
    """
    from sqlalchemy import create_engine

    url = bind.url
    database = getattr(url, "database", None)
    if not database or database == ":memory:":
        # 一个"没跑"的结果不许同时带 success —— 那是两句互相打脸的话。
        return _skipped(
            "in_memory_database",
            {t: "database-wide" for t in tables},
            dialect="sqlite",
            mode="database",
        )
    engine = None
    before = after = None
    try:
        engine = create_engine(url.render_as_string(hide_password=False))
        with engine.connect() as conn:
            raw = conn.connection
            # sqlite3 驱动默认隐式开事务，VACUUM 必须在 autocommit 下执行
            raw.isolation_level = None
            before = _sqlite_logical_size(conn)
            conn.exec_driver_sql("VACUUM")
            after = _sqlite_logical_size(conn)
    except Exception as exc:
        logger.warning("sqlite VACUUM 失败: %s", exc)
        return {
            "dialect": "sqlite",
            "mode": "database",
            "success": False,
            "error": str(exc),
            "tables": {t: "database-wide" for t in tables},
        }
    finally:
        if engine is not None:
            engine.dispose()
    return {
        "dialect": "sqlite",
        "mode": "database",
        "success": True,
        "bytes_before": before,
        "bytes_after": after,
        "bytes_freed": (before - after) if (before and after) else None,
        "file_bytes": _file_size(database),
        "note": "文件在所有连接关闭后收缩；bytes_* 为数据库逻辑大小",
        "tables": {t: "database-wide" for t in tables},
    }


def _sqlite_logical_size(conn) -> Optional[int]:
    try:
        pages = conn.exec_driver_sql("PRAGMA page_count").scalar()
        page_size = conn.exec_driver_sql("PRAGMA page_size").scalar()
        if pages is None or page_size is None:
            return None
        return int(pages) * int(page_size)
    except Exception:
        return None


def _vacuum_postgres(bind, tables: List[str], *, force_full: Optional[bool]) -> Dict:
    max_rows = full_vacuum_max_rows()
    result: Dict[str, Dict] = {}
    total_freed = 0
    engine = bind.engine if hasattr(bind, "engine") else bind
    # AUTOCOMMIT：VACUUM 不允许出现在事务块里
    autocommit_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
    with autocommit_engine.connect() as conn:
        for table in tables:
            entry: Dict = {}
            try:
                size_before = _pg_table_size(conn, table)
                live_rows = _pg_live_rows(conn, table)
                use_full = (
                    force_full
                    if force_full is not None
                    else (live_rows is None or live_rows <= max_rows)
                )
                statement = (
                    f'VACUUM (FULL, ANALYZE) "{table}"'
                    if use_full
                    else f'VACUUM (ANALYZE) "{table}"'
                )
                conn.exec_driver_sql(statement)
                size_after = _pg_table_size(conn, table)
                freed = (
                    size_before - size_after
                    if size_before is not None and size_after is not None
                    else None
                )
                if freed:
                    total_freed += max(0, freed)
                entry = {
                    "mode": "full" if use_full else "plain",
                    "success": True,
                    "live_rows": live_rows,
                    "bytes_before": size_before,
                    "bytes_after": size_after,
                    "bytes_freed": freed,
                }
                if not use_full:
                    entry["note"] = (
                        f"行数 {live_rows} 超过 VACUUM_FULL_MAX_ROWS={max_rows}，"
                        "降级为普通 VACUUM（不缩文件，避免长时间锁表）"
                    )
            except Exception as exc:
                logger.warning("postgres VACUUM %s 失败: %s", table, exc)
                entry = {"mode": "failed", "success": False, "error": str(exc)}
            result[table] = entry
    succeeded = all(e.get("success") for e in result.values())
    payload: Dict = {
        "dialect": "postgresql",
        "mode": "per-table",
        "success": succeeded,
        "bytes_freed": total_freed or None,
        "full_vacuum_max_rows": max_rows,
        "tables": result,
    }
    if not succeeded:
        # 按表失败也要在顶层留一句原因：页面上那一栏读的是顶层 error，
        # 只把它留在 tables 里等于"数据库答了、页面上问不出来"。
        payload["error"] = failure_detail(payload)
    return payload


def _pg_table_size(conn, table: str) -> Optional[int]:
    try:
        row = conn.exec_driver_sql(
            "SELECT pg_total_relation_size(%(name)s::regclass)", {"name": table}
        ).scalar()
        return int(row) if row is not None else None
    except Exception:
        return None


def _pg_live_rows(conn, table: str) -> Optional[int]:
    try:
        row = conn.exec_driver_sql(f'SELECT count(*) FROM "{table}"').scalar()
        return int(row) if row is not None else None
    except Exception:
        return None


def _file_size(path: Optional[str]) -> Optional[int]:
    if not path or path == ":memory:":
        return None
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def format_bytes(value: Optional[int]) -> str:
    if not value:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
