"""member_display_name: display_name column on organization_members

Revision ID: 352f7e3148af
Revises: a4b5c6d7e803
Create Date: 2026-07-22 13:00:00.000000+00:00

Имя участника внутри конкретной организации, независимое от глобального
`users.name`. NULL = не задано (используется настоящее имя). Уникальность не
требуется, индекс не нужен (поиск — по небольшому списку участников
организации). Данные не переносятся; downgrade снимает колонку.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "352f7e3148af"
down_revision: str | None = "a4b5c6d7e803"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organization_members",
        sa.Column("display_name", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organization_members", "display_name")
