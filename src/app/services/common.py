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
from sqlalchemy.orm import selectinload

from src.app.core.access_catalog import (
    AccessLevel,
    forbidden_message,
    full_permissions,
    level_covers,
)
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


# ─── access_profiles (ADR-006): права по разделам кабинета ────────────────────


async def _get_membership_with_profile(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> OrganizationMember | None:
    result = await session.execute(
        select(OrganizationMember)
        .options(selectinload(OrganizationMember.access_profile))
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


def profile_permissions(member: OrganizationMember | None) -> dict[str, str]:
    """Права профиля участника-`employee`; `{}` без профиля или с удалённым.

    Ожидает загруженный `member.access_profile` (selectinload) — без lazy-load.
    """
    if member is None or member.role != MemberRole.employee:
        return {}
    profile = member.access_profile
    if profile is None or profile.is_deleted:
        return {}
    return dict(profile.permissions)


async def get_effective_permissions(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
) -> dict[str, str]:
    """Эффективные права пользователя в организации (вычисляются, не хранятся).

    owner / member `admin` / `super_admin` — все разделы каталога на
    максимальном уровне; member `employee` — права активного профиля; иначе `{}`.
    """
    if org.owner_id == user_id:
        return full_permissions()
    member = await _get_membership_with_profile(session, org.id, user_id)
    if member is not None and member.role == MemberRole.admin:
        return full_permissions()
    if await is_super_admin(session, user_id):
        return full_permissions()
    return profile_permissions(member)


async def has_permission(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
    section: str,
    level: AccessLevel,
    *,
    allow_super_admin: bool = True,
) -> bool:
    """Булева форма `ensure_permission` — для условной видимости полей ответа."""
    if org.owner_id == user_id:
        return True
    member = await _get_membership_with_profile(session, org.id, user_id)
    if member is not None and member.role == MemberRole.admin:
        return True
    if level_covers(profile_permissions(member).get(section), level):
        return True
    return allow_super_admin and await is_super_admin(session, user_id)


async def ensure_permission(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
    section: str,
    level: AccessLevel,
    *,
    allow_super_admin: bool = True,
) -> None:
    """Допускает полного администратора или обладателя профиля с нужным правом.

    Полный администратор (owner / member `admin` / `super_admin`, последний —
    только при `allow_super_admin=True`) проходит всегда. Участник-`employee`
    проходит, если уровень раздела в его активном профиле покрывает требуемый
    (`manage` ⊇ `view`). Иначе 403 `FORBIDDEN` с названием раздела и уровня.

    Права читаются из БД на каждый вызов — смена профиля действует немедленно.
    Порядок проверок ADR-004: этот guard (403) идёт до
    `require_active_subscription` (402).
    """
    if await has_permission(
        session, org, user_id, section, level, allow_super_admin=allow_super_admin
    ):
        return
    raise AccessError("FORBIDDEN", forbidden_message(section, level), 403)


async def is_full_admin(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
) -> bool:
    """Булева форма `ensure_admin_or_owner` (owner / member `admin` / `super_admin`)."""
    try:
        await ensure_admin_or_owner(session, org, user_id)
    except AccessError:
        return False
    return True


async def ensure_can_manage_member(
    session: AsyncSession,
    org: Organization,
    actor_id: uuid.UUID,
    target: OrganizationMember,
) -> None:
    """Защита от эскалации (access_profiles, п.6): обладатель профиля с
    `members:manage` работает только с участниками роли `employee`. Операции над
    участником-`admin` — только полный администратор."""
    if target.role != MemberRole.admin:
        return
    if await is_full_admin(session, org, actor_id):
        return
    raise AccessError(
        "FORBIDDEN",
        "Действия с администраторами организации доступны только полным администраторам",
        403,
    )
