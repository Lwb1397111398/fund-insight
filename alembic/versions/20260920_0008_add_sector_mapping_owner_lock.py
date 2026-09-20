"""Add owner-lock and agent evidence payload columns.

0007 之后仍有两处不够用（第 2 轮评审 #2/#10）：
- 无法区分"老板手工挑定的有意代理"与"agent 写入的行"，agent 会把 债券→证券ETF、
  SpaceX→军工ETF 这类有意替代再改掉；
- 证据链（每一轮候选/验证/判定）没有落点，前端"证据可复核"就是空话。
"""

from alembic import context, op
import sqlalchemy as sa


revision = "add_sector_mapping_owner_lock"
down_revision = "add_sector_mapping_agent_columns"
branch_labels = None
depends_on = None


ADDITIONS = {
    "sector_fund_mapping": (
        ("is_fetchable", sa.Boolean()),
        ("evidence", sa.Text()),
        ("reviewed_by", sa.String(30)),
        ("owner_locked", sa.Boolean()),
    ),
}


def _columns(table):
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return None
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    for table, columns in ADDITIONS.items():
        existing = _columns(table)
        if existing is None:
            continue
        for name, type_ in columns:
            if name not in existing:
                with op.batch_alter_table(table) as batch_op:
                    batch_op.add_column(sa.Column(name, type_, nullable=True))
        if "owner_locked" not in existing:
            op.create_index("ix_sector_fund_mapping_owner_locked", table, ["owner_locked"])


def downgrade() -> None:
    for table, columns in ADDITIONS.items():
        existing = _columns(table)
        if existing is None:
            continue
        if "owner_locked" in existing:
            try:
                op.drop_index("ix_sector_fund_mapping_owner_locked", table_name=table)
            except Exception:
                pass
        for name, _ in reversed(columns):
            if name in existing:
                # SQLite 不支持 DROP COLUMN（旧版本），必须走 batch_alter_table 重建表
                with op.batch_alter_table(table) as batch_op:
                    batch_op.drop_column(name)
