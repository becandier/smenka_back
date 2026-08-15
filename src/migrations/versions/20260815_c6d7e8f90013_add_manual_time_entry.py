"""manual_time_entry: shifts ручной ввод/правка/удаление + payroll_adjustments

Revision ID: c6d7e8f90013
Revises: b4c5d6e70012
Create Date: 2026-08-15 09:00:00.000000+00:00

Ручной ввод/правка смены администратором (`created_by_user_id`,
`edited_by_user_id`/`edited_at`, `manual_note`) и soft-delete
(`deleted_by_user_id`/`deleted_at` — использует уже существующую
`shifts.is_deleted`, заготовку фичи `fines`). Все колонки nullable без
бэкфилла: существующие смены остаются с NULL, трактуются как «созданы
сотрудником, не редактировались».

Новая таблица `payroll_adjustments` — ручное начисление/удержание, знаковая
сумма в копейках (`amount_minor != 0`, CHECK на уровне БД — defense in depth
поверх валидации на уровне схемы/сервиса).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6d7e8f90013"
down_revision: str | None = "b4c5d6e70012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("shifts", sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("shifts", sa.Column("edited_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("shifts", sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("shifts", sa.Column("manual_note", sa.String(length=500), nullable=True))
    op.add_column("shifts", sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("shifts", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_shifts_created_by_user_id_users",
        "shifts",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_shifts_edited_by_user_id_users",
        "shifts",
        "users",
        ["edited_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_shifts_deleted_by_user_id_users",
        "shifts",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "payroll_adjustments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shift_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), server_default="RUB", nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("comment", sa.String(length=500), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["member_id"], ["organization_members.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shift_id"], ["shifts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["deleted_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("amount_minor != 0", name="ck_payroll_adjustments_amount_nonzero"),
    )
    op.create_index(
        "ix_payroll_adjustments_org_is_deleted",
        "payroll_adjustments",
        ["organization_id", "is_deleted"],
    )
    op.create_index(
        "ix_payroll_adjustments_member_is_deleted",
        "payroll_adjustments",
        ["member_id", "is_deleted"],
    )
    op.create_index(
        "ix_payroll_adjustments_occurred_at", "payroll_adjustments", ["occurred_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_payroll_adjustments_occurred_at", table_name="payroll_adjustments")
    op.drop_index("ix_payroll_adjustments_member_is_deleted", table_name="payroll_adjustments")
    op.drop_index("ix_payroll_adjustments_org_is_deleted", table_name="payroll_adjustments")
    op.drop_table("payroll_adjustments")

    op.drop_constraint("fk_shifts_deleted_by_user_id_users", "shifts", type_="foreignkey")
    op.drop_constraint("fk_shifts_edited_by_user_id_users", "shifts", type_="foreignkey")
    op.drop_constraint("fk_shifts_created_by_user_id_users", "shifts", type_="foreignkey")
    op.drop_column("shifts", "deleted_at")
    op.drop_column("shifts", "deleted_by_user_id")
    op.drop_column("shifts", "manual_note")
    op.drop_column("shifts", "edited_at")
    op.drop_column("shifts", "edited_by_user_id")
    op.drop_column("shifts", "created_by_user_id")
