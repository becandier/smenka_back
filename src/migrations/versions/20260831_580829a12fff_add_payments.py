"""online_payments: add payments (онлайн-платежи через ЮKassa)

Revision ID: 580829a12fff
Revises: 9273787a791d
Create Date: 2026-08-31 10:05:00.000000+00:00

Финансовый след — записи не удаляются (backend.md, «Записи не удаляются»).
`applied_at` — защита от повторного применения одного и того же платежа к
подписке (идемпотентность вебхука/поллинга, `services/billing.py::apply_payment`).
`uq_payments_provider_payment_id` — частичный уникальный индекс (только когда
`provider_payment_id` заполнен): у платежа, ещё не подтверждённого созданием
на стороне ЮKassa (или упавшего до этого шага), это поле NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "580829a12fff"
down_revision: str | None = "9273787a791d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("plan_code", sa.String(length=32), nullable=False),
        sa.Column("months", sa.Integer(), nullable=True),
        sa.Column("base_amount_minor", sa.Integer(), nullable=False),
        sa.Column("discount_percent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="RUB", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("provider", sa.String(length=16), server_default="yookassa", nullable=False),
        sa.Column("provider_payment_id", sa.String(length=64), nullable=True),
        sa.Column("idempotence_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("confirmation_url", sa.String(length=1024), nullable=True),
        sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("subscription_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cancellation_reason", sa.String(length=64), nullable=True),
        sa.Column("provider_payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_code"], ["plans.code"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["subscription_event_id"], ["subscription_events.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_payments_org_created",
        "payments",
        ["organization_id", "created_at"],
    )
    op.create_index("ix_payments_status", "payments", ["status"])
    op.create_index(
        "uq_payments_provider_payment_id",
        "payments",
        ["provider_payment_id"],
        unique=True,
        postgresql_where=sa.text("provider_payment_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_payments_provider_payment_id", table_name="payments")
    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_index("ix_payments_org_created", table_name="payments")
    op.drop_table("payments")
