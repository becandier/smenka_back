"""Сервис аудита: запись чувствительных действий и чтение ленты организации.

Запись создаётся в той же транзакции, что и само действие (commit делает
вызывающий — endpoint или контекст Celery-задачи), чтобы аудит не расходился с
фактом: если действие откатилось — запись аудита тоже.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.models.audit_log import AuditAction, AuditLog, AuditResource
from src.app.models.user import User
from src.app.services.shift import ensure_utc


async def record(
    session: AsyncSession,
    *,
    action: AuditAction,
    resource_type: AuditResource,
    organization_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    summary: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    """Записать действие в журнал (async-эндпоинты)."""
    session.add(
        AuditLog(
            action=action.value,
            resource_type=resource_type.value,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            resource_id=resource_id,
            summary=summary,
            ip_address=ip_address,
        )
    )
    await session.flush()


def record_sync(
    session: Session,
    *,
    action: AuditAction,
    resource_type: AuditResource,
    organization_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    summary: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> None:
    """Записать действие в журнал из синхронной Celery-задачи."""
    session.add(
        AuditLog(
            action=action.value,
            resource_type=resource_type.value,
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            resource_id=resource_id,
            summary=summary,
            ip_address=ip_address,
        )
    )


async def list_audit_logs(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
    action: AuditAction | None = None,
    actor_user_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> tuple[list[AuditLog], int, dict[uuid.UUID, str]]:
    """Лента аудита организации (created_at DESC) + total + имена инициаторов.

    `date_to` включительно (как в date_filters). Имена возвращаются отдельной
    картой `{user_id: name}`, чтобы избежать N+1 при сборке ответа.
    """
    conditions = [AuditLog.organization_id == organization_id]
    if action is not None:
        conditions.append(AuditLog.action == action.value)
    if actor_user_id is not None:
        conditions.append(AuditLog.actor_user_id == actor_user_id)
    if date_from is not None:
        conditions.append(AuditLog.created_at >= ensure_utc(date_from))
    if date_to is not None:
        conditions.append(AuditLog.created_at <= ensure_utc(date_to))

    total_result = await session.execute(
        select(func.count()).select_from(AuditLog).where(*conditions)
    )
    total = total_result.scalar_one()

    result = await session.execute(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = list(result.scalars().all())

    actor_ids = {item.actor_user_id for item in items if item.actor_user_id is not None}
    names: dict[uuid.UUID, str] = {}
    if actor_ids:
        users_result = await session.execute(
            select(User.id, User.name).where(User.id.in_(actor_ids))
        )
        names = {row[0]: row[1] for row in users_result.all()}

    return items, total, names
