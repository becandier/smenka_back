import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.checklist import (
    ChecklistInstance,
    ChecklistInstanceItem,
    ChecklistInstanceStatus,
    ChecklistTemplateItem,
)
from src.app.models.organization import OrganizationMember
from src.app.models.shift import Shift
from src.app.services.checklist_assignment import _compute_effective
from src.app.services.checklist_template import ChecklistError

logger = get_logger(__name__)


async def create_instances_for_shift(
    session: AsyncSession,
    shift: Shift,
    member: OrganizationMember,
) -> list[ChecklistInstance]:
    """Create checklist instances (snapshots) at shift start for org shifts."""
    if shift.organization_id is None:
        return []

    effective_pairs = await _compute_effective(
        session,
        shift.organization_id,
        member,
    )
    if not effective_pairs:
        return []

    template_ids = [t.id for t, _ in effective_pairs]
    items_result = await session.execute(
        select(ChecklistTemplateItem)
        .where(ChecklistTemplateItem.template_id.in_(template_ids))
        .order_by(ChecklistTemplateItem.position)
    )
    items_by_tpl: dict[uuid.UUID, list[ChecklistTemplateItem]] = {}
    for item in items_result.scalars().all():
        items_by_tpl.setdefault(item.template_id, []).append(item)

    created: list[ChecklistInstance] = []
    for template, _source in effective_pairs:
        instance = ChecklistInstance(
            shift_id=shift.id,
            template_id=template.id,
            name=template.name,
            type=template.type,
            is_required=template.is_required,
            status=ChecklistInstanceStatus.pending,
        )
        session.add(instance)
        await session.flush()

        tpl_items = items_by_tpl.get(template.id, [])
        for tpl_item in tpl_items:
            session.add(
                ChecklistInstanceItem(
                    instance_id=instance.id,
                    template_item_id=tpl_item.id,
                    text=tpl_item.text,
                    is_required=tpl_item.is_required,
                    position=tpl_item.position,
                )
            )

        # Shortcut: if no required items, instance is already completed.
        has_required_items = any(it.is_required for it in tpl_items)
        if not has_required_items:
            instance.status = ChecklistInstanceStatus.completed
            instance.completed_at = datetime.now(UTC)

        created.append(instance)

    await session.flush()
    logger.info(
        "checklist_instances_created",
        shift_id=str(shift.id),
        count=len(created),
    )
    return created


async def _check_shift_access(
    session: AsyncSession,
    shift: Shift,
    requester_id: uuid.UUID,
) -> None:
    if shift.user_id == requester_id:
        return
    if shift.organization_id is None:
        raise ChecklistError("FORBIDDEN", "Нет доступа к чек-листам смены", 403)

    from src.app.models.organization import MemberRole, Organization

    org_result = await session.execute(
        select(Organization).where(Organization.id == shift.organization_id)
    )
    org = org_result.scalar_one_or_none()
    if org is None:
        raise ChecklistError("FORBIDDEN", "Нет доступа к чек-листам смены", 403)
    if org.owner_id == requester_id:
        return

    member_result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == shift.organization_id,
            OrganizationMember.user_id == requester_id,
            OrganizationMember.role == MemberRole.admin,
        )
    )
    if member_result.scalar_one_or_none() is None:
        raise ChecklistError("FORBIDDEN", "Нет доступа к чек-листам смены", 403)


async def _get_shift(session: AsyncSession, shift_id: uuid.UUID) -> Shift:
    result = await session.execute(select(Shift).where(Shift.id == shift_id))
    shift = result.scalar_one_or_none()
    if shift is None:
        raise ChecklistError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


