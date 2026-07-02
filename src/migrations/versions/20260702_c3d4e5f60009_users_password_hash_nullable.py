"""users.password_hash nullable (OAuth-only пользователи)

Revision ID: c3d4e5f60009
Revises: b2c3d4e50008
Create Date: 2026-07-02 12:00:00.000000+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f60009"
down_revision: str | None = "b2c3d4e50008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.Text(),
        nullable=True,
    )


def downgrade() -> None:
    # ВНИМАНИЕ: откат сломается (NOT NULL violation), если к этому моменту
    # существуют OAuth-only пользователи (password_hash IS NULL) — такие
    # строки нужно вручную согласовать (удалить/заполнить пароль) перед downgrade.
    op.alter_column(
        "users",
        "password_hash",
        existing_type=sa.Text(),
        nullable=False,
    )
