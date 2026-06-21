"""привязка точки к смене: shifts.work_location_id + organization_settings.require_work_location

Revision ID: a1b2c3d40006
Revises: e5a6b7c90005
Create Date: 2026-06-21 14:10:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a1b2c3d40006'
down_revision: str | None = 'e5a6b7c90005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'shifts',
        sa.Column('work_location_id', postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        'fk_shifts_work_location_id',
        'shifts',
        'work_locations',
        ['work_location_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_shifts_work_location_id',
        'shifts',
        ['work_location_id'],
    )
    op.add_column(
        'organization_settings',
        sa.Column(
            'require_work_location',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )


def downgrade() -> None:
    op.drop_column('organization_settings', 'require_work_location')
    op.drop_index('ix_shifts_work_location_id', table_name='shifts')
    op.drop_constraint('fk_shifts_work_location_id', 'shifts', type_='foreignkey')
    op.drop_column('shifts', 'work_location_id')
