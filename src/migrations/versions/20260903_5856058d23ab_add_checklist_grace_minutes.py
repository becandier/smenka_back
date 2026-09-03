"""checklist_grace_period: checklist_grace_minutes на organization_settings

Revision ID: 5856058d23ab
Revises: 682bd9857504
Create Date: 2026-09-03 00:00:00.000000+00:00

Окно дозаполнения чек-листа после закрытия смены (backend.md фичи
`checklist_grace_period`). `server_default='30'` — осознанно: все существующие
организации сразу получают 30-минутное окно, `0` возвращает прежнее поведение
(правка чек-листа завершённой смены отклоняется `SHIFT_FINISHED`).

Отдельно — частичный индекс под Celery-задачу
`finalize_expired_checklist_grace_periods`, которая финализирует статус
обязательных чек-листов по истечении окна (пока окно открыто, они остаются
`pending`, а не `incomplete`).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5856058d23ab"
down_revision: str | None = "682bd9857504"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organization_settings",
        sa.Column(
            "checklist_grace_minutes",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.create_index(
        "ix_checklist_instances_pending_required",
        "checklist_instances",
        ["shift_id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending' AND is_required = true"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_checklist_instances_pending_required",
        table_name="checklist_instances",
    )
    op.drop_column("organization_settings", "checklist_grace_minutes")
