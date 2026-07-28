"""Add the sector fund mapping review flag."""

from alembic import context, op
import sqlalchemy as sa


revision = "add_sector_mapping_reviewed"
down_revision = "add_fund_history_quality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set()
    if not context.is_offline_mode():
        inspector = sa.inspect(bind)
        if "sector_fund_mapping" not in inspector.get_table_names():
            return
        existing = {
            column["name"] for column in inspector.get_columns("sector_fund_mapping")
        }
    if "reviewed" not in existing:
        op.add_column(
            "sector_fund_mapping",
            sa.Column(
                "reviewed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not context.is_offline_mode():
        inspector = sa.inspect(bind)
        if "sector_fund_mapping" not in inspector.get_table_names():
            return
        existing = {
            column["name"] for column in inspector.get_columns("sector_fund_mapping")
        }
        if "reviewed" not in existing:
            return
    op.drop_column("sector_fund_mapping", "reviewed")
