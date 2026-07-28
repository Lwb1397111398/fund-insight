"""Add sector fund mapping timestamps."""

from alembic import context, op
import sqlalchemy as sa


revision = "add_sector_mapping_timestamps"
down_revision = "add_sector_mapping_reviewed"
branch_labels = None
depends_on = None


TIMESTAMP_COLUMNS = ("created_at", "updated_at")


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
    for name in TIMESTAMP_COLUMNS:
        if name not in existing:
            op.add_column(
                "sector_fund_mapping",
                sa.Column(name, sa.DateTime(), nullable=True),
            )


def downgrade() -> None:
    bind = op.get_bind()
    existing = None
    if not context.is_offline_mode():
        inspector = sa.inspect(bind)
        if "sector_fund_mapping" not in inspector.get_table_names():
            return
        existing = {
            column["name"] for column in inspector.get_columns("sector_fund_mapping")
        }
    for name in reversed(TIMESTAMP_COLUMNS):
        if existing is None or name in existing:
            op.drop_column("sector_fund_mapping", name)
