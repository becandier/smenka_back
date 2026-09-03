"""checklist_grace_period: checklist_grace_minutes на organization_settings

Revision ID: 5856058d23ab
Revises: 682bd9857504
Create Date: 2026-09-03 00:00:00.000000+00:00

Окно дозаполнения чек-листа после закрытия смены (backend.md фичи
`checklist_grace_period`). `server_default='30'` — осознанно: все существующие
организации сразу получают 30-минутное окно, `0` возвращает прежнее поведение
(правка чек-листа завершённой смены отклоняется `SHIFT_FINISHED`).

Частичный индекс под Celery-задачу `finalize_expired_checklist_grace_periods`
вынесен в отдельную non-transactional миграцию `d43e672a012a`
(`CREATE INDEX CONCURRENTLY` нельзя выполнить в той же транзакции, что и
`ADD COLUMN`, а обычный `CREATE INDEX` держит блокирующий на запись SHARE-лок
на `checklist_instances`, которая растёт на каждый старт org-смены — на живом
проде это неприемлемый риск).
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


def downgrade() -> None:
    op.drop_column("organization_settings", "checklist_grace_minutes")
