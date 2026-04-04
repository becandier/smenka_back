import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.organization_settings import OrganizationSettings
from src.app.services.organization import OrgError, get_organization, _check_owner


async def get_settings(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> OrganizationSettings:
    org = await get_organization(session, org_id)
    _check_owner(org, requester_id)

    result = await session.execute(
        select(OrganizationSettings).where(
            OrganizationSettings.organization_id == org_id,
        )
    )
    settings = result.scalar_one_or_none()
    if settings is None:
        raise OrgError("SETTINGS_NOT_FOUND", "Настройки не найдены", 404)
    return settings


async def update_settings(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    **fields,
) -> OrganizationSettings:
    settings = await get_settings(session, org_id, requester_id)
    for key, value in fields.items():
        if value is not None:
            setattr(settings, key, value)
    await session.flush()
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
