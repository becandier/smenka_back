import secrets
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.models.organization import MemberRole, Organization, OrganizationMember


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
    return org


async def get_organization(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> Organization:
    result = await session.execute(
        select(Organization).where(
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
    owned_q = select(Organization).where(
        Organization.owner_id == user_id,
        Organization.is_deleted.is_(False),
    )
    owned_result = await session.execute(owned_q)
    owned = list(owned_result.scalars().all())
    owned_ids = {o.id for o in owned}

    member_q = (
        select(Organization)
        .join(OrganizationMember)
        .where(
            OrganizationMember.user_id == user_id,
            Organization.is_deleted.is_(False),
        )
    )
    member_result = await session.execute(member_q)
    member_orgs = [o for o in member_result.scalars().all() if o.id not in owned_ids]

    return owned + member_orgs


async def update_organization(
    session: AsyncSession,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    name: str,
) -> Organization:
    org = await get_organization(session, org_id)
    _check_owner(org, owner_id)
    org.name = name
    await session.flush()
    return org


async def delete_organization(
    session: AsyncSession,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> None:
    org = await get_organization(session, org_id)
    _check_owner(org, owner_id)
    org.is_deleted = True
    await session.flush()


async def rotate_invite_code(
    session: AsyncSession,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
) -> str:
    org = await get_organization(session, org_id)
    _check_owner(org, owner_id)
    org.invite_code = _generate_invite_code()
    await session.flush()
    return org.invite_code


async def join_by_invite(
    session: AsyncSession,
    invite_code: str,
    user_id: uuid.UUID,
) -> tuple[Organization, OrganizationMember]:
    result = await session.execute(
        select(Organization).where(
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
        .options(selectinload(OrganizationMember.user))
        .where(OrganizationMember.organization_id == org_id)
    )
    return list(result.scalars().all())


async def remove_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
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

    # Self-leave: any member can leave
    if member_user_id == requester_id:
        await session.delete(member)
        await session.flush()
        return

    # Owner can remove anyone
    if org.owner_id == requester_id:
        await session.delete(member)
        await session.flush()
        return

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


def _check_owner(org: Organization, user_id: uuid.UUID) -> None:
    if org.owner_id != user_id:
        raise OrgError("FORBIDDEN", "Только владелец может выполнить это действие", 403)


async def _check_org_access(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
) -> None:
    """Check that user is owner or member of the org."""
    if org.owner_id == user_id:
        return

    result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.user_id == user_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise OrgError("FORBIDDEN", "Нет доступа к организации", 403)
