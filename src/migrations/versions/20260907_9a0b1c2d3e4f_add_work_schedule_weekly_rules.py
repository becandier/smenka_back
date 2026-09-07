"""Add optional weekly windows to work schedules."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9a0b1c2d3e4f"
down_revision: str | None = "7f8a9b0c1d2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_schedule_weekly_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=True),
        sa.Column("end_time", sa.Time(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_schedule_id"], ["work_schedules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_schedule_id", "weekday", name="uq_work_schedule_weekly_rule"),
    )
    op.create_index(
        "ix_work_schedule_weekly_rules_work_schedule_id",
        "work_schedule_weekly_rules",
        ["work_schedule_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_work_schedule_weekly_rules_work_schedule_id", table_name="work_schedule_weekly_rules"
    )
    op.drop_table("work_schedule_weekly_rules")
