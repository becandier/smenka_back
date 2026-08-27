import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.organization import Organization
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.work_location import WorkLocation
from src.app.services import entitlements
from src.app.services.common import ensure_admin_or_owner
from src.app.services.organization import OrgError, _check_org_access, get_organization
from src.app.utils.geo import haversine_distance

logger = get_logger(__name__)


async def resolve_nearest_work_location(
    session: AsyncSession,
    org_id: uuid.UUID,
    latitude: float,
    longitude: float,
) -> WorkLocation | None:
    """Ближайшая точка организации среди совпавших по радиусу (Haversine).

    Общий хелпер для `_resolve_org_shift_start` (`services/shift.py`, старт
    org-смены при `geo_check_enabled=true`) и `get_my_schedules` (резолв
    точки для превью графиков ДО старта смены). `None`, если ни одна зона
    организации не совпала — вызывающая сторона решает, ошибка это или нет:
    для старта смены — да (`GEO_CHECK_FAILED`), для `my-schedules` — нет.
    """
    result = await session.execute(
        select(WorkLocation).where(WorkLocation.organization_id == org_id)
    )
    locations = list(result.scalars().all())

    matched = []
    for loc in locations:
        distance = haversine_distance(latitude, longitude, loc.latitude, loc.longitude)
        if distance <= loc.radius_meters:
            matched.append((loc, distance))
    if not matched:
        return None
    return min(matched, key=lambda pair: pair[1])[0]


async def create_work_location(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    name: str,
    latitude: float,
    longitude: float,
    radius_meters: int = 100,
    address: str | None = None,
) -> WorkLocation:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    await entitlements.require_capacity(session, org, entitlements.LimitKind.locations)

    location = WorkLocation(
        organization_id=org_id,
        name=name,
        latitude=latitude,
        longitude=longitude,
        radius_meters=radius_meters,
        address=address,
    )
    session.add(location)
    await session.flush()
    logger.info("work_location_created", org_id=str(org_id), location_id=str(location.id))
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
    **fields: Any,
) -> WorkLocation:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)

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
    await entitlements.require_active_subscription(session, org, requester_id)

    location = await _get_location(session, org_id, location_id)
    await session.delete(location)
    await session.flush()

    # Auto-disable geo_check / require_work_location if no locations left:
    # обе настройки требуют хотя бы одной точки, иначе старт смены стал бы невозможен.
    remaining = await session.execute(
        select(func.count())
        .select_from(WorkLocation)
        .where(
            WorkLocation.organization_id == org_id,
        )
    )
    if remaining.scalar_one() == 0:
        settings_result = await session.execute(
            select(OrganizationSettings).where(
                OrganizationSettings.organization_id == org_id,
            )
        )
        org_settings = settings_result.scalar_one_or_none()
        if org_settings is not None:
            changed = False
            if org_settings.geo_check_enabled:
                org_settings.geo_check_enabled = False
                changed = True
                logger.info("geo_check_auto_disabled", org_id=str(org_id))
            if org_settings.require_work_location:
                org_settings.require_work_location = False
                changed = True
                logger.info("require_work_location_auto_disabled", org_id=str(org_id))
            if changed:
                await session.flush()

    logger.info("work_location_deleted", org_id=str(org_id), location_id=str(location_id))


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
        message="Нет прав для управления точками",
    )
