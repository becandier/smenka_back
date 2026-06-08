"""add checklist templates and items

Revision ID: c2d3e4f51234
Revises: b1c2d3e4f501
Create Date: 2026-04-18 11:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'c2d3e4f51234'
down_revision: str | None = 'b1c2d3e4f501'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'checklist_templates',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column(
            'type',
            sa.Enum('shift_start', 'shift_end', name='checklisttype'),
            nullable=False,
        ),
        sa.Column('is_required', sa.Boolean(), nullable=False),
        sa.Column('max_per_shift', sa.Integer(), nullable=False),
        sa.Column('is_archived', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['organization_id'], ['organizations.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_checklist_templates_organization_id',
        'checklist_templates',
        ['organization_id'],
    )
    op.create_index(
        'ix_checklist_templates_is_archived',
        'checklist_templates',
        ['is_archived'],
    )

    op.create_table(
        'checklist_template_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('template_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('text', sa.String(length=500), nullable=False),
        sa.Column('is_required', sa.Boolean(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['template_id'], ['checklist_templates.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_checklist_template_items_template_id',
        'checklist_template_items',
        ['template_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_checklist_template_items_template_id',
        table_name='checklist_template_items',
    )
    op.drop_table('checklist_template_items')
    op.drop_index(
        'ix_checklist_templates_is_archived',
        table_name='checklist_templates',
    )
    op.drop_index(
        'ix_checklist_templates_organization_id',
        table_name='checklist_templates',
    )
    op.drop_table('checklist_templates')
    sa.Enum(name='checklisttype').drop(op.get_bind(), checkfirst=True)
