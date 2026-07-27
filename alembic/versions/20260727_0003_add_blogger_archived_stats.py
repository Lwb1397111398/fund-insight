"""Add archived blogger verification totals."""

from alembic import context, op
import sqlalchemy as sa


revision = "add_blogger_archived_stats"
down_revision = "add_prediction_change_logs"
branch_labels = None
depends_on = None


ARCHIVED_COLUMNS = (
    ("archived_verified_count", sa.Integer(), "0"),
    ("archived_correct_count", sa.Integer(), "0"),
    ("archived_verify_score", sa.Float(), "0"),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set()
    if not context.is_offline_mode():
        inspector = sa.inspect(bind)
        if "bloggers" not in inspector.get_table_names():
            return
        existing = {column["name"] for column in inspector.get_columns("bloggers")}

    for name, column_type, default in ARCHIVED_COLUMNS:
        if name not in existing:
            op.add_column(
                "bloggers",
                sa.Column(
                    name,
                    column_type,
                    nullable=False,
                    server_default=sa.text(default),
                ),
            )


def downgrade() -> None:
    bind = op.get_bind()
    existing = None
    if not context.is_offline_mode():
        inspector = sa.inspect(bind)
        if "bloggers" not in inspector.get_table_names():
            return
        existing = {column["name"] for column in inspector.get_columns("bloggers")}

    for name, _, _ in reversed(ARCHIVED_COLUMNS):
        if existing is None or name in existing:
            op.drop_column("bloggers", name)
