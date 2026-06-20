"""add address column to work_locations (читаемый адрес точки)

Revision ID: c3e4f5a70003
Revises: b2d3f4a60002
Create Date: 2026-06-20 13:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'c3e4f5a70003'
down_revision: str | None = 'b2d3f4a60002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'work_locations',
        sa.Column('address', sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('work_locations', 'address')
