"""online_payments: add subscription_events.payment_id

Revision ID: 682bd9857504
Revises: 580829a12fff
Create Date: 2026-08-31 10:10:00.000000+00:00

`subscription_events.type` остаётся обычным VARCHAR без CHECK (как и раньше в
tariffs) — новые значения `paid_online`/`payment_refunded`
(`models/subscription.py::SubscriptionEventType`) не требуют миграции схемы,
только этой колонки-связки с породившим событие платежом. У ручных
продлений/правок супер-админа `payment_id` остаётся NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "682bd9857504"
down_revision: str | None = "580829a12fff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscription_events",
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscription_events_payment_id",
        "subscription_events",
        "payments",
        ["payment_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_subscription_events_payment_id", "subscription_events", type_="foreignkey"
    )
    op.drop_column("subscription_events", "payment_id")
