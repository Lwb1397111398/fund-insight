"""数据导入导出服务。

这里承载 JSON 备份格式的兼容逻辑，路由层只负责 HTTP 收发。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Sequence

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, JSON, Numeric, String, Text, text
from sqlalchemy.orm import Session

from src.models.database import (
    Blogger,
    FundHistory,
    FundInfo,
    InvestmentAdvice,
    Post,
    Prediction,
    SectorAlias,
    SectorFundMapping,
    Viewpoint,
)


@dataclass(frozen=True)
class TableSpec:
    export_key: str
    model: Any
    identity_fields: Sequence[str]
    exclude_fields: Sequence[str] = ()


EXPORT_VERSION = "1.0"


TABLE_SPECS: Sequence[TableSpec] = (
    TableSpec("bloggers", Blogger, ("id",)),
    TableSpec("fund_info", FundInfo, ("fund_code",)),
    TableSpec("posts", Post, ("id",)),
    TableSpec("predictions", Prediction, ("id",), exclude_fields=("llm_raw_response",)),
    TableSpec("viewpoints", Viewpoint, ("id",)),
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

    def import_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
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
                        if self._find_existing(spec, cleaned) is not None:
                            skipped_count += 1
                            continue

                        self.db.add(spec.model(**cleaned))
                        imported_count += 1

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
