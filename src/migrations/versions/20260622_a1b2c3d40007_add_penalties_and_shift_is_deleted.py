"""штрафы: organization_penalty_templates + penalties + shifts.is_deleted

Revision ID: a1b2c3d40007
Revises: a1b2c3d40006
Create Date: 2026-06-22 09:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d40007'
down_revision: str | None = 'a1b2c3d40006'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'organization_penalty_templates',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reason', sa.String(length=200), nullable=False),
        sa.Column('amount_minor', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), server_default='RUB', nullable=False),
        sa.Column('is_deleted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_penalty_templates_org_is_deleted',
        'organization_penalty_templates',
        ['organization_id', 'is_deleted'],
    )

    op.create_table(
        'penalties',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('organization_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('member_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('shift_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('template_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('reason', sa.String(length=200), nullable=False),
        sa.Column('amount_minor', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), server_default='RUB', nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('comment', sa.String(length=500), nullable=True),
        sa.Column('created_by_user_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('is_deleted', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('deleted_by_user_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['member_id'], ['organization_members.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['shift_id'], ['shifts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(
            ['template_id'],
            ['organization_penalty_templates.id'],
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['deleted_by_user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_penalties_org_is_deleted', 'penalties', ['organization_id', 'is_deleted'])
    op.create_index('ix_penalties_member_is_deleted', 'penalties', ['member_id', 'is_deleted'])
    op.create_index('ix_penalties_shift_id', 'penalties', ['shift_id'])
    op.create_index('ix_penalties_occurred_at', 'penalties', ['occurred_at'])

    op.add_column(
        'shifts',
        sa.Column(
            'is_deleted',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )


def downgrade() -> None:
    op.drop_column('shifts', 'is_deleted')

    op.drop_index('ix_penalties_occurred_at', table_name='penalties')
    op.drop_index('ix_penalties_shift_id', table_name='penalties')
    op.drop_index('ix_penalties_member_is_deleted', table_name='penalties')
    op.drop_index('ix_penalties_org_is_deleted', table_name='penalties')
    op.drop_table('penalties')

    op.drop_index(
        'ix_penalty_templates_org_is_deleted',
        table_name='organization_penalty_templates',
    )
    op.drop_table('organization_penalty_templates')
