"""auto_finish_hours_nullable

Revision ID: 7aac2b35c33d
Revises: a1b2c3d4e5f6
Create Date: 2026-04-09 12:22:37.183793+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '7aac2b35c33d'
down_revision: str | None = 'a1b2c3d4e5f6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column('organization_settings', 'auto_finish_hours',
               existing_type=sa.INTEGER(),
               nullable=True)


def downgrade() -> None:
    op.alter_column('organization_settings', 'auto_finish_hours',
               existing_type=sa.INTEGER(),
               nullable=False)
