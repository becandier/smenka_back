"""Общие guard-функции доступа к организации.

Единая точка проверки прав на org-ресурсы: владелец / админ / участник.
Раньше эти проверки были продублированы по сервисам (`_check_org_access`,
несколько копий `_check_admin_or_owner`) и ни одна не учитывала платформенного
`super_admin`. Здесь логика собрана в одном месте и добавлена ветка super_admin.

Инвариант платформы: пользователь с `User.role == super_admin` имеет доступ ко
всем org-ресурсам на чтение и управление, даже если он не состоит в организации
(см. docs/tasks/admin_panel/backend.md, Блок C2).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.user import User, UserRole


class AccessError(Exception):
    """Ошибка доступа к org-ресурсу. Маппится в {data,error} 403 в main.py."""

    def __init__(self, code: str, message: str, status_code: int = 403):
        self.code = code
        self.message = message
        self.status_code = status_code


async def is_super_admin(session: AsyncSession, user_id: uuid.UUID) -> bool:
    """Проверка платформенной роли, переиспользуемая и вне org-guard'ов ниже
    (напр. `services/entitlements.py`, где `require_active_subscription`
    должен пропускать super_admin в обход read-only)."""
    result = await session.execute(select(User.role).where(User.id == user_id))
    return result.scalar_one_or_none() == UserRole.super_admin


async def ensure_owner(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
    *,
    message: str = "Только владелец может выполнить это действие",
) -> None:
    """Допускает только владельца организации или super_admin."""
    if org.owner_id == user_id:
        return
    if await is_super_admin(session, user_id):
        return
    raise AccessError("FORBIDDEN", message, 403)


async def ensure_member(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
    *,
    message: str = "Нет доступа к организации",
) -> None:
    """Допускает владельца, любого участника организации или super_admin."""
    if org.owner_id == user_id:
        return
    result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.user_id == user_id,
        )
    )
    if result.scalar_one_or_none() is not None:
        return
    if await is_super_admin(session, user_id):
        return
    raise AccessError("FORBIDDEN", message, 403)


async def ensure_admin_or_owner(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
    *,
    message: str = "Недостаточно прав",
    allow_super_admin: bool = True,
) -> None:
    """Допускает владельца или участника с ролью admin; по умолчанию — и super_admin.

    `allow_super_admin=False` — для операционных инструментов организации, где
    ТЗ фичи явно исключает платформенный доступ (manual_time_entry, R8:
    «операционный инструмент организации», единственное явное исключение из
    платформенного инварианта super_admin, закреплено тестами).
    """
    if org.owner_id == user_id:
        return
    result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.user_id == user_id,
            OrganizationMember.role == MemberRole.admin,
        )
    )
    if result.scalar_one_or_none() is not None:
        return
    if allow_super_admin and await is_super_admin(session, user_id):
        return
    raise AccessError("FORBIDDEN", message, 403)
