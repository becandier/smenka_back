"""schedule_window_enforcement: early_start_minutes на organization_settings

Revision ID: 65aa0f57410e
Revises: 8e42f2b643ea
Create Date: 2026-08-16 19:30:59.985749+00:00

Запрет старта смены вне окна графика (S1/S2, backend.md фичи
`schedule_window_enforcement`). Единственное изменение схемы — допуск на
ранний старт, настройка организации: за сколько минут до планового начала
графика сотруднику разрешено начать смену. `0` (server_default) — строго не
раньше начала, обратная совместимость для существующих организаций.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "65aa0f57410e"
down_revision: str | None = "8e42f2b643ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organization_settings",
        sa.Column(
            "early_start_minutes",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("organization_settings", "early_start_minutes")
