"""add checklist role assignments and member overrides

Revision ID: d4e5f6012345
Revises: c2d3e4f51234
Create Date: 2026-04-18 12:00:00.000000+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'd4e5f6012345'
down_revision: Union[str, None] = 'c2d3e4f51234'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'checklist_role_assignments',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('template_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('role_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['template_id'], ['checklist_templates.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['role_id'], ['organization_roles.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'template_id', 'role_id', name='uq_checklist_role_assignment',
        ),
    )
    op.create_index(
        'ix_checklist_role_assignments_template_id',
        'checklist_role_assignments',
        ['template_id'],
    )
    op.create_index(
        'ix_checklist_role_assignments_role_id',
        'checklist_role_assignments',
        ['role_id'],
    )

    op.create_table(
        'checklist_member_overrides',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('template_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('member_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            'override_type',
            sa.Enum('add', 'remove', name='overridetype'),
            nullable=False,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['template_id'], ['checklist_templates.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['member_id'], ['organization_members.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'template_id', 'member_id', name='uq_checklist_member_override',
        ),
    )
    op.create_index(
        'ix_checklist_member_overrides_template_id',
        'checklist_member_overrides',
        ['template_id'],
    )
    op.create_index(
        'ix_checklist_member_overrides_member_id',
        'checklist_member_overrides',
        ['member_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_checklist_member_overrides_member_id',
        table_name='checklist_member_overrides',
    )
    op.drop_index(
        'ix_checklist_member_overrides_template_id',
        table_name='checklist_member_overrides',
    )
    op.drop_table('checklist_member_overrides')
    sa.Enum(name='overridetype').drop(op.get_bind(), checkfirst=True)

    op.drop_index(
        'ix_checklist_role_assignments_role_id',
        table_name='checklist_role_assignments',
    )
    op.drop_index(
        'ix_checklist_role_assignments_template_id',
        table_name='checklist_role_assignments',
    )
    op.drop_table('checklist_role_assignments')
