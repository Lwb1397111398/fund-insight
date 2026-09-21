"""Add sector-fund agent evidence columns.

给"板块→基金 agent"落地留证据：哪来的（match_source）、是直接对应还是替代标的
（match_kind，老板确认部分映射属"没有对口基金，取关联度最大的替代"）、置信度、
验证时间/结论、LLM 理由。同时给 sector_alias 补来源、给预测变更日志补批次号
（批量重映射要能整批回滚，原表没有任何 batch/run 标识）。
"""

from alembic import context, op
import sqlalchemy as sa


revision = "add_sector_mapping_agent_columns"
down_revision = "add_sector_mapping_timestamps"
branch_labels = None
depends_on = None


ADDITIONS = {
    "sector_fund_mapping": (
        ("match_source", sa.String(20)),
        ("match_kind", sa.String(12)),
        ("confidence", sa.Float()),
        ("verified_at", sa.DateTime()),
        ("verify_message", sa.Text()),
        ("llm_reason", sa.Text()),
    ),
    "sector_alias": (
        ("source", sa.String(20)),
    ),
    "prediction_change_logs": (
        ("run_id", sa.String(40)),
    ),
}


def _columns(table):
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return None
    return {column["name"] for column in inspector.get_columns(table)}


def _indexes(table):
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    for table, columns in ADDITIONS.items():
        existing = _columns(table)
        if existing is None:
            continue
        for name, type_ in columns:
            if name not in existing:
                with op.batch_alter_table(table) as batch_op:
                    batch_op.add_column(sa.Column(name, type_, nullable=True))
    # 判"要不要建索引"必须看索引名：run_id 这一列正是本迁移上面刚加的，
    # 重新 inspect 列名会以为"早就存在"，于是索引永远漏建（第 4 轮评审 MINOR）
    need_index = (_columns("prediction_change_logs") is not None
                  and "ix_prediction_change_logs_run_id"
                  not in _indexes("prediction_change_logs"))
    if need_index:
        op.create_index(
            "ix_prediction_change_logs_run_id", "prediction_change_logs", ["run_id"])


def downgrade() -> None:
    # 先删索引再删列：带着索引 DROP COLUMN 在部分数据库上会直接报错
    try:
        op.drop_index("ix_prediction_change_logs_run_id",
                      table_name="prediction_change_logs")
    except Exception:
        pass
    for table, columns in ADDITIONS.items():
        existing = _columns(table)
        if existing is None:
            continue
        for name, _ in reversed(columns):
            if name in existing:
                # SQLite 旧版本不支持 DROP COLUMN，必须走 batch_alter_table 重建表
                with op.batch_alter_table(table) as batch_op:
                    batch_op.drop_column(name)
