"""add organization_roles and member role_id

Revision ID: b1c2d3e4f501
Revises: 7aac2b35c33d
Create Date: 2026-04-18 10:00:00.000000+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'b1c2d3e4f501'
down_revision: Union[str, None] = '7aac2b35c33d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'organization_roles',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['organization_id'], ['organizations.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'name', name='uq_org_role_name'),
    )
    op.create_index(
        'ix_organization_roles_organization_id',
        'organization_roles',
        ['organization_id'],
    )

    op.add_column(
        'organization_members',
        sa.Column('role_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_organization_members_role_id',
        'organization_members',
        'organization_roles',
        ['role_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_organization_members_role_id',
        'organization_members',
        ['role_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_organization_members_role_id', table_name='organization_members')
    op.drop_constraint(
        'fk_organization_members_role_id',
        'organization_members',
        type_='foreignkey',
    )
    op.drop_column('organization_members', 'role_id')
    op.drop_index(
        'ix_organization_roles_organization_id',
        table_name='organization_roles',
    )
    op.drop_table('organization_roles')
