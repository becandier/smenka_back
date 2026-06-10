"""add organization_member_rates (история ставок участника)

Revision ID: f7a8b9c0d1e2
Revises: e5f601234567
Create Date: 2026-06-10 14:40:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'f7a8b9c0d1e2'
down_revision: str | None = 'e5f601234567'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'organization_member_rates',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('member_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('rate_amount_minor', sa.Integer(), nullable=False),
        sa.Column(
            'rate_type',
            sa.Enum('hourly', 'per_shift', name='ratetype'),
            nullable=False,
        ),
        sa.Column(
            'currency',
            sa.String(length=3),
            nullable=False,
            server_default='RUB',
        ),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('note', sa.String(length=500), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['member_id'], ['organization_members.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'member_id', 'effective_from', name='uq_member_rate_effective_from',
        ),
    )
    op.create_index(
        'ix_organization_member_rates_member_id',
        'organization_member_rates',
        ['member_id'],
    )
    op.create_index(
        'ix_member_rates_member_id_effective_from_desc',
        'organization_member_rates',
        ['member_id', sa.text('effective_from DESC')],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_member_rates_member_id_effective_from_desc',
        table_name='organization_member_rates',
    )
    op.drop_index(
        'ix_organization_member_rates_member_id',
        table_name='organization_member_rates',
    )
    op.drop_table('organization_member_rates')
    sa.Enum(name='ratetype').drop(op.get_bind(), checkfirst=True)
