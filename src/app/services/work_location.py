import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.work_location import WorkLocation
from src.app.services.organization import OrgError, get_organization, _check_org_access


async def create_work_location(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    name: str,
    latitude: float,
    longitude: float,
    radius_meters: int = 100,
) -> WorkLocation:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    location = WorkLocation(
        organization_id=org_id,
        name=name,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius_meters,
    )
    session.add(location)
    await session.flush()
    return location


async def get_work_locations(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[WorkLocation]:
    org = await get_organization(session, org_id)
    await _check_org_access(session, org, requester_id)

    result = await session.execute(
        select(WorkLocation).where(WorkLocation.organization_id == org_id)
    )
    return list(result.scalars().all())


async def update_work_location(
    session: AsyncSession,
    org_id: uuid.UUID,
    location_id: uuid.UUID,
    requester_id: uuid.UUID,
    **fields,
) -> WorkLocation:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    location = await _get_location(session, org_id, location_id)
    for key, value in fields.items():
        if value is not None:
            setattr(location, key, value)
    await session.flush()
    return location


async def delete_work_location(
    session: AsyncSession,
    org_id: uuid.UUID,
    location_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    location = await _get_location(session, org_id, location_id)
    await session.delete(location)
    await session.flush()


async def _get_location(
    session: AsyncSession,
    org_id: uuid.UUID,
    location_id: uuid.UUID,
) -> WorkLocation:
    result = await session.execute(
        select(WorkLocation).where(
            WorkLocation.id == location_id,
            WorkLocation.organization_id == org_id,
        )
    )
    location = result.scalar_one_or_none()
    if location is None:
        raise OrgError("LOCATION_NOT_FOUND", "Точка не найдена", 404)
    return location


async def _check_admin_or_owner(session, org, user_id):
    """Only owner or admin can manage locations."""
    from src.app.models.organization import MemberRole, OrganizationMember

    if org.owner_id == user_id:
        return

    result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org.id,
            OrganizationMember.user_id == user_id,
            OrganizationMember.role == MemberRole.admin,
        )
    )
    if result.scalar_one_or_none() is None:
        raise OrgError("FORBIDDEN", "Нет прав для управления точками", 403)
