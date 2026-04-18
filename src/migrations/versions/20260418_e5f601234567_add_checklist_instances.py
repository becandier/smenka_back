"""add checklist instances and items, shift.has_incomplete_required_checklists

Revision ID: e5f601234567
Revises: d4e5f6012345
Create Date: 2026-04-18 13:00:00.000000+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'e5f601234567'
down_revision: Union[str, None] = 'd4e5f6012345'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'shifts',
        sa.Column(
            'has_incomplete_required_checklists',
            sa.Boolean(),
            nullable=False,
            server_default='false',
        ),
    )

    existing_checklist_type = postgresql.ENUM(
        'shift_start', 'shift_end', name='checklisttype', create_type=False,
    )

    op.create_table(
        'checklist_instances',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('shift_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('template_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('type', existing_checklist_type, nullable=False),
        sa.Column('is_required', sa.Boolean(), nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                'pending', 'completed', 'incomplete',
                name='checklistinstancestatus',
            ),
            nullable=False,
        ),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['shift_id'], ['shifts.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['template_id'], ['checklist_templates.id'], ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_checklist_instances_shift_id',
        'checklist_instances',
        ['shift_id'],
    )

    op.create_table(
        'checklist_instance_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('instance_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            'template_item_id', postgresql.UUID(as_uuid=True), nullable=True,
        ),
        sa.Column('text', sa.String(length=500), nullable=False),
        sa.Column('is_required', sa.Boolean(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('is_completed', sa.Boolean(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('change_count', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['instance_id'], ['checklist_instances.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['template_item_id'],
            ['checklist_template_items.id'],
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_checklist_instance_items_instance_id',
        'checklist_instance_items',
        ['instance_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_checklist_instance_items_instance_id',
        table_name='checklist_instance_items',
    )
    op.drop_table('checklist_instance_items')
    op.drop_index(
        'ix_checklist_instances_shift_id',
        table_name='checklist_instances',
    )
    op.drop_table('checklist_instances')
    sa.Enum(name='checklistinstancestatus').drop(op.get_bind(), checkfirst=True)
    op.drop_column('shifts', 'has_incomplete_required_checklists')
