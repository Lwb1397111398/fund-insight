"""数据导入导出服务。

这里承载 JSON 备份格式的兼容逻辑，路由层只负责 HTTP 收发。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Callable, Dict, Iterable, List, Sequence

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, JSON, Numeric, String, Text, text
from sqlalchemy.orm import Session

from src.models.database import (
    AdviceReasoning,
    AnalysisLog,
    BatchAnalysisTask,
    Blogger,
    CleanupItemLog,
    CleanupLog,
    CleanupTask,
    CrawlerArticleRecord,
    FundHistory,
    FundInfo,
    InvestmentAdvice,
    Post,
    Prediction,
    PredictionChangeLog,
    PredictionGroup,
    SectorAlias,
    SectorFundMapping,
    VerificationTask,
    Viewpoint,
)
from src.models.database import SystemConfig

logger = logging.getLogger(__name__)

IMPORT_JOB_KEY = "data_import_job"


@dataclass(frozen=True)
class TableSpec:
    export_key: str
    model: Any
    identity_fields: Sequence[str]
    exclude_fields: Sequence[str] = ()


EXPORT_VERSION = "1.3"


TABLE_SPECS: Sequence[TableSpec] = (
    TableSpec("bloggers", Blogger, ("id",)),
    TableSpec("fund_info", FundInfo, ("fund_code",)),
    TableSpec("posts", Post, ("id",)),
    TableSpec("predictions", Prediction, ("id",), exclude_fields=("llm_raw_response",)),
    TableSpec("prediction_change_logs", PredictionChangeLog, ("id",)),
    TableSpec("batch_analysis_tasks", BatchAnalysisTask, ("id",)),
    TableSpec("analysis_logs", AnalysisLog, ("id",)),
    TableSpec("verification_tasks", VerificationTask, ("id",)),
    TableSpec("prediction_groups", PredictionGroup, ("id",)),
    TableSpec("viewpoints", Viewpoint, ("id",)),
    TableSpec("crawler_article_records", CrawlerArticleRecord, ("article_id",)),
    TableSpec("fund_history", FundHistory, ("fund_code", "nav_date")),
    TableSpec("sector_alias", SectorAlias, ("alias_name",)),
    TableSpec("sector_fund_mapping", SectorFundMapping, ("sector_name", "fund_code")),
    TableSpec("investment_advice", InvestmentAdvice, ("id",)),
)


class DataPortabilityService:
    """处理 Fund Insight JSON 备份的导出与合并导入。"""

    def __init__(self, db: Session):
        self.db = db

    def export_data(self) -> Dict[str, Any]:
        exported: Dict[str, Any] = {
            "export_version": EXPORT_VERSION,
            "export_date": datetime.now().isoformat(),
        }

        for spec in TABLE_SPECS:
            exported[spec.export_key] = [
                self._serialize_row(row, spec.exclude_fields)
                for row in self.db.query(spec.model).all()
            ]

        exported["summary"] = {
            spec.export_key: len(exported[spec.export_key])
            for spec in TABLE_SPECS
        }
        return exported

    def import_data(
        self,
        data: Dict[str, Any],
        replace: bool = False,
        progress_cb: Callable[[str, int, int], None] = None,
    ) -> Dict[str, Any]:
        """导入 JSON 备份。

        Args:
            data: 导出格式的 JSON 对象
            replace: True 时先清空所有白名单表再导入（覆盖模式）；
                     False 为合并模式（按 natural key 跳过已存在记录）。
            progress_cb: 可选进度回调 (table_key, done_rows, total_rows)，每张表
                完成与每 500 行时调用。注意：回调必须用独立连接写状态，
                不能触碰主事务会话（任何 commit 都会提前提交导入事务）。
        """
        imported = {spec.export_key: 0 for spec in TABLE_SPECS}
        skipped = {spec.export_key: 0 for spec in TABLE_SPECS}
        failed = {spec.export_key: 0 for spec in TABLE_SPECS}
        created_dependencies = {"fund_info": 0}
        warnings: List[str] = []

        try:
            if not isinstance(data, dict):
                raise ValueError("导入数据必须是 JSON 对象")

            unsupported_keys = sorted(
                set(data.keys()) - {spec.export_key for spec in TABLE_SPECS} - {"export_version", "export_date", "summary"}
            )
            for key in unsupported_keys:
                warnings.append(f"忽略未知数据区块: {key}")

            if replace:
                # 覆盖模式：清空所有白名单表。
                # 用独立直连执行 TRUNCATE，并临时放大该连接的 statement_timeout。
                # 原因：Supabase 直连默认 statement_timeout=8s，而 TRUNCATE ...
                # CASCADE 需要拿 19 张表的排他锁，线上有并发查询时等待锁超过 8s
                # 会被数据库直接 cancel；在连接级 SET 一个更宽容的超时后即可完成。
                # 清完立即 COMMIT 释放锁，再走会话内的导入（整体仍在同一事务，
                # 导入失败只回滚导入，不会把已清空的表恢复——这正是覆盖语义）。
                bind = self.db.get_bind()
                dialect_name = bind.dialect.name if bind is not None else ""
                if dialect_name == "postgresql":
                    tables = [spec.model.__tablename__ for spec in TABLE_SPECS]
                    # advice_reasoning 引用 investment_advice；cleanup_* 三张引用
                    # bloggers/posts/predictions。这些表不在导出范围内但必须一并清掉，
                    # 否则 CASCADE 清主表时会被外键拖住。
                    tables += [
                        "advice_reasoning",
                        "cleanup_item_logs",
                        "cleanup_logs",
                        "cleanup_tasks",
                    ]
                    engine = bind.engine if hasattr(bind, "engine") else bind
                    self._truncate_postgres(engine, tables)
                else:
                    # SQLite 无 TRUNCATE，逐表删（仅本地/测试用，量级小）
                    self.db.query(AdviceReasoning).delete(synchronize_session=False)
                    self.db.query(CleanupItemLog).delete(synchronize_session=False)
                    self.db.query(CleanupLog).delete(synchronize_session=False)
                    self.db.query(CleanupTask).delete(synchronize_session=False)
                    for spec in reversed(TABLE_SPECS):
                        self.db.query(spec.model).delete(synchronize_session=False)
                    self.db.flush()
                warnings.append("覆盖模式：已清空原有数据后导入。")

            with self.db.no_autoflush:
                for spec in TABLE_SPECS:
                    rows = data.get(spec.export_key, [])
                    if rows is None:
                        rows = []
                    if not isinstance(rows, list):
                        raise ValueError(f"{spec.export_key} 必须是数组")

                    if spec.export_key == "sector_fund_mapping":
                        # 映射表依赖 fund_info.fund_code。旧版备份可能含有未同步净值的映射，
                        # 先补齐最小基金记录，才能在保留用户映射的同时满足外键约束。
                        self.db.flush()
                        created_dependencies["fund_info"] += self._create_mapping_fund_dependencies(
                            spec,
                            rows,
                            warnings,
                        )

                    imported_count = 0
                    skipped_count = 0
                    for index, item in enumerate(rows, start=1):
                        if not isinstance(item, dict):
                            raise ValueError(f"{spec.export_key} 第 {index} 行必须是对象")

                        cleaned = self._clean_row(spec, item)
                        # 覆盖模式：表已在进入循环前被 TRUNCATE/清空，逐行 _find_existing
                        # 必为 None，纯属每行一次网络往返（线上实测 9.6k 行要 2+ 小时）。
                        # 直接跳过，用无 SELECT 的批量 INSERT 灌入。
                        if not replace and self._find_existing(spec, cleaned) is not None:
                            skipped_count += 1
                            continue

                        self.db.add(spec.model(**cleaned))
                        imported_count += 1

                        # 每 200 行向数据库 flush 一批：
                        # 1. 尽早暴露外键/约束错误（不必等全部 add 完）；
                        # 2. 给导入日志提供"活着"的信号，避免大批量时看起来卡死。
                        if imported_count % 200 == 0:
                            self.db.flush()
                            logger.info(
                                f"[Import] {spec.export_key} 已写入 {imported_count}/{len(rows)}"
                            )
                            if progress_cb and imported_count % 500 == 0:
                                progress_cb(spec.export_key, index, len(rows))

                    if progress_cb:
                        progress_cb(spec.export_key, len(rows), len(rows))

                    imported[spec.export_key] = imported_count
                    skipped[spec.export_key] = skipped_count
                    failed[spec.export_key] = 0

            self.db.flush()
            self._reset_sequences()
            self.db.commit()
            return self._success_response(
                imported,
                skipped,
                failed,
                created_dependencies,
                warnings,
            )
        except Exception as exc:
            self.db.rollback()
            warnings.append("导入事务已回滚，本次没有写入任何数据。")
            return self._failure_response(
                str(exc),
                imported,
                skipped,
                failed,
                created_dependencies,
                warnings,
            )

    # ========== 后台任务状态（用于大数据量覆盖导入） ==========

    @staticmethod
    def _truncate_postgres(engine, tables: Sequence[str]) -> None:
        """清表：先杀掉持锁的僵尸会话，再逐表 TRUNCATE（带重试）。

        线上发现两类阻塞源：
        1. Web 服务连接泄漏出的 idle-in-transaction 会话（持 AccessShareLock
           且长时间不结束），直接 pg_terminate_backend 清掉；
        2. 正常的并发查询，逐表 TRUNCATE 只需等该表当前的锁释放即可，
           比一次性 TRUNCATE 19 张表（需要同时拿到所有排他锁）成功率高得多。
        """
        table_list = ", ".join(f'"{t}"' for t in tables)

        def _kill_blockers(conn) -> int:
            rows = conn.execute(text("""
                SELECT a.pid, now() - a.query_start AS age
                FROM pg_locks l
                JOIN pg_stat_activity a ON a.pid = l.pid
                WHERE l.relation::regclass::text = ANY(:tables)
                  AND a.state = 'idle in transaction'
                  AND a.pid <> pg_backend_pid()
                  AND now() - a.query_start > interval '60 seconds'
            """), {"tables": list(tables)}).fetchall()
            for row in rows:
                logger.warning(f"[Import] 终止僵尸事务会话 pid={row[0]} age={row[1]}")
                conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": row[0]})
            return len(rows)

        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = '180s'"))
            conn.execute(text("SET lock_timeout = '90s'"))
            killed = _kill_blockers(conn)
            if killed:
                conn.commit()
                time.sleep(1)  # 等被杀会话真正释放锁

        # 逐表 TRUNCATE：每张表独立重试，避免"19 张表同时拿排他锁"的强条件
        for tbl in tables:
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    with engine.connect() as conn:
                        conn.execute(text("SET statement_timeout = '180s'"))
                        conn.execute(text("SET lock_timeout = '90s'"))
                        conn.execute(text(f'TRUNCATE TABLE "{tbl}" RESTART IDENTITY CASCADE'))
                        conn.commit()
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    message = str(exc).lower()
                    retriable = "lock timeout" in message or "statement timeout" in message
                    if not retriable or attempt == 3:
                        raise
                    logger.warning(f"[Import] TRUNCATE {tbl} 第 {attempt} 次等锁超时，重试")
                    # 再清一次可能新出现的僵尸会话，然后退避
                    try:
                        with engine.connect() as conn:
                            _kill_blockers(conn)
                            conn.commit()
                    except Exception:
                        pass
                    time.sleep(attempt * 2)
            if last_error is not None:
                raise last_error
        logger.info(f"[Import] 覆盖模式清表完成: {table_list}")

    def get_import_job_status(self) -> Dict[str, Any]:
        """读取导入任务状态（持久化在 system_config，跨请求可见）。"""
        row = self.db.query(SystemConfig).filter(
            SystemConfig.config_key == IMPORT_JOB_KEY
        ).first()
        if not row or not row.config_value:
            return {"status": "idle"}
        try:
            return json.loads(row.config_value)
        except Exception:
            return {"status": "idle"}

    def _set_import_job_status(self, payload: Dict[str, Any]):
        row = self.db.query(SystemConfig).filter(
            SystemConfig.config_key == IMPORT_JOB_KEY
        ).first()
        if row:
            row.config_value = json.dumps(payload, ensure_ascii=False)
        else:
            row = SystemConfig(
                config_key=IMPORT_JOB_KEY,
                config_value=json.dumps(payload, ensure_ascii=False),
                description="数据导入后台任务状态",
            )
            self.db.add(row)
        self.db.commit()

    def run_import_background(self, data: Dict[str, Any], replace: bool) -> None:
        """后台线程执行导入，进度写入 system_config 供前端轮询。"""
        try:
            job_started_at = datetime.now().isoformat()
            self._set_import_job_status({
                "status": "running",
                "replace": replace,
                "started_at": job_started_at,
                "message": "正在清空并导入数据...",
            })

            def progress_cb(table_key: str, done_rows: int, total_rows: int) -> None:
                # 独立连接写进度：主事务会话在导入期间绝不能被 commit，
                # 否则会提前提交导入事务、破坏整体回滚的原子性。
                try:
                    bind = self.db.get_bind()
                    engine = bind.engine if hasattr(bind, "engine") else bind
                    with engine.connect() as conn:
                        conn.execute(text(
                            "UPDATE system_config SET config_value = :value "
                            "WHERE config_key = :key"
                        ), {
                            "value": json.dumps({
                                "status": "running",
                                "replace": replace,
                                "started_at": job_started_at,
                                "current_table": table_key,
                                "processed_rows": done_rows,
                                "total_rows": total_rows,
                                "message": f"正在导入 {table_key} {done_rows}/{total_rows} 行...",
                            }, ensure_ascii=False),
                            "key": IMPORT_JOB_KEY,
                        })
                        conn.commit()
                except Exception:
                    pass

            result = self.import_data(data, replace=replace, progress_cb=progress_cb)
            self._set_import_job_status({
                "status": "done" if result.get("success") else "failed",
                "replace": replace,
                "finished_at": datetime.now().isoformat(),
                "result": result,
            })
        except Exception as exc:
            logger.exception("[Import] 后台导入异常")
            try:
                self._set_import_job_status({
                    "status": "failed",
                    "replace": replace,
                    "finished_at": datetime.now().isoformat(),
                    "result": {"success": False, "message": f"后台导入异常: {exc}"},
                })
            except Exception:
                pass

    def _create_mapping_fund_dependencies(
        self,
        spec: TableSpec,
        rows: Sequence[Dict[str, Any]],
        warnings: List[str],
    ) -> int:
        mappings = [self._clean_row(spec, row) for row in rows if isinstance(row, dict)]
        mapping_codes = {
            mapping.get("fund_code")
            for mapping in mappings
            if mapping.get("fund_code")
        }
        if not mapping_codes:
            return 0

        existing_codes = {
            code
            for (code,) in self.db.query(FundInfo.fund_code)
            .filter(FundInfo.fund_code.in_(mapping_codes))
            .all()
        }
        created = 0

        for mapping in mappings:
            fund_code = mapping.get("fund_code")
            if not fund_code or fund_code in existing_codes:
                continue

            self.db.add(FundInfo(
                fund_code=fund_code,
                fund_name=mapping.get("fund_name"),
                sector_type=mapping.get("sector_name"),
                data_quality="recovery_placeholder",
                data_quality_note="Created during backup import for sector mapping dependency.",
                can_delete=False,
            ))
            existing_codes.add(fund_code)
            created += 1

        if created:
            warnings.append(
                f"已为 {created} 条板块映射补齐缺失的基金基础记录；请后续同步基金净值。"
            )
        return created

    def _clean_row(self, spec: TableSpec, item: Dict[str, Any]) -> Dict[str, Any]:
        columns = {column.name: column for column in spec.model.__table__.columns}
        cleaned = {}

        for key, value in item.items():
            column = columns.get(key)
            if column is None or key in spec.exclude_fields:
                continue
            cleaned[key] = self._coerce_value(value, column)

        return cleaned

    def _find_existing(self, spec: TableSpec, cleaned: Dict[str, Any]) -> Any:
        filters = []
        for field in spec.identity_fields:
            if cleaned.get(field) is None:
                return None
            filters.append(getattr(spec.model, field) == cleaned[field])
        return self.db.query(spec.model).filter(*filters).first()

    def _reset_sequences(self) -> None:
        bind = self.db.get_bind()
        dialect_name = bind.dialect.name if bind is not None else ""
        id_tables = [
            spec.model.__tablename__
            for spec in TABLE_SPECS
            if "id" in spec.model.__table__.columns
        ]

        if dialect_name == "postgresql":
            for table_name in id_tables:
                self.db.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {table_name}), 1), "
                    f"COALESCE((SELECT MAX(id) FROM {table_name}) IS NOT NULL, false))"
                ))
        elif dialect_name == "sqlite":
            try:
                existing_sequences = {
                    row[0]
                    for row in self.db.execute(text("SELECT name FROM sqlite_sequence")).fetchall()
                }
            except Exception:
                return
            for table_name in id_tables:
                if table_name in existing_sequences:
                    self.db.execute(text(
                        f"UPDATE sqlite_sequence SET seq = "
                        f"COALESCE((SELECT MAX(id) FROM {table_name}), 0) "
                        f"WHERE name = :table_name"
                    ), {"table_name": table_name})

    def _success_response(
        self,
        imported: Dict[str, int],
        skipped: Dict[str, int],
        failed: Dict[str, int],
        created_dependencies: Dict[str, int],
        warnings: List[str],
    ) -> Dict[str, Any]:
        total_imported = sum(imported.values())
        total_skipped = sum(skipped.values())
        total_failed = sum(failed.values())

        if total_imported > 0:
            message = f"导入完成，共导入 {total_imported} 条记录"
            if total_skipped > 0:
                message += f"（跳过 {total_skipped} 条已存在记录）"
        else:
            message = f"无新数据导入（{total_skipped} 条已存在）"

        return {
            "success": True,
            "message": message,
            "data": {
                "imported": imported,
                "skipped": skipped,
                "failed": failed,
                "total_imported": total_imported,
                "total_skipped": total_skipped,
                "total_failed": total_failed,
                "created_dependencies": created_dependencies,
                "total_created_dependencies": sum(created_dependencies.values()),
                "warnings": warnings,
            },
        }

    def _failure_response(
        self,
        error: str,
        imported: Dict[str, int],
        skipped: Dict[str, int],
        failed: Dict[str, int],
        created_dependencies: Dict[str, int],
        warnings: List[str],
    ) -> Dict[str, Any]:
        rolled_back_count = sum(imported.values())
        rolled_back_imported = {key: 0 for key in imported}
        return {
            "success": False,
            "message": f"导入失败: {error}",
            "data": {
                "imported": rolled_back_imported,
                "skipped": skipped,
                "failed": failed,
                "total_imported": 0,
                "total_skipped": sum(skipped.values()),
                "total_failed": sum(failed.values()),
                "created_dependencies": {key: 0 for key in created_dependencies},
                "total_created_dependencies": 0,
                "rolled_back": True,
                "total_rolled_back": rolled_back_count,
                "warnings": warnings,
            },
        }

    @staticmethod
    def _serialize_row(obj: Any, exclude_fields: Iterable[str] = ()) -> Dict[str, Any]:
        exclude = set(exclude_fields)
        row = {}
        for column in obj.__table__.columns:
            if column.name in exclude:
                continue
            value = getattr(obj, column.name)
            if isinstance(value, (date, datetime)):
                row[column.name] = value.isoformat()
            else:
                row[column.name] = value
        return row

    @staticmethod
    def _coerce_value(value: Any, column: Any) -> Any:
        if value is None:
            return None
        if value == "":
            return None

        column_type = column.type
        field_name = column.name

        if isinstance(column_type, DateTime):
            if isinstance(value, datetime):
                return value
            if isinstance(value, date):
                return datetime.combine(value, time.min)
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError(f"{field_name} 不是有效日期时间: {value}") from exc
            raise ValueError(f"{field_name} 不是有效日期时间: {value}")

        if isinstance(column_type, Date):
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            if isinstance(value, str):
                try:
                    return date.fromisoformat(value[:10])
                except ValueError as exc:
                    raise ValueError(f"{field_name} 不是有效日期: {value}") from exc
            raise ValueError(f"{field_name} 不是有效日期: {value}")

        if isinstance(column_type, Boolean):
            if isinstance(value, bool):
                return value
            if isinstance(value, int):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes", "y", "是"}
            return bool(value)

        if isinstance(column_type, JSON):
            if isinstance(value, str):
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value
            return value

        if isinstance(column_type, Integer) and not isinstance(value, bool):
            return int(value)

        if isinstance(column_type, Float):
            return float(value)

        if isinstance(column_type, Numeric):
            return Decimal(str(value))

        if isinstance(column_type, (String, Text)):
            return str(value)

        return value