async def get_shift_checklists(
    session: AsyncSession,
    shift_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[tuple[ChecklistInstance, int, int]]:
    shift = await _get_shift(session, shift_id)
    await _check_shift_access(session, shift, requester_id)

    result = await session.execute(
        select(ChecklistInstance)
        .where(ChecklistInstance.shift_id == shift_id)
        .order_by(ChecklistInstance.created_at)
    )
    instances = list(result.scalars().all())
    if not instances:
        return []

    items_result = await session.execute(
        select(
            ChecklistInstanceItem.instance_id,
            ChecklistInstanceItem.is_completed,
        ).where(ChecklistInstanceItem.instance_id.in_([i.id for i in instances]))
    )
    totals: dict[uuid.UUID, int] = {i.id: 0 for i in instances}
    completed: dict[uuid.UUID, int] = {i.id: 0 for i in instances}
    for inst_id, is_done in items_result.all():
        totals[inst_id] += 1
        if is_done:
            completed[inst_id] += 1

    return [(inst, totals[inst.id], completed[inst.id]) for inst in instances]


async def get_instance_detail(
    session: AsyncSession,
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> ChecklistInstance:
    shift = await _get_shift(session, shift_id)
    await _check_shift_access(session, shift, requester_id)

    result = await session.execute(
        select(ChecklistInstance)
        .options(selectinload(ChecklistInstance.items))
        .where(
            ChecklistInstance.id == instance_id,
            ChecklistInstance.shift_id == shift_id,
        )
    )
    instance = result.scalar_one_or_none()
    if instance is None:
        raise ChecklistError("INSTANCE_NOT_FOUND", "Экземпляр не найден", 404)
    return instance


async def update_instance_item(
    session: AsyncSession,
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    item_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    is_completed: bool,
    comment: str | None,
) -> ChecklistInstanceItem:
    shift = await _get_shift(session, shift_id)

    if shift.user_id != requester_id:
        raise ChecklistError("FORBIDDEN", "Заполнять может только владелец смены", 403)

    from src.app.models.shift import ShiftStatus

    if shift.status == ShiftStatus.finished:
        raise ChecklistError(
            "SHIFT_FINISHED",
            "Нельзя редактировать чек-листы завершённой смены",
            400,
        )

    instance_result = await session.execute(
        select(ChecklistInstance).where(
            ChecklistInstance.id == instance_id,
            ChecklistInstance.shift_id == shift_id,
        )
    )
    instance = instance_result.scalar_one_or_none()
    if instance is None:
        raise ChecklistError("INSTANCE_NOT_FOUND", "Экземпляр не найден", 404)

    item_result = await session.execute(
        select(ChecklistInstanceItem).where(
            ChecklistInstanceItem.id == item_id,
            ChecklistInstanceItem.instance_id == instance_id,
        )
    )
    item = item_result.scalar_one_or_none()
    if item is None:
        raise ChecklistError("ITEM_NOT_FOUND", "Пункт не найден", 404)

    now = datetime.now(UTC)
    if item.is_completed != is_completed:
        item.is_completed = is_completed
        item.completed_at = now if is_completed else None
    item.comment = comment
    item.change_count = (item.change_count or 0) + 1

    await session.flush()

    required_pending_result = await session.execute(
        select(func.count()).where(
            ChecklistInstanceItem.instance_id == instance_id,
            ChecklistInstanceItem.is_required.is_(True),
            ChecklistInstanceItem.is_completed.is_(False),
        )
    )
    pending_required = required_pending_result.scalar_one()

    if pending_required == 0:
        if instance.status != ChecklistInstanceStatus.completed:
            instance.status = ChecklistInstanceStatus.completed
            instance.completed_at = now
    else:
        if instance.status != ChecklistInstanceStatus.pending:
            instance.status = ChecklistInstanceStatus.pending
            instance.completed_at = None

    await session.flush()
    return item


async def finalize_shift_checklists(
    session: AsyncSession,
    shift_id: uuid.UUID,
) -> bool:
    """Mark pending required instances as incomplete.

    Returns True if the shift has any incomplete required checklists.
    Caller is responsible for setting shift.has_incomplete_required_checklists.
    """
    await session.execute(
        update(ChecklistInstance)
        .where(
            ChecklistInstance.shift_id == shift_id,
            ChecklistInstance.status == ChecklistInstanceStatus.pending,
            ChecklistInstance.is_required.is_(True),
        )
        .values(status=ChecklistInstanceStatus.incomplete)
    )

    incomplete_count_result = await session.execute(
        select(func.count()).where(
            ChecklistInstance.shift_id == shift_id,
            ChecklistInstance.status == ChecklistInstanceStatus.incomplete,
            ChecklistInstance.is_required.is_(True),
        )
    )
    has_incomplete = incomplete_count_result.scalar_one() > 0
    await session.flush()
    return has_incomplete
