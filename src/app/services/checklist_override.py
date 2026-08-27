import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.checklist import (
    ChecklistMemberOverride,
    ChecklistTemplate,
    OverrideType,
)
from src.app.services import entitlements
from src.app.services.checklist_assignment import _get_member
from src.app.services.checklist_template import (
    ChecklistError,
    _check_admin_or_owner,
    _get_template,
)
from src.app.services.organization import _check_org_access, get_organization

logger = get_logger(__name__)


async def list_member_overrides(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[tuple[ChecklistMemberOverride, ChecklistTemplate]]:
    """List all overrides for a member (включая архивные шаблоны)."""
    org = await get_organization(session, org_id)

    if requester_id != user_id:
        await _check_admin_or_owner(session, org, requester_id)
    else:
        await _check_org_access(session, org, requester_id)

    member = await _get_member(session, org_id, user_id)

    result = await session.execute(
        select(ChecklistMemberOverride, ChecklistTemplate)
        .join(
            ChecklistTemplate,
            ChecklistTemplate.id == ChecklistMemberOverride.template_id,
        )
        .where(
            ChecklistMemberOverride.member_id == member.id,
            ChecklistTemplate.organization_id == org_id,
        )
        .order_by(ChecklistMemberOverride.created_at)
    )
    return [(ov, tpl) for ov, tpl in result.all()]


async def upsert_override(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user_id: uuid.UUID,
    override_type_raw: str,
    requester_id: uuid.UUID,
) -> OverrideType:
    """Idempotent upsert of one override via INSERT ... ON CONFLICT DO UPDATE."""
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)

    try:
        override_type = OverrideType(override_type_raw)
    except ValueError:
        raise ChecklistError(
            "INVALID_OVERRIDE_TYPE",
            "type должен быть add или remove",
            400,
        ) from None

    template = await _get_template(session, org_id, template_id)
    if template.is_deleted:
        raise ChecklistError(
            "TEMPLATE_ARCHIVED",
            "Нельзя создавать override для удалённого шаблона",
            400,
        )

    member = await _get_member(session, org_id, user_id)

    stmt = (
        pg_insert(ChecklistMemberOverride)
        .values(
            template_id=template_id,
            member_id=member.id,
            override_type=override_type,
        )
        .on_conflict_do_update(
            constraint="uq_checklist_member_override",
            set_={"override_type": override_type},
        )
    )
    await session.execute(stmt)
    await session.flush()

    logger.info(
        "member_override_upserted",
        org_id=str(org_id),
        template_id=str(template_id),
        user_id=str(user_id),
        type=override_type.value,
    )
    return override_type


async def delete_override(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Idempotent delete — no error if override doesn't exist."""
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)

    # Validate template belongs to org (consistent with PUT).
    await _get_template(session, org_id, template_id)
    member = await _get_member(session, org_id, user_id)

    result = await session.execute(
        select(ChecklistMemberOverride).where(
            ChecklistMemberOverride.template_id == template_id,
            ChecklistMemberOverride.member_id == member.id,
        )
    )
    override = result.scalar_one_or_none()
    if override is not None:
        await session.delete(override)
        await session.flush()
        logger.info(
            "member_override_deleted",
            org_id=str(org_id),
            template_id=str(template_id),
            user_id=str(user_id),
        )
