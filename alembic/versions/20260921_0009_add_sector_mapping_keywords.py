"""Add the sector_fund_mapping.keywords column (agent 关键词检索轨迹).

第 4 轮评审核实：`keywords` 在模型里（`SectorFundMapping.keywords = Column(JSON)`）、
本地镜像库也有（当年 `create_all` 建的），但**没有任何一个迁移加过它**。
生产上表本身是 create_all 建的，所以列在不在取决于那一次建表；S6 上线前必须
按 `information_schema` 实测，缺就靠这一条补上。
"""

from alembic import context, op
import sqlalchemy as sa


revision = "add_sector_mapping_keywords"
down_revision = "add_sector_mapping_owner_lock"
branch_labels = None
depends_on = None


def _columns(table):
    if context.is_offline_mode():
        return set()
    inspector = sa.inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return None
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    existing = _columns("sector_fund_mapping")
    if existing is None or "keywords" in existing:
        return
    with op.batch_alter_table("sector_fund_mapping") as batch_op:
        batch_op.add_column(sa.Column("keywords", sa.JSON(), nullable=True))


def downgrade() -> None:
    existing = _columns("sector_fund_mapping")
    if existing is None or "keywords" not in existing:
        return
    with op.batch_alter_table("sector_fund_mapping") as batch_op:
        batch_op.drop_column("keywords")
