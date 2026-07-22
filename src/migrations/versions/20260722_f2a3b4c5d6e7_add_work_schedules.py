"""work_schedules: графики работы, авто-финиш по графику, переработки

Revision ID: f2a3b4c5d6e7
Revises: 352f7e3148af
Create Date: 2026-07-22 15:00:00.000000+00:00

Порядок операций — по backend.md фичи work_schedules:
1. `organizations.timezone` (NOT NULL, default 'Europe/Moscow').
2. `work_schedules` + индекс по `organization_id`.
3. Привязки `work_schedule_roles` / `work_schedule_locations` /
   `work_schedule_member_overrides` (калька чек-листов).
4. `shifts`: снимок графика (`work_schedule_id`/`schedule_name`/`scheduled_start_at`/
   `scheduled_end_at`), `finish_reason` (nullable) + частичный индекс по
   `scheduled_end_at` (только active/paused — под выборку Celery).
5. `shift_overtime_requests` + частичный уникальный индекс (максимум одна
   pending/approved заявка на смену).
6. `organization_settings`: новые поля авто-финиша/опоздания/переработки;
   `auto_finish_hours` дропается (заменён `auto_finish_by_schedule`).

Прод: снять дамп БД перед прогоном (удаляется колонка `auto_finish_hours`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "352f7e3148af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. organizations.timezone
    op.add_column(
        "organizations",
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default="Europe/Moscow",
            nullable=False,
        ),
    )

    # 2. work_schedules
    op.create_table(
        "work_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_work_schedules_organization_id", "work_schedules", ["organization_id"]
    )

    # 3. Привязки (калька чек-листов). Тип `scheduleoverridetype` создаётся
    # автоматически событием create_table на колонке ниже (тип новый, ещё не
    # существует) — предварительный CREATE TYPE здесь не нужен (в отличие от
    # add_column к существующей таблице, см. finish_reason ниже).
    scheduleoverridetype = sa.Enum("add", "remove", name="scheduleoverridetype")

    op.create_table(
        "work_schedule_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["work_schedules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["organization_roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("schedule_id", "role_id", name="uq_work_schedule_role"),
    )
    op.create_index("ix_work_schedule_roles_schedule_id", "work_schedule_roles", ["schedule_id"])
    op.create_index("ix_work_schedule_roles_role_id", "work_schedule_roles", ["role_id"])

    op.create_table(
        "work_schedule_locations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("work_location_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["work_schedules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["work_location_id"], ["work_locations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "work_location_id", name="uq_work_schedule_location"
        ),
    )
    op.create_index(
        "ix_work_schedule_locations_schedule_id", "work_schedule_locations", ["schedule_id"]
    )
    op.create_index(
        "ix_work_schedule_locations_work_location_id",
        "work_schedule_locations",
        ["work_location_id"],
    )

    op.create_table(
        "work_schedule_member_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("schedule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("override_type", scheduleoverridetype, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["work_schedules.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["organization_members.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "member_id", name="uq_work_schedule_member_override"
        ),
    )
    op.create_index(
        "ix_work_schedule_member_overrides_schedule_id",
        "work_schedule_member_overrides",
        ["schedule_id"],
    )
    op.create_index(
        "ix_work_schedule_member_overrides_member_id",
        "work_schedule_member_overrides",
        ["member_id"],
    )

    # 4. shifts: снимок графика + finish_reason
    shiftfinishreason = sa.Enum("manual", "auto_schedule", name="shiftfinishreason")
    shiftfinishreason.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "shifts",
        sa.Column("work_schedule_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "shifts",
        sa.Column("schedule_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "shifts",
        sa.Column("scheduled_start_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "shifts",
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "shifts",
        sa.Column("finish_reason", shiftfinishreason, nullable=True),
    )
    op.create_foreign_key(
        "fk_shifts_work_schedule_id",
        "shifts",
        "work_schedules",
        ["work_schedule_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_shifts_work_schedule_id", "shifts", ["work_schedule_id"])
    op.create_index(
        "ix_shifts_scheduled_end_at",
        "shifts",
        ["scheduled_end_at"],
        postgresql_where=sa.text("status IN ('active', 'paused')"),
    )

    # 5. shift_overtime_requests (новый тип создаётся событием create_table)
    overtimerequeststatus = sa.Enum(
        "pending", "approved", "rejected", name="overtimerequeststatus"
    )

    op.create_table(
        "shift_overtime_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shift_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("comment", sa.String(length=500), nullable=False),
        sa.Column(
            "status",
            overtimerequeststatus,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("reviewed_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_comment", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["shift_id"], ["shifts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_shift_overtime_requests_shift_id", "shift_overtime_requests", ["shift_id"]
    )
    op.create_index(
        "uq_shift_overtime_requests_active",
        "shift_overtime_requests",
        ["shift_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'approved')"),
    )

    # 6. organization_settings
    op.add_column(
        "organization_settings",
        sa.Column(
            "auto_finish_by_schedule",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "organization_settings",
        sa.Column(
            "require_schedule",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "organization_settings",
        sa.Column(
            "late_tolerance_minutes",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "organization_settings",
        sa.Column(
            "overtime_request_days",
            sa.Integer(),
            server_default=sa.text("7"),
            nullable=False,
        ),
    )
    op.drop_column("organization_settings", "auto_finish_hours")


def downgrade() -> None:
    # 6. organization_settings — auto_finish_hours восстанавливается nullable,
    # значения теряются (это допустимо и осознано, см. backend.md).
    op.add_column(
        "organization_settings",
        sa.Column("auto_finish_hours", sa.Integer(), nullable=True),
    )
    op.drop_column("organization_settings", "overtime_request_days")
    op.drop_column("organization_settings", "late_tolerance_minutes")
    op.drop_column("organization_settings", "require_schedule")
    op.drop_column("organization_settings", "auto_finish_by_schedule")

    # 5. shift_overtime_requests
    op.drop_index(
        "uq_shift_overtime_requests_active", table_name="shift_overtime_requests"
    )
    op.drop_index("ix_shift_overtime_requests_shift_id", table_name="shift_overtime_requests")
    op.drop_table("shift_overtime_requests")
    sa.Enum(name="overtimerequeststatus").drop(op.get_bind(), checkfirst=True)

    # 4. shifts
    op.drop_index("ix_shifts_scheduled_end_at", table_name="shifts")
    op.drop_index("ix_shifts_work_schedule_id", table_name="shifts")
    op.drop_constraint("fk_shifts_work_schedule_id", "shifts", type_="foreignkey")
    op.drop_column("shifts", "finish_reason")
    op.drop_column("shifts", "scheduled_end_at")
    op.drop_column("shifts", "scheduled_start_at")
    op.drop_column("shifts", "schedule_name")
    op.drop_column("shifts", "work_schedule_id")
    sa.Enum(name="shiftfinishreason").drop(op.get_bind(), checkfirst=True)

    # 3. привязки
    op.drop_index(
        "ix_work_schedule_member_overrides_member_id",
        table_name="work_schedule_member_overrides",
    )
    op.drop_index(
        "ix_work_schedule_member_overrides_schedule_id",
        table_name="work_schedule_member_overrides",
    )
    op.drop_table("work_schedule_member_overrides")
    sa.Enum(name="scheduleoverridetype").drop(op.get_bind(), checkfirst=True)

    op.drop_index(
        "ix_work_schedule_locations_work_location_id", table_name="work_schedule_locations"
    )
    op.drop_index(
        "ix_work_schedule_locations_schedule_id", table_name="work_schedule_locations"
    )
    op.drop_table("work_schedule_locations")

    op.drop_index("ix_work_schedule_roles_role_id", table_name="work_schedule_roles")
    op.drop_index("ix_work_schedule_roles_schedule_id", table_name="work_schedule_roles")
    op.drop_table("work_schedule_roles")

    # 2. work_schedules
    op.drop_index("ix_work_schedules_organization_id", table_name="work_schedules")
    op.drop_table("work_schedules")

    # 1. organizations.timezone
    op.drop_column("organizations", "timezone")
