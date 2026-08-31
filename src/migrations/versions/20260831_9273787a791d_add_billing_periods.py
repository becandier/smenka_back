"""online_payments: add billing_periods (справочник периодов продажи и скидки)

Revision ID: 9273787a791d
Revises: cd0999882d68
Create Date: 2026-08-31 10:00:00.000000+00:00

Периоды продажи (1/3/6 месяцев) и скидка за предоплату — отдельная таблица, а
не константы в коде (backend.md фичи online_payments, «Домен»): набор
периодов и размер скидки — маркетинговый параметр, меняется чаще цен планов.
Годового периода в v1 нет сознательно — лимит магазина ЮKassa 100 000 ₽/мес
на оборот.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9273787a791d"
down_revision: str | None = "cd0999882d68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

billing_periods_table = sa.table(
    "billing_periods",
    sa.column("months", sa.Integer),
    sa.column("discount_percent", sa.Integer),
    sa.column("is_active", sa.Boolean),
    sa.column("sort_order", sa.Integer),
)


def upgrade() -> None:
    op.create_table(
        "billing_periods",
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("discount_percent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("months"),
    )

    op.bulk_insert(
        billing_periods_table,
        [
            {"months": 1, "discount_percent": 0, "is_active": True, "sort_order": 10},
            {"months": 3, "discount_percent": 5, "is_active": True, "sort_order": 20},
            {"months": 6, "discount_percent": 10, "is_active": True, "sort_order": 30},
        ],
    )


def downgrade() -> None:
    op.drop_table("billing_periods")
