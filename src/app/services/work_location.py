import uuid
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class LocationDistance:
    """Точка организации + расстояние (метры, Haversine) до заданных координат."""

    location: WorkLocation
    distance_meters: float

    @property
    def within_radius(self) -> bool:
        return self.distance_meters <= self.location.radius_meters


def _sort_key(pair: LocationDistance) -> tuple[float, datetime, uuid.UUID]:
    """Детерминированный тай-брейк порядка точек: расстояние → `created_at` → `id`.

    Без него порядок при равном расстоянии зависел бы от порядка выдачи БД
    (никем не гарантирован) — один и тот же запрос мог бы отдавать разных
    победителей. Общий тай-брейк для `nearby` и автоматического резолва при
    старте смены (shift_start_location_choice/backend.md).
    """
    return (pair.distance_meters, pair.location.created_at, pair.location.id)


async def _sorted_org_locations(
    session: AsyncSession,
    org_id: uuid.UUID,
    latitude: float,
    longitude: float,
) -> list[LocationDistance]:
    """Все точки организации с расстоянием до координат, отсортированные детерминированно.

    Единственное место, где считается Haversine для точек организации, —
    используется и `resolve_nearest_work_location` (старт смены), и
    `get_nearby_work_locations` (`GET .../work-locations/nearby`), чтобы список,
    показанный сотруднику, не мог разойтись с тем, что примет старт.
    """
    result = await session.execute(
        select(WorkLocation).where(WorkLocation.organization_id == org_id)
    )
    locations = list(result.scalars().all())

    pairs = [
        LocationDistance(
            location=loc,
            distance_meters=haversine_distance(latitude, longitude, loc.latitude, loc.longitude),
        )
        for loc in locations
    ]
    pairs.sort(key=_sort_key)
    return pairs


async def resolve_nearest_work_location(
    session: AsyncSession,
    org_id: uuid.UUID,
    latitude: float,
    longitude: float,
) -> WorkLocation | None:
    """Ближайшая точка организации среди совпавших по радиусу (Haversine).

    Общий хелпер для `_resolve_org_shift_start` (`services/shift.py`, старт
    org-смены при `geo_check_enabled=true` и без явного выбора точки) и
    `get_my_schedules` (резолв точки для превью графиков ДО старта смены).
    `None`, если ни одна зона организации не совпала — вызывающая сторона
    решает, ошибка это или нет: для старта смены — да (`GEO_CHECK_FAILED`),
    для `my-schedules` — нет. При равном расстоянии — детерминированный
    тай-брейк (`_sort_key`).
    """
    pairs = await _sorted_org_locations(session, org_id, latitude, longitude)
    matched = [p for p in pairs if p.within_radius]
    return matched[0].location if matched else None


async def get_nearby_work_locations(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    latitude: float,
    longitude: float,
) -> tuple[list[LocationDistance], LocationDistance | None]:
    """Точки организации, подходящие сотруднику по радиусу (`GET .../work-locations/nearby`).

    Возвращает `(matched, nearest_outside)`:
    - `matched` — точки, в чей радиус попали координаты, по возрастанию
      расстояния (тай-брейк `_sort_key`); первая — ближайшая (`is_nearest`).
    - `nearest_outside` — ближайшая точка организации ВНЕ радиуса, либо
      `None`, если у организации нет точек за пределами `matched`.

    Тот же расчёт (`_sorted_org_locations`), что и `resolve_nearest_work_location`,
    используемый при старте смены, — список не может разойтись с тем, что
    примет старт.
    """
    org = await get_organization(session, org_id)
    await _check_org_access(session, org, requester_id)

    pairs = await _sorted_org_locations(session, org_id, latitude, longitude)
    matched = [p for p in pairs if p.within_radius]
    outside = [p for p in pairs if not p.within_radius]
    nearest_outside = outside[0] if outside else None
    return matched, nearest_outside


async def get_org_location_distance(
    session: AsyncSession,
    org_id: uuid.UUID,
    location_id: uuid.UUID,
    latitude: float,
    longitude: float,
) -> LocationDistance | None:
    """Расстояние от координат до одной точки организации.

    `None`, если точка с таким id не существует или принадлежит другой
    организации — используется при старте смены для валидации явно
    выбранного сотрудником `work_location_id` при `geo_check_enabled=true`
    (shift_start_location_choice/backend.md): та же формула (`haversine_distance`),
    что и в `_sorted_org_locations`.
    """
    result = await session.execute(
        select(WorkLocation).where(
            WorkLocation.id == location_id,
            WorkLocation.organization_id == org_id,
        )
    )
    location = result.scalar_one_or_none()
    if location is None:
        return None
    return LocationDistance(
        location=location,
        distance_meters=haversine_distance(
            latitude, longitude, location.latitude, location.longitude
        ),
    )


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
