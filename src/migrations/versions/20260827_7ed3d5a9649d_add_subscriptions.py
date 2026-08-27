"""tariffs: add subscriptions (подписка организации на тариф)

Revision ID: 7ed3d5a9649d
Revises: fe0f10f98052
Create Date: 2026-08-27 12:05:00.000000+00:00

Одна подписка на организацию (UNIQUE `organization_id`). `status` хранит
ТОЛЬКО ручные состояния (`trialing`/`active`/`canceled`) — просрочка
(`past_due`/`suspended`) вычисляется на лету от дат (`services/entitlements.py`),
поэтому здесь обычный VARCHAR без CHECK/native enum (как `notifications.type`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7ed3d5a9649d"
down_revision: str | None = "fe0f10f98052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="trialing", nullable=False),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=512), nullable=True),
        sa.Column("last_expiry_notice_days", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_code"], ["plans.code"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", name="uq_subscription_org"),
    )
    op.create_index(
        "ix_subscriptions_current_period_end",
        "subscriptions",
        ["current_period_end"],
    )
    op.create_index(
        "ix_subscriptions_trial_ends_at",
        "subscriptions",
        ["trial_ends_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_trial_ends_at", table_name="subscriptions")
    op.drop_index("ix_subscriptions_current_period_end", table_name="subscriptions")
    op.drop_table("subscriptions")
