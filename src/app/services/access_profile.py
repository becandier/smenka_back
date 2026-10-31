"""Профили доступа организации (access_profiles, ADR-006).

Профиль — именованный набор прав по разделам веб-кабинета
(`core/access_catalog.py`), выдаётся участнику с ролью `employee`. Управление
профилями и их выдача — только полный администратор (owner / member `admin` /
`super_admin`): ни один раздел каталога не даёт права выдавать себе больше.
Чтение списка профилей доступно и обладателю `members:view` (видеть, у кого
какой доступ).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.access_profile import OrganizationAccessProfile
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.services import entitlements
from src.app.services.common import ensure_admin_or_owner, ensure_permission
from src.app.services.organization import get_organization

logger = get_logger(__name__)

_NAME_UNIQUE_INDEX = "uq_organization_access_profiles_org_name"
_FULL_ADMIN_MESSAGE = "Управлять профилями доступа может только полный администратор"


class AccessProfileError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


def _not_found() -> AccessProfileError:
    return AccessProfileError("ACCESS_PROFILE_NOT_FOUND", "Профиль доступа не найден", 404)


def _name_taken() -> AccessProfileError:
    return AccessProfileError(
        "ACCESS_PROFILE_NAME_TAKEN", "Профиль доступа с таким названием уже существует", 409
    )


async def _ensure_can_read_profiles(
    session: AsyncSession, org: Organization, user_id: uuid.UUID
) -> None:
    """Полный админ или обладатель `members:view` (полный админ проходит guard сразу)."""
    await ensure_permission(session, org, user_id, "members", "view")


async def _ensure_can_manage_profiles(
    session: AsyncSession, org: Organization, user_id: uuid.UUID
) -> None:
    await ensure_admin_or_owner(session, org, user_id, message=_FULL_ADMIN_MESSAGE)
    await entitlements.require_active_subscription(session, org, user_id)


async def _get_profile(
    session: AsyncSession,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> OrganizationAccessProfile:
    """Профиль организации, включая удалённый; чужой организации — 404."""
    result = await session.execute(
        select(OrganizationAccessProfile).where(
            OrganizationAccessProfile.id == profile_id,
            OrganizationAccessProfile.organization_id == org_id,
        )
    )
    profile = result.scalar_one_or_none()
    if profile is None:
        raise _not_found()
    return profile


async def get_active_profile(
    session: AsyncSession,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
) -> OrganizationAccessProfile:
    """Активный профиль организации; удалённый или чужой — 404 ACCESS_PROFILE_NOT_FOUND."""
    profile = await _get_profile(session, org_id, profile_id)
    if profile.is_deleted:
        raise _not_found()
    return profile


async def _ensure_name_free(
    session: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    conditions = [
        OrganizationAccessProfile.organization_id == org_id,
        OrganizationAccessProfile.is_deleted.is_(False),
        func.lower(OrganizationAccessProfile.name) == name.lower(),
    ]
    if exclude_id is not None:
        conditions.append(OrganizationAccessProfile.id != exclude_id)
    result = await session.execute(select(OrganizationAccessProfile.id).where(*conditions))
    if result.first() is not None:
        raise _name_taken()


async def _flush_profile(session: AsyncSession) -> None:
    """flush с переводом гонки по частичному уникальному индексу в 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        if _NAME_UNIQUE_INDEX in str(exc.orig):
            raise _name_taken() from exc
        raise


