"""tariffs: add plans (справочник тарифов) + seed standard/premium

Revision ID: fe0f10f98052
Revises: ac0ce20ba98d
Create Date: 2026-08-27 12:00:00.000000+00:00

Справочник тарифов (backend.md фичи `tariffs`). Редактирование из админки в v1
не делаем — цена/лимиты/фичи меняются только миграцией. `code` — PK (не
surrogate uuid): значений всего два, они фигурируют как FK у `subscriptions.
plan_code`, и человекочитаемый код удобнее в отладке/сидах, чем произвольный uuid.

Сид: `standard` (5000 ₽ = 500000 копеек, 15 сотрудников, 3 точки, без штрафов и
импорта тестов), `premium` (10000 ₽ = 1000000 копеек, без лимитов, все фичи).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fe0f10f98052"
down_revision: str | None = "ac0ce20ba98d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

plans_table = sa.table(
    "plans",
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("price_minor", sa.Integer),
    sa.column("currency", sa.String),
    sa.column("max_employees", sa.Integer),
    sa.column("max_locations", sa.Integer),
    sa.column("feature_fines", sa.Boolean),
    sa.column("feature_test_import", sa.Boolean),
    sa.column("sort_order", sa.Integer),
    sa.column("is_active", sa.Boolean),
)


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("price_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="RUB", nullable=False),
        sa.Column("max_employees", sa.Integer(), nullable=True),
        sa.Column("max_locations", sa.Integer(), nullable=True),
        sa.Column("feature_fines", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "feature_test_import", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )

    op.bulk_insert(
        plans_table,
        [
            {
                "code": "standard",
                "name": "Стандарт",
                "price_minor": 500000,
                "currency": "RUB",
                "max_employees": 15,
                "max_locations": 3,
                "feature_fines": False,
                "feature_test_import": False,
                "sort_order": 10,
                "is_active": True,
            },
            {
                "code": "premium",
                "name": "Премиум",
                "price_minor": 1000000,
                "currency": "RUB",
                "max_employees": None,
                "max_locations": None,
                "feature_fines": True,
                "feature_test_import": True,
                "sort_order": 20,
                "is_active": True,
            },
        ],
    )


def downgrade() -> None:
    op.drop_table("plans")
