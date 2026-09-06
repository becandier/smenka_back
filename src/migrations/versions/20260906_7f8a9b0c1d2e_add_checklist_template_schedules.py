"""Add checklist template to work schedule assignments."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7f8a9b0c1d2e"
down_revision: str | None = "d43e672a012a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "checklist_template_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["template_id"], ["checklist_templates.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["work_schedule_id"], ["work_schedules.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_id", "work_schedule_id", name="uq_checklist_template_schedule"
        ),
    )
    op.create_index(
        "ix_checklist_template_schedules_template_id",
        "checklist_template_schedules",
        ["template_id"],
    )
    op.create_index(
        "ix_checklist_template_schedules_work_schedule_id",
        "checklist_template_schedules",
        ["work_schedule_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_checklist_template_schedules_work_schedule_id",
        table_name="checklist_template_schedules",
    )
    op.drop_index(
        "ix_checklist_template_schedules_template_id",
        table_name="checklist_template_schedules",
    )
    op.drop_table("checklist_template_schedules")
