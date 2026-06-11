"""add attempts to verification_codes (счётчик неверных вводов кода)

Revision ID: a1c2e3f50001
Revises: f7a8b9c0d1e2
Create Date: 2026-06-11 14:00:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a1c2e3f50001'
down_revision: str | None = 'f7a8b9c0d1e2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'verification_codes',
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('verification_codes', 'attempts')
