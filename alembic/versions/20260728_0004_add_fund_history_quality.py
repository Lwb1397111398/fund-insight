"""Add fund history quality metadata columns."""

from alembic import context, op
import sqlalchemy as sa


revision = "add_fund_history_quality"
down_revision = "add_blogger_archived_stats"
branch_labels = None
depends_on = None


QUALITY_COLUMNS = (
    ("data_quality", sa.String(length=20), "'normal'"),
    ("quality_note", sa.String(length=200), None),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set()
    if not context.is_offline_mode():
        inspector = sa.inspect(bind)
        if "fund_history" not in inspector.get_table_names():
            return
        existing = {column["name"] for column in inspector.get_columns("fund_history")}

    for name, column_type, default in QUALITY_COLUMNS:
        if name not in existing:
            op.add_column(
                "fund_history",
                sa.Column(
                    name,
                    column_type,
                    nullable=True,
                    server_default=sa.text(default) if default else None,
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    existing = None
    if not context.is_offline_mode():
        inspector = sa.inspect(bind)
        if "fund_history" not in inspector.get_table_names():
            return
        existing = {column["name"] for column in inspector.get_columns("fund_history")}

    for name, _, _ in reversed(QUALITY_COLUMNS):
        if existing is None or name in existing:
            op.drop_column("fund_history", name)
