"""add user role column

Revision ID: a1b2c3d4e5f6
Revises: fd49e1a252de
Create Date: 2026-04-07 12:00:00.000000+00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'fd49e1a252de'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    userrole_enum = sa.Enum('super_admin', 'user', name='userrole')
    userrole_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        'users',
        sa.Column('role', userrole_enum, server_default='user', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('users', 'role')

    sa.Enum(name='userrole').drop(op.get_bind(), checkfirst=True)
