"""tariffs: data-миграция — подписки для существующих организаций

Revision ID: cd0999882d68
Revises: 83e8937dc81f
Create Date: 2026-08-27 12:15:00.000000+00:00

Мягкая посадка при вводе тарифов (backend.md, §«Миграции», п.4): всем
`organizations WHERE is_deleted = false` без подписки — `premium`/`active`
на 30 дней от момента выката, `note = 'Автоматическая посадка при вводе
тарифов'`. Ничего не отключается в момент выката — дальше супер-админ
разводит клиентов по тарифам вручную кнопкой «Продлить».

Каждой созданной подписке — событие `created` в `subscription_events` с
`actor_user_id = NULL` (системное событие, не привязано к конкретному
человеку). UUID генерируются в Python (а не `gen_random_uuid()`), чтобы не
зависеть от версии PostgreSQL/расширений и точно попарно связать подписку с
её событием без повторного запроса по тексту заметки.

Организация «Атлетика» (боевой клиент, Премиум на 12 месяцев) сюда
намеренно НЕ хардкодится — проставляется вручную кнопкой «Продлить» в
супер-админке сразу после выката (см. backend.md).

Идемпотентно: выбираются только организации без подписки — повторный прогон
не создаёт дублей поверх уже посаженных (в т.ч. тех, что завела обычная
логика приложения для организаций, созданных уже после раскатки кода, но до
применения этой миграции).
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "cd0999882d68"
down_revision: str | None = "83e8937dc81f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LANDING_NOTE = "Автоматическая посадка при вводе тарифов"

organizations_table = sa.table(
    "organizations",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("is_deleted", sa.Boolean),
)
subscriptions_table = sa.table(
    "subscriptions",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("organization_id", postgresql.UUID(as_uuid=True)),
    sa.column("plan_code", sa.String),
    sa.column("status", sa.String),
    sa.column("current_period_start", sa.DateTime(timezone=True)),
    sa.column("current_period_end", sa.DateTime(timezone=True)),
    sa.column("note", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
subscription_events_table = sa.table(
    "subscription_events",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("organization_id", postgresql.UUID(as_uuid=True)),
    sa.column("type", sa.String),
    sa.column("to_plan_code", sa.String),
    sa.column("to_status", sa.String),
    sa.column("period_end_after", sa.DateTime(timezone=True)),
    sa.column("note", sa.String),
    sa.column("actor_user_id", postgresql.UUID(as_uuid=True)),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    bind = op.get_bind()

    org_ids = [
        row[0]
        for row in bind.execute(
            sa.select(organizations_table.c.id)
            .select_from(organizations_table)
            .outerjoin(
                subscriptions_table,
                subscriptions_table.c.organization_id == organizations_table.c.id,
            )
            .where(
                organizations_table.c.is_deleted.is_(False),
                subscriptions_table.c.id.is_(None),
            )
        ).all()
    ]
    if not org_ids:
        return

    now = datetime.now(UTC)
    period_end = now + timedelta(days=30)

    subscription_rows = []
    event_rows = []
    for org_id in org_ids:
        subscription_rows.append(
            {
                "id": uuid.uuid4(),
                "organization_id": org_id,
                "plan_code": "premium",
                "status": "active",
                "current_period_start": now,
                "current_period_end": period_end,
                "note": _LANDING_NOTE,
                "created_at": now,
                "updated_at": now,
            }
        )
        event_rows.append(
            {
                "id": uuid.uuid4(),
                "organization_id": org_id,
                "type": "created",
                "to_plan_code": "premium",
                "to_status": "active",
                "period_end_after": period_end,
                "note": _LANDING_NOTE,
                "actor_user_id": None,
                "created_at": now,
            }
        )

    op.bulk_insert(subscriptions_table, subscription_rows)
    op.bulk_insert(subscription_events_table, event_rows)


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        subscription_events_table.delete().where(
            subscription_events_table.c.type == "created",
            subscription_events_table.c.note == _LANDING_NOTE,
        )
    )
    bind.execute(subscriptions_table.delete().where(subscriptions_table.c.note == _LANDING_NOTE))
