"""Add immutable prediction change logs."""

from alembic import context, op
import sqlalchemy as sa


revision = "add_prediction_change_logs"
down_revision = "prediction_schema_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if (
        not context.is_offline_mode()
        and "prediction_change_logs" in sa.inspect(bind).get_table_names()
    ):
        return

    op.create_table(
        "prediction_change_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("prediction_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=False),
        sa.Column("after_state", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["predictions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_prediction_change_logs_prediction_id",
        "prediction_change_logs",
        ["prediction_id"],
    )
    op.create_index(
        "ix_prediction_change_logs_created_at",
        "prediction_change_logs",
        ["created_at"],
    )
    op.create_index(
        "ix_prediction_change_logs_action",
        "prediction_change_logs",
        ["action"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        not context.is_offline_mode()
        and "prediction_change_logs" not in sa.inspect(bind).get_table_names()
    ):
        return
    op.drop_table("prediction_change_logs")
