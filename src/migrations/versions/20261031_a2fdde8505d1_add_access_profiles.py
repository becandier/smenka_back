"""access_profiles: профили доступа организации (ADR-006)

Revision ID: a2fdde8505d1
Revises: b1c2d3e4f5a6
Create Date: 2026-10-31 00:00:00.000000+00:00

1. Новая таблица `organization_access_profiles` — именованные наборы прав по
   разделам веб-кабинета (`permissions` JSONB), soft-delete по ADR-003.
   Частичный уникальный индекс `uq_organization_access_profiles_org_name` по
   `(organization_id, lower(name)) WHERE is_deleted = false`.
2. `organization_members.access_profile_id` — nullable, без default, FK
   `ON DELETE SET NULL` + индекс.

Только аддитивно, без backfill: все существующие участники остаются без
профиля, поведение ролей owner/admin/employee не меняется. Nullable-колонка без
default в PG 16 — изменение только каталога; индекс строится по колонке, где у
всех строк NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a2fdde8505d1"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_access_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("permissions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deleted_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_organization_access_profiles_org_is_deleted",
        "organization_access_profiles",
        ["organization_id", "is_deleted"],
        unique=False,
    )
    op.create_index(
        "uq_organization_access_profiles_org_name",
        "organization_access_profiles",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
    )

    op.add_column(
        "organization_members",
        sa.Column("access_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "organization_members_access_profile_id_fkey",
        "organization_members",
        "organization_access_profiles",
        ["access_profile_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_organization_members_access_profile_id",
        "organization_members",
        ["access_profile_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_organization_members_access_profile_id", table_name="organization_members")
    op.drop_constraint(
        "organization_members_access_profile_id_fkey",
        "organization_members",
        type_="foreignkey",
    )
    op.drop_column("organization_members", "access_profile_id")
    op.drop_index(
        "uq_organization_access_profiles_org_name",
        table_name="organization_access_profiles",
    )
    op.drop_index(
        "ix_organization_access_profiles_org_is_deleted",
        table_name="organization_access_profiles",
    )
    op.drop_table("organization_access_profiles")
