"""unified_soft_delete: единый мягкий delete вместо is_archived/is_deleted-зоопарка

Revision ID: 8e42f2b643ea
Revises: c6d7e8f90013
Create Date: 2026-08-16 12:00:00.000000+00:00

Приводит `checklist_templates`/`test_templates` к тому же паттерну, что уже
есть у `shifts`/`penalties`/`payroll_adjustments` (`is_deleted` +
`deleted_at` + `deleted_by_user_id`): `is_archived` переименовывается в
`is_deleted`, добавляются `deleted_at`/`deleted_by_user_id`.

`work_schedules.is_archived` переименовывается в `is_paused` — там это не
удаление, а временное выключение (график остаётся в списке и включается
обратно), см. ADR-003 и backend.md фичи `unified_soft_delete`.

`organization_penalty_templates` и `organizations` получают `deleted_at`/
`deleted_by_user_id` (поле `is_deleted` у них уже было).

Backfill не требуется: значения флагов переносятся как есть (ALTER TABLE ...
RENAME COLUMN не трогает данные), `deleted_at`/`deleted_by_user_id` у уже
удалённых записей остаются NULL — на проде на момент миграции удалённых
записей в затрагиваемых таблицах нет (см. backend.md).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8e42f2b643ea"
down_revision: str | None = "c6d7e8f90013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- checklist_templates: is_archived → is_deleted + deleted_at/deleted_by ---
    op.alter_column(
        "checklist_templates", "is_archived", new_column_name="is_deleted"
    )
    op.execute(
        "ALTER INDEX ix_checklist_templates_is_archived "
        "RENAME TO ix_checklist_templates_is_deleted"
    )
    op.add_column(
        "checklist_templates",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "checklist_templates",
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_checklist_templates_deleted_by_user_id_users",
        "checklist_templates",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- test_templates: is_archived → is_deleted + deleted_at/deleted_by ------
    op.alter_column("test_templates", "is_archived", new_column_name="is_deleted")
    op.execute(
        "ALTER INDEX ix_test_templates_is_archived RENAME TO ix_test_templates_is_deleted"
    )
    op.add_column(
        "test_templates",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "test_templates",
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_test_templates_deleted_by_user_id_users",
        "test_templates",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- work_schedules: is_archived → is_paused (временное выключение, не удаление) ---
    op.alter_column("work_schedules", "is_archived", new_column_name="is_paused")

    # --- organization_penalty_templates: + deleted_at/deleted_by ---------------
    op.add_column(
        "organization_penalty_templates",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "organization_penalty_templates",
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_organization_penalty_templates_deleted_by_user_id_users",
        "organization_penalty_templates",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # --- organizations: + deleted_at/deleted_by ---------------------------------
    op.add_column(
        "organizations",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_organizations_deleted_by_user_id_users",
        "organizations",
        "users",
        ["deleted_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # --- organizations -----------------------------------------------------------
    op.drop_constraint(
        "fk_organizations_deleted_by_user_id_users", "organizations", type_="foreignkey"
    )
    op.drop_column("organizations", "deleted_by_user_id")
    op.drop_column("organizations", "deleted_at")

    # --- organization_penalty_templates -------------------------------------------
    op.drop_constraint(
        "fk_organization_penalty_templates_deleted_by_user_id_users",
        "organization_penalty_templates",
        type_="foreignkey",
    )
    op.drop_column("organization_penalty_templates", "deleted_by_user_id")
    op.drop_column("organization_penalty_templates", "deleted_at")

    # --- work_schedules ------------------------------------------------------------
    op.alter_column("work_schedules", "is_paused", new_column_name="is_archived")

    # --- test_templates --------------------------------------------------------------
    op.drop_constraint(
        "fk_test_templates_deleted_by_user_id_users", "test_templates", type_="foreignkey"
    )
    op.drop_column("test_templates", "deleted_by_user_id")
    op.drop_column("test_templates", "deleted_at")
    op.execute(
        "ALTER INDEX ix_test_templates_is_deleted RENAME TO ix_test_templates_is_archived"
    )
    op.alter_column("test_templates", "is_deleted", new_column_name="is_archived")

    # --- checklist_templates -----------------------------------------------------
    op.drop_constraint(
        "fk_checklist_templates_deleted_by_user_id_users",
        "checklist_templates",
        type_="foreignkey",
    )
    op.drop_column("checklist_templates", "deleted_by_user_id")
    op.drop_column("checklist_templates", "deleted_at")
    op.execute(
        "ALTER INDEX ix_checklist_templates_is_deleted "
        "RENAME TO ix_checklist_templates_is_archived"
    )
    op.alter_column(
        "checklist_templates", "is_deleted", new_column_name="is_archived"
    )
