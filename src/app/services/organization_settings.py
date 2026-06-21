import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.organization import Organization
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.work_location import WorkLocation
from src.app.services.common import ensure_admin_or_owner
from src.app.services.organization import OrgError, get_organization

logger = get_logger(__name__)


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
        message="Нет прав для управления настройками",
    )


async def get_settings(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> OrganizationSettings:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    result = await session.execute(
        select(OrganizationSettings).where(
            OrganizationSettings.organization_id == org_id,
        )
    )
    settings = result.scalar_one_or_none()
    if settings is None:
        raise OrgError("SETTINGS_NOT_FOUND", "Настройки не найдены", 404)
    return settings


async def _count_locations(session: AsyncSession, org_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(WorkLocation)
        .where(
            WorkLocation.organization_id == org_id,
        )
    )
    return result.scalar_one()


async def update_settings(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    **fields: Any,
) -> OrganizationSettings:
    settings = await get_settings(session, org_id, requester_id)

    if fields.get("geo_check_enabled") is True and not settings.geo_check_enabled:
        count = await _count_locations(session, org_id)
        if count == 0:
            raise OrgError(
                "NO_WORK_LOCATIONS",
                "Сначала добавьте рабочую точку",
                400,
            )

    if (
        fields.get("require_work_location") is True
        and not settings.require_work_location
        and await _count_locations(session, org_id) == 0
    ):
        raise OrgError(
            "WORK_LOCATION_REQUIRED_NO_LOCATIONS",
            "Нельзя требовать точку: у организации нет ни одной рабочей точки",
            409,
        )

    for key, value in fields.items():
        setattr(settings, key, value)
    await session.flush()
    logger.info("org_settings_updated", org_id=str(org_id))
    return settings


async def get_settings_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> OrganizationSettings | None:
    """Get settings without permission check (for internal use in shift service)."""
    result = await session.execute(
        select(OrganizationSettings).where(
            OrganizationSettings.organization_id == org_id,
        )
    )
    return result.scalar_one_or_none()