async def count_members(
    session: AsyncSession,
    profile_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """`{profile_id: число участников}` одним запросом с группировкой."""
    if not profile_ids:
        return {}
    result = await session.execute(
        select(OrganizationMember.access_profile_id, func.count())
        .where(OrganizationMember.access_profile_id.in_(profile_ids))
        .group_by(OrganizationMember.access_profile_id)
    )
    return {pid: cnt for pid, cnt in result.all() if pid is not None}


async def list_profiles(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> tuple[list[OrganizationAccessProfile], dict[uuid.UUID, int]]:
    org = await get_organization(session, org_id)
    await _ensure_can_read_profiles(session, org, requester_id)
    query = select(OrganizationAccessProfile).where(
        OrganizationAccessProfile.organization_id == org_id
    )
    if not include_deleted:
        query = query.where(OrganizationAccessProfile.is_deleted.is_(False))
    result = await session.execute(
        query.order_by(OrganizationAccessProfile.name, OrganizationAccessProfile.created_at)
    )
    profiles = list(result.scalars().all())
    counts = await count_members(session, [p.id for p in profiles])
    return profiles, counts


async def get_profile_detail(
    session: AsyncSession,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> tuple[OrganizationAccessProfile, int]:
    org = await get_organization(session, org_id)
    await _ensure_can_read_profiles(session, org, requester_id)
    profile = await _get_profile(session, org_id, profile_id)
    counts = await count_members(session, [profile.id])
    return profile, counts.get(profile.id, 0)


async def create_profile(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    name: str,
    description: str | None,
    permissions: dict[str, str],
) -> OrganizationAccessProfile:
    org = await get_organization(session, org_id)
    await _ensure_can_manage_profiles(session, org, requester_id)
    await _ensure_name_free(session, org_id, name)
    profile = OrganizationAccessProfile(
        organization_id=org_id,
        name=name,
        description=description,
        permissions=permissions,
    )
    session.add(profile)
    await _flush_profile(session)
    logger.info("access_profile_created", org_id=str(org_id), profile_id=str(profile.id))
    return profile


async def update_profile(
    session: AsyncSession,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    fields: dict[str, object],
) -> tuple[OrganizationAccessProfile, list[str]]:
    """Partial-обновление; возвращает (профиль, список реально изменённых полей)."""
    org = await get_organization(session, org_id)
    await _ensure_can_manage_profiles(session, org, requester_id)
    profile = await get_active_profile(session, org_id, profile_id)

    changed: list[str] = []
    name = fields.get("name")
    if isinstance(name, str) and name != profile.name:
        if name.lower() != profile.name.lower():
            await _ensure_name_free(session, org_id, name, exclude_id=profile.id)
        profile.name = name
        changed.append("name")
    if "description" in fields and fields["description"] != profile.description:
        description = fields["description"]
        profile.description = description if isinstance(description, str) else None
        changed.append("description")
    permissions = fields.get("permissions")
    if isinstance(permissions, dict) and permissions != profile.permissions:
        profile.permissions = permissions
        changed.append("permissions")

    if changed:
        await _flush_profile(session)
        logger.info(
            "access_profile_updated",
            org_id=str(org_id),
            profile_id=str(profile.id),
            fields=changed,
        )
    return profile, changed


async def delete_profile(
    session: AsyncSession,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> tuple[OrganizationAccessProfile, int]:
    """Soft-delete + отвязка участников в той же транзакции. Возвращает (профиль, N)."""
    org = await get_organization(session, org_id)
    await _ensure_can_manage_profiles(session, org, requester_id)
    profile = await get_active_profile(session, org_id, profile_id)

    profile.is_deleted = True
    profile.deleted_at = datetime.now(UTC)
    profile.deleted_by_user_id = requester_id
    # FK ON DELETE SET NULL при soft-delete не срабатывает — отвязываем явно.
    detach = await session.execute(
        update(OrganizationMember)
        .where(OrganizationMember.access_profile_id == profile.id)
        .values(access_profile_id=None)
        .execution_options(synchronize_session="fetch")
    )
    detached = int(getattr(detach, "rowcount", 0) or 0)
    await session.flush()
    logger.info(
        "access_profile_deleted",
        org_id=str(org_id),
        profile_id=str(profile.id),
        detached_members=detached,
    )
    return profile, detached


async def restore_profile(
    session: AsyncSession,
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> OrganizationAccessProfile:
    """Восстановить удалённый профиль. Участников не возвращает."""
    org = await get_organization(session, org_id)
    await _ensure_can_manage_profiles(session, org, requester_id)
    profile = await _get_profile(session, org_id, profile_id)
    if not profile.is_deleted:
        raise AccessProfileError("ACCESS_PROFILE_NOT_DELETED", "Профиль доступа не удалён", 409)
    await _ensure_name_free(session, org_id, profile.name, exclude_id=profile.id)
    profile.is_deleted = False
    profile.deleted_at = None
    profile.deleted_by_user_id = None
    await _flush_profile(session)
    logger.info("access_profile_restored", org_id=str(org_id), profile_id=str(profile.id))
    return profile


async def assign_profile_to_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    profile_id: uuid.UUID | None,
    requester_id: uuid.UUID,
    *,
    is_super_admin: bool = False,
) -> OrganizationMember:
    """Выдать (`profile_id`) или снять (`None`) профиль доступа участнику."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_FULL_ADMIN_MESSAGE)
    # Как и с системной ролью: себе профиль не выдаётся и не снимается
    # (кроме super_admin — он действует сквозным доступом, не как участник).
    if not is_super_admin and requester_id == member_user_id:
        raise AccessProfileError("FORBIDDEN", "Нельзя изменить собственный профиль доступа", 403)
    await entitlements.require_active_subscription(session, org, requester_id)

    result = await session.execute(
        select(OrganizationMember)
        .options(
            selectinload(OrganizationMember.user),
            selectinload(OrganizationMember.custom_role),
            selectinload(OrganizationMember.access_profile),
        )
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == member_user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise AccessProfileError("MEMBER_NOT_FOUND", "Участник не найден", 404)

    profile = None
    if profile_id is not None:
        profile = await get_active_profile(session, org_id, profile_id)
        if member.role != MemberRole.employee:
            raise AccessProfileError(
                "ACCESS_PROFILE_REQUIRES_EMPLOYEE",
                "Профиль доступа назначается только сотруднику с ролью employee",
                409,
            )

    member.access_profile_id = profile.id if profile is not None else None
    member.access_profile = profile
    await session.flush()
    logger.info(
        "member_access_profile_updated",
        org_id=str(org_id),
        user_id=str(member_user_id),
        access_profile_id=str(profile.id) if profile is not None else None,
    )
    return member
