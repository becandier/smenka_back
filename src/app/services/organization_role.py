import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.organization import Organization, OrganizationMember
from src.app.models.organization_role import OrganizationRole
from src.app.services.common import ensure_admin_or_owner
from src.app.services.organization import _check_org_access, get_organization

logger = get_logger(__name__)


class RoleError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


async def _check_admin_or_owner(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
) -> None:
    """Владелец, admin или super_admin. Делегирует в services.common."""
    await ensure_admin_or_owner(
        session,
        org,
        user_id,
        message="Нет прав для управления ролями",
    )


async def _get_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    role_id: uuid.UUID,
) -> OrganizationRole:
    result = await session.execute(
        select(OrganizationRole).where(
            OrganizationRole.id == role_id,
            OrganizationRole.organization_id == org_id,
        )
    )
    role = result.scalar_one_or_none()
    if role is None:
        raise RoleError("ROLE_NOT_FOUND", "Роль не найдена", 404)
    return role


async def create_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    requester_id: uuid.UUID,
) -> OrganizationRole:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    existing = await session.execute(
        select(OrganizationRole).where(
            OrganizationRole.organization_id == org_id,
            OrganizationRole.name == name,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise RoleError("ROLE_NAME_TAKEN", "Роль с таким именем уже существует", 409)

    role = OrganizationRole(organization_id=org_id, name=name)
    session.add(role)
    await session.flush()
    logger.info(
        "role_created",
        org_id=str(org_id),
        role_id=str(role.id),
        name=name,
    )
    return role


async def get_roles(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[OrganizationRole]:
    org = await get_organization(session, org_id)
    await _check_org_access(session, org, requester_id)
    result = await session.execute(
        select(OrganizationRole)
        .where(OrganizationRole.organization_id == org_id)
        .order_by(OrganizationRole.created_at)
    )
    return list(result.scalars().all())


async def update_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    role_id: uuid.UUID,
    name: str,
    requester_id: uuid.UUID,
) -> OrganizationRole:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    role = await _get_role(session, org_id, role_id)

    if role.name == name:
        return role

    existing = await session.execute(
        select(OrganizationRole).where(
            OrganizationRole.organization_id == org_id,
            OrganizationRole.name == name,
            OrganizationRole.id != role_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise RoleError("ROLE_NAME_TAKEN", "Роль с таким именем уже существует", 409)

    role.name = name
    await session.flush()
    logger.info(
        "role_updated",
        org_id=str(org_id),
        role_id=str(role_id),
        name=name,
    )
    return role


async def delete_role(
    session: AsyncSession,
    org_id: uuid.UUID,
    role_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    role = await _get_role(session, org_id, role_id)
    await session.delete(role)
    await session.flush()
    logger.info(
        "role_deleted",
        org_id=str(org_id),
        role_id=str(role_id),
    )


async def assign_role_to_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: uuid.UUID | None,
    requester_id: uuid.UUID,
) -> OrganizationMember:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    if role_id is not None:
        await _get_role(session, org_id, role_id)

    result = await session.execute(
        select(OrganizationMember)
        .options(
            selectinload(OrganizationMember.user),
            selectinload(OrganizationMember.custom_role),
        )
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise RoleError("MEMBER_NOT_FOUND", "Участник не найден", 404)

    member.role_id = role_id
    await session.flush()
    await session.refresh(member, ["custom_role"])
    logger.info(
        "member_custom_role_assigned",
        org_id=str(org_id),
        user_id=str(user_id),
        role_id=str(role_id) if role_id else None,
    )
    return member
