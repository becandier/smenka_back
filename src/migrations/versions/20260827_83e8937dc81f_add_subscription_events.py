"""tariffs: add subscription_events (append-only журнал изменений подписки)

Revision ID: 83e8937dc81f
Revises: 7ed3d5a9649d
Create Date: 2026-08-27 12:10:00.000000+00:00

Записи создаются, никогда не редактируются и не удаляются (backend.md,
«Каждое изменение подписки... пишется отдельной неизменяемой записью»).
Композитный индекс `(organization_id, created_at)` покрывает основной запрос
`GET .../subscription/events` (история одной организации, новые сверху).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "83e8937dc81f"
down_revision: str | None = "7ed3d5a9649d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscription_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(length=24), nullable=False),
        sa.Column("from_plan_code", sa.String(length=32), nullable=True),
        sa.Column("to_plan_code", sa.String(length=32), nullable=True),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=True),
        sa.Column("period_end_before", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_end_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("months", sa.Integer(), nullable=True),
        sa.Column("amount_minor", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=512), nullable=True),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_subscription_events_org_created",
        "subscription_events",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_subscription_events_org_created", table_name="subscription_events")
    op.drop_table("subscription_events")
