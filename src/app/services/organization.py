import re
import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.services.common import ensure_admin_or_owner, ensure_member, ensure_owner

logger = get_logger(__name__)

# Управляющие символы, которые НЕ являются переводом строки/табуляцией (те
# схлопываются в пробел ДО этой проверки) — переносим их в исключение
# INVALID_DISPLAY_NAME, а не молча вырезаем.
_FORBIDDEN_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DISPLAY_NAME_MAX_LENGTH = 100


class OrgError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


def _generate_invite_code() -> str:
    return secrets.token_hex(4).upper()


async def create_organization(
    session: AsyncSession,
    name: str,
    owner_id: uuid.UUID,
) -> Organization:
    org = Organization(name=name, owner_id=owner_id)
    session.add(org)
    await session.flush()

    from src.app.models.organization_settings import OrganizationSettings

    settings = OrganizationSettings(organization_id=org.id, auto_finish_hours=16)
    session.add(settings)
    await session.flush()
    await session.refresh(org, ["settings"])

    logger.info("organization_created", org_id=str(org.id), owner_id=str(owner_id))
    return org


async def get_organization(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> Organization:
    result = await session.execute(
        select(Organization)
        .options(selectinload(Organization.settings))
        .where(
            Organization.id == org_id,
            Organization.is_deleted.is_(False),
        )
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise OrgError("ORG_NOT_FOUND", "Организация не найдена", 404)
    return org


async def get_user_organizations(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> list[Organization]:
    """Get all active orgs where user is owner or member."""
    owned_q = (
        select(Organization)
        .options(selectinload(Organization.settings))
        .where(
            Organization.owner_id == user_id,
            Organization.is_deleted.is_(False),
        )
    )
    owned_result = await session.execute(owned_q)
    owned = list(owned_result.scalars().all())
    owned_ids = {o.id for o in owned}

    member_q = (
        select(Organization)
        .join(OrganizationMember)
        .options(selectinload(Organization.settings))
        .where(
            OrganizationMember.user_id == user_id,
            Organization.is_deleted.is_(False),
        )
    )
    member_result = await session.execute(member_q)
    member_orgs = [o for o in member_result.scalars().all() if o.id not in owned_ids]

    return owned + member_orgs


async def batch_get_my_roles(
    session: AsyncSession,
    orgs: list[Organization],
    user_id: uuid.UUID,
) -> dict[uuid.UUID, tuple[str | None, object | None]]:
    """Return {org_id: (my_role, my_custom_role)} for the given orgs in one membership query.

    my_role is 'owner'/'admin'/'employee' or None if the user is neither
    owner nor member (possible for super_admin viewing /organizations/all).
    my_custom_role is OrganizationRole or None.
    """
    if not orgs:
        return {}

    result: dict[uuid.UUID, tuple[str | None, object | None]] = {}
    non_owned_ids: list[uuid.UUID] = []
    for org in orgs:
        if org.owner_id == user_id:
            result[org.id] = ("owner", None)
        else:
            non_owned_ids.append(org.id)

    if non_owned_ids:
        member_result = await session.execute(
            select(OrganizationMember)
            .options(selectinload(OrganizationMember.custom_role))
            .where(
                OrganizationMember.user_id == user_id,
                OrganizationMember.organization_id.in_(non_owned_ids),
            )
        )
        by_org = {m.organization_id: m for m in member_result.scalars().all()}
        for org_id in non_owned_ids:
            member = by_org.get(org_id)
            if member is None:
                result[org_id] = (None, None)
            else:
                result[org_id] = (member.role.value, member.custom_role)

    return result


async def update_organization(
    session: AsyncSession,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    name: str,
) -> Organization:
    org = await get_organization(session, org_id)
    # Переименование — управляющее действие: доступно owner, admin-участнику и super_admin.
    await ensure_admin_or_owner(session, org, actor_id)
    org.name = name
    await session.flush()
    return org


async def delete_organization(
    session: AsyncSession,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> None:
    org = await get_organization(session, org_id)
    await ensure_owner(session, org, owner_id)
    org.is_deleted = True
    await session.flush()
    logger.info("organization_deleted", org_id=str(org_id))


async def rotate_invite_code(
    session: AsyncSession,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> str:
    org = await get_organization(session, org_id)
    # Онбординг сотрудников — функция админа: ротировать код может owner и admin.
    await ensure_admin_or_owner(session, org, actor_id)
    org.invite_code = _generate_invite_code()
    await session.flush()
    return org.invite_code


async def join_by_invite(
    session: AsyncSession,
    invite_code: str,
    user_id: uuid.UUID,
) -> tuple[Organization, OrganizationMember]:
    result = await session.execute(
        select(Organization)
        .options(selectinload(Organization.settings))
        .where(
            Organization.invite_code == invite_code,
            Organization.is_deleted.is_(False),
        )
    )
    org = result.scalar_one_or_none()
    if org is None:
        raise OrgError("INVALID_INVITE", "Неверный инвайт-код", 404)

    if org.owner_id == user_id:
        raise OrgError("OWNER_CANNOT_JOIN", "Владелец не может присоединиться как участник", 400)

    existing = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.user_id == user_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise OrgError("ALREADY_MEMBER", "Вы уже состоите в этой организации", 409)

    member = OrganizationMember(
        organization_id=org.id,
        user_id=user_id,
        role=MemberRole.employee,
    )
    session.add(member)
    await session.flush()
    logger.info("member_joined", org_id=str(org.id), user_id=str(user_id))
    return org, member


async def get_members(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[OrganizationMember]:
    org = await get_organization(session, org_id)
    await _check_org_access(session, org, requester_id)

    result = await session.execute(
        select(OrganizationMember)
        .options(
            selectinload(OrganizationMember.user),
            selectinload(OrganizationMember.custom_role),
        )
        .where(OrganizationMember.organization_id == org_id)
    )
    return list(result.scalars().all())


async def remove_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> uuid.UUID:
    """Удалить участника. Возвращает id записи membership (для аудита)."""
    org = await get_organization(session, org_id)

    result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == member_user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise OrgError("MEMBER_NOT_FOUND", "Участник не найден", 404)
    member_id = member.id

    # Self-leave: any member can leave
    if member_user_id == requester_id:
        await session.delete(member)
        await session.flush()
        return member_id

    # Owner can remove anyone
    if org.owner_id == requester_id:
        await session.delete(member)
        await session.flush()
        return member_id

    # Admin can remove
    requester_member = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == requester_id,
        )
    )
    req = requester_member.scalar_one_or_none()
    if req is None or req.role != MemberRole.admin:
        raise OrgError("FORBIDDEN", "Нет прав для удаления участника", 403)

    await session.delete(member)
    await session.flush()
    logger.info("member_removed", org_id=str(org_id), user_id=str(member_user_id))
    return member_id


async def get_all_organizations(
    session: AsyncSession,
) -> list[Organization]:
    """Get all active organizations (for super_admin)."""
    result = await session.execute(
        select(Organization)
        .options(selectinload(Organization.settings))
        .where(Organization.is_deleted.is_(False))
    )
    return list(result.scalars().all())


async def update_member_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    new_role: str,
    requester_id: uuid.UUID,
    is_super_admin: bool = False,
) -> OrganizationMember:
    org = await get_organization(session, org_id)

    # Only owner or super_admin can change roles
    if not is_super_admin and org.owner_id != requester_id:
        raise OrgError("FORBIDDEN", "Только владелец или super_admin может менять роли", 403)

    try:
        role_enum = MemberRole(new_role)
    except ValueError:
        raise OrgError(
            "INVALID_ROLE",
            f"Роль должна быть: {', '.join(r.value for r in MemberRole)}",
            400,
        ) from None

    result = await session.execute(
        select(OrganizationMember)
        .options(
            selectinload(OrganizationMember.user),
            selectinload(OrganizationMember.custom_role),
        )
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == member_user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise OrgError("MEMBER_NOT_FOUND", "Участник не найден", 404)

    member.role = role_enum
    await session.flush()
    logger.info(
        "member_role_updated",
        org_id=str(org_id),
        user_id=str(member_user_id),
        new_role=new_role,
    )
    return member


def normalize_display_name(raw: str | None) -> str | None:
    """Нормализовать `display_name` участника (`member_display_name`).

    - `None` → `None` (сброс на настоящее имя);
    - внутренние `\\n`/`\\r`/`\\t` схлопываются в обычный пробел, затем обрезаются
      пробелы по краям;
    - пустая строка/строка из одних пробелов после нормализации → `None`
      (тоже трактуется как сброс, а не ошибка);
    - длина 1–100 символов, иначе `INVALID_DISPLAY_NAME`;
    - прочие управляющие символы (не перевод строки/таб) отклоняются той же ошибкой.
    """
    if raw is None:
        return None

    collapsed = re.sub(r"[\n\r\t]+", " ", raw)
    stripped = collapsed.strip()
    if not stripped:
        return None

    if _FORBIDDEN_CONTROL_CHARS.search(stripped) or len(stripped) > DISPLAY_NAME_MAX_LENGTH:
        raise OrgError(
            "INVALID_DISPLAY_NAME",
            "Имя в организации: от 1 до 100 символов",
            400,
        )
    return stripped


async def update_member_display_name(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    raw_display_name: str | None,
    requester_id: uuid.UUID,
) -> tuple[OrganizationMember, str | None, str | None]:
    """Задать/сбросить `display_name` участника. Возвращает (member, old, new) для аудита.

    Права — тот же хелпер, что и у прочих управляющих операций над участниками:
    owner, admin-участник организации или super_admin. Сотрудник (в т.ч. себе)
    получает `FORBIDDEN` (403) — это управленческий атрибут, не самообслуживание.
    """
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)

    result = await session.execute(
        select(OrganizationMember)
        .options(
            selectinload(OrganizationMember.user),
            selectinload(OrganizationMember.custom_role),
        )
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == member_user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise OrgError("MEMBER_NOT_FOUND", "Участник не найден", 404)

    new_value = normalize_display_name(raw_display_name)
    old_value = member.display_name
    member.display_name = new_value
    await session.flush()
    logger.info(
        "member_display_name_updated",
        org_id=str(org_id),
        user_id=str(member_user_id),
    )
    return member, old_value, new_value


async def _check_org_access(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
) -> None:
    """Владелец, участник или super_admin. Делегирует в services.common."""
    await ensure_member(session, org, user_id)
