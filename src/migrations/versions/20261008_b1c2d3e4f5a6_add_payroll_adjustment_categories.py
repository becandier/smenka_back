"""payroll_breakdown: категории ручных начислений

Revision ID: b1c2d3e4f5a6
Revises: 56fddf4416fa
Create Date: 2026-10-08 00:00:00.000000+00:00

1. Новая таблица `payroll_adjustment_categories` — справочник категорий
   начислений организации (soft-delete по ADR-003). Частичный уникальный индекс
   `uq_payroll_adjustment_categories_org_name` по `(organization_id, lower(name))
   WHERE is_deleted = false` — имя уникально среди живых категорий организации
   без учёта регистра; после удаления имя можно занять снова.
2. `payroll_adjustments.category_id` — nullable, без default, FK `ON DELETE SET
   NULL` + индекс. `NULL` = «Без категории».

Только аддитивно, без backfill: существующие начисления остаются с
`category_id = NULL` (угадывание по `reason` запрещено ТЗ). Добавление
nullable-колонки без default в PG 16 — изменение только каталога, без
переписывания таблицы; блокировка кратковременная. Индекс по новой колонке
строится по таблице, где у всех строк NULL, — таблица `payroll_adjustments` на
проде маленькая, обычный `CREATE INDEX` не даёт заметной паузы записи.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "56fddf4416fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payroll_adjustment_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deleted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_payroll_adjustment_categories_org_is_deleted",
        "payroll_adjustment_categories",
        ["organization_id", "is_deleted"],
        unique=False,
    )
    op.create_index(
        "uq_payroll_adjustment_categories_org_name",
        "payroll_adjustment_categories",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    op.add_column(
        "payroll_adjustments",
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "payroll_adjustments_category_id_fkey",
        "payroll_adjustments",
        "payroll_adjustment_categories",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_payroll_adjustments_category_id",
        "payroll_adjustments",
        ["category_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_payroll_adjustments_category_id", table_name="payroll_adjustments")
    op.drop_constraint(
        "payroll_adjustments_category_id_fkey", "payroll_adjustments", type_="foreignkey"
    )
    op.drop_column("payroll_adjustments", "category_id")
    op.drop_index(
        "uq_payroll_adjustment_categories_org_name",
        table_name="payroll_adjustment_categories",
    )
    op.drop_index(
        "ix_payroll_adjustment_categories_org_is_deleted",
        table_name="payroll_adjustment_categories",
    )
    op.drop_table("payroll_adjustment_categories")
