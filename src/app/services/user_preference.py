"""Личные настройки пользователя (фича table_column_settings).

Хранилище непрозрачных JSON-значений по строковому ключу. Все операции
изолированы по `user_id` текущего пользователя; организация, подписка и
профили доступа здесь не участвуют — настройки личные и пишутся в том числе
в read-only режиме организации. В `audit_logs` не пишем.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.user_preference import UserPreference

PREFERENCES_LIMIT = 200


class PreferenceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


async def list_preferences(
    session: AsyncSession,
    user_id: uuid.UUID,
    prefix: str | None = None,
) -> dict[str, dict[str, Any]]:
    stmt = select(UserPreference.key, UserPreference.value).where(
        UserPreference.user_id == user_id
    )
    if prefix is not None:
        # starts_with вместо LIKE: `_` входит в алфавит ключа и в LIKE был бы
        # шаблоном «любой символ».
        stmt = stmt.where(func.starts_with(UserPreference.key, prefix))
    rows = await session.execute(stmt.order_by(UserPreference.key))
    return dict(rows.tuples().all())


async def put_preference(
    session: AsyncSession,
    user_id: uuid.UUID,
    key: str,
    value: dict[str, Any],
) -> UserPreference:
    """Создаёт или целиком заменяет значение (атомарный upsert).

    Лимит `PREFERENCES_LIMIT` проверяется только при создании нового ключа;
    перезапись существующего проходит всегда. Лимит мягкий: гонка двух
    параллельных созданий на границе не закрывается (ТЗ).
    """
    count, key_exists = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.bool_or(UserPreference.key == key), False),
            ).where(UserPreference.user_id == user_id)
        )
    ).one()
    if not key_exists and count >= PREFERENCES_LIMIT:
        raise PreferenceError(
            "PREFERENCES_LIMIT_REACHED",
            f"Достигнут лимит личных настроек ({PREFERENCES_LIMIT})",
            409,
        )

    now = datetime.now(UTC)
    stmt = (
        pg_insert(UserPreference)
        .values(user_id=user_id, key=key, value=value, created_at=now, updated_at=now)
        .on_conflict_do_update(
            index_elements=[UserPreference.user_id, UserPreference.key],
            set_={"value": value, "updated_at": now},
        )
        .returning(UserPreference)
        .execution_options(populate_existing=True)
    )
    preference = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return preference


async def delete_preference(session: AsyncSession, user_id: uuid.UUID, key: str) -> int:
    """Идемпотентное удаление: 1 — ключ был и удалён, 0 — ключа не было."""
    result = await session.execute(
        delete(UserPreference)
        .where(UserPreference.user_id == user_id, UserPreference.key == key)
        .returning(UserPreference.key)
    )
    deleted = len(result.all())
    await session.commit()
    return deleted
