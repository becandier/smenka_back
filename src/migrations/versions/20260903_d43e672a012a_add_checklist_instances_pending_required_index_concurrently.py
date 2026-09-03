"""checklist_grace_period: частичный индекс pending+required (CONCURRENTLY)

Revision ID: d43e672a012a
Revises: 5856058d23ab
Create Date: 2026-09-03 00:00:01.000000+00:00

Частичный индекс `ix_checklist_instances_pending_required` под Celery-задачу
`finalize_expired_checklist_grace_periods` (кандидаты на терминальную фиксацию
по истечении окна дозаполнения — см. ADR-004).

Вынесен из `5856058d23ab` в отдельную non-transactional миграцию намеренно:
обычный `CREATE INDEX` держит SHARE-лок на `checklist_instances` на всё время
построения индекса — это блокирует INSERT/UPDATE/DELETE по таблице (в т.ч.
`create_instances_for_shift`, которая пишет туда на каждый старт организационной
смены, по два экземпляра). Таблица растёт непрерывно на живом проде, поэтому
индекс строится `CONCURRENTLY` — без блокировки записи, ценой того, что при
сбое построения (например, конкурентный `VACUUM`/таймаут) индекс может остаться
`INVALID` и потребовать ручного `DROP INDEX` + повторного прогона миграции.
`CREATE/DROP INDEX CONCURRENTLY` нельзя выполнять внутри транзакции — отсюда
`autocommit_block()` и то, что эта миграция не может быть объединена с
`ADD COLUMN` из `5856058d23ab` (та остаётся обычной, транзакционной).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d43e672a012a"
down_revision: str | None = "5856058d23ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_checklist_instances_pending_required",
            "checklist_instances",
            ["shift_id"],
            unique=False,
            postgresql_where=sa.text("status = 'pending' AND is_required = true"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_checklist_instances_pending_required",
            table_name="checklist_instances",
            postgresql_concurrently=True,
        )
