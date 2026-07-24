"""Платформенный центр уведомлений (pull-модель, без OS/web-push).

Публичного эндпоинта создания нет — уведомления создаёт только сервисный слой
(`create_notification`/`bulk_create_notifications`), вызываемый производителем
события (в v1 — `services/employee_test.py` при назначении теста). Пользователь
видит и меняет только свои уведомления (`user_id == current_user.id`).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.notification import Notification

logger = get_logger(__name__)


class NotificationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class NotificationInput:
    """Один элемент для `bulk_create_notifications` — та же сигнатура полей,
    что и у `create_notification`, но без сессии (передаётся один раз для всех)."""

    user_id: uuid.UUID
    type: str
    title: str
    body: str | None = None
    payload: dict[str, Any] | None = None
    organization_id: uuid.UUID | None = None


async def create_notification(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    type: str,  # noqa: A002 — имя параметра зафиксировано контрактом backend.md
    title: str,
    body: str | None = None,
    payload: dict[str, Any] | None = None,
    organization_id: uuid.UUID | None = None,
) -> Notification:
    """Создать одно уведомление. Не коммитит — вызывающий код (обычно тот же
    сервис, что создаёт бизнес-событие) сам управляет транзакцией."""
    notification = Notification(
        user_id=user_id,
        organization_id=organization_id,
        type=type,
        title=title,
        body=body,
        payload=payload,
    )
    session.add(notification)
    await session.flush()
    logger.info(
        "notification_created",
        notification_id=str(notification.id),
        user_id=str(user_id),
        type=type,
    )
    return notification


async def bulk_create_notifications(
    session: AsyncSession,
    items: list[NotificationInput],
) -> list[Notification]:
    """Массовое создание в одной транзакции (напр. назначение теста N сотрудникам).
    Не коммитит. Пустой список — no-op."""
    if not items:
        return []
    notifications = [
        Notification(
            user_id=item.user_id,
            organization_id=item.organization_id,
            type=item.type,
            title=item.title,
            body=item.body,
            payload=item.payload,
        )
        for item in items
    ]
    session.add_all(notifications)
    await session.flush()
    logger.info("notifications_bulk_created", count=len(notifications))
    return notifications


async def list_notifications(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int = 20,
    offset: int = 0,
    unread: bool | None = None,
) -> tuple[list[Notification], int]:
    conditions = [Notification.user_id == user_id]
    if unread:
        conditions.append(Notification.is_read.is_(False))

    total = (
        await session.execute(select(func.count()).select_from(Notification).where(*conditions))
    ).scalar_one()

    result = await session.execute(
        select(Notification)
        .where(*conditions)
        .order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total


async def count_unread(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
    )
    return result.scalar_one()


async def mark_read(
    session: AsyncSession,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> Notification:
    """Идемпотентно: повторный вызов на уже прочитанном — no-op, не двигает read_at."""
    result = await session.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.user_id == user_id,
        )
    )
    notification = result.scalar_one_or_none()
    if notification is None:
        raise NotificationError("NOTIFICATION_NOT_FOUND", "Уведомление не найдено", 404)

    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.now(UTC)
        await session.flush()
    return notification


async def mark_all_read(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Ставит read_at=now() только непрочитанным. Возвращает число обновлённых."""
    now = datetime.now(UTC)
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Notification)
            .where(Notification.user_id == user_id, Notification.is_read.is_(False))
            .values(is_read=True, read_at=now)
        ),
    )
    await session.flush()
    return result.rowcount or 0
