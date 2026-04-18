"""auto_finish_hours_nullable

Revision ID: 7aac2b35c33d
Revises: a1b2c3d4e5f6
Create Date: 2026-04-09 12:22:37.183793+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7aac2b35c33d'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('organization_settings', 'auto_finish_hours',
               existing_type=sa.INTEGER(),
               nullable=True)


def downgrade() -> None:
    op.alter_column('organization_settings', 'auto_finish_hours',
               existing_type=sa.INTEGER(),
               nullable=False)
