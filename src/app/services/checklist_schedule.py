"""Назначение шаблонов чек-листов графикам работы."""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.checklist import ChecklistTemplateSchedule
from src.app.models.work_schedule import WorkSchedule
from src.app.services import entitlements
from src.app.services.checklist_template import (
    ChecklistError,
    _check_admin_or_owner,
    _get_template,
    _replace_m2m_links,
)
from src.app.services.organization import get_organization

logger = get_logger(__name__)


async def set_template_schedules(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    schedule_ids: list[uuid.UUID],
    requester_id: uuid.UUID,
) -> list[uuid.UUID]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    await _get_template(session, org_id, template_id)

    if schedule_ids:
        result = await session.execute(
            select(WorkSchedule.id).where(
                WorkSchedule.id.in_(schedule_ids),
                WorkSchedule.organization_id == org_id,
                WorkSchedule.is_paused.is_(False),
            )
        )
        if {row[0] for row in result.all()} != set(schedule_ids):
            raise ChecklistError("SCHEDULE_NOT_FOUND", "График не найден", 404)

    existing = await session.execute(
        select(ChecklistTemplateSchedule).where(
            ChecklistTemplateSchedule.template_id == template_id
        )
    )
    target = set(schedule_ids)
    await _replace_m2m_links(
        session,
        existing.scalars().all(),
        key_of=lambda row: row.work_schedule_id,
        target_ids=target,
        make_new=lambda schedule_id: ChecklistTemplateSchedule(
            template_id=template_id, work_schedule_id=schedule_id
        ),
    )
    logger.info(
        "checklist_template_schedules_assigned",
        org_id=str(org_id),
        template_id=str(template_id),
        schedule_count=len(target),
    )
    return list(target)


async def get_schedule_ids_for_templates(
    session: AsyncSession,
    template_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    ids = list(template_ids)
    if not ids:
        return {}
    result = await session.execute(
        select(
            ChecklistTemplateSchedule.template_id,
            ChecklistTemplateSchedule.work_schedule_id,
        ).where(ChecklistTemplateSchedule.template_id.in_(ids))
    )
    mapping: dict[uuid.UUID, list[uuid.UUID]] = {}
    for template_id, schedule_id in result.all():
        mapping.setdefault(template_id, []).append(schedule_id)
    return mapping
