import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.checklist import (
    ChecklistTemplate,
    ChecklistTemplateItem,
    ChecklistType,
)
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.services.organization import get_organization

logger = get_logger(__name__)


class ChecklistError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


async def _check_admin_or_owner(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
) -> None:
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
        raise ChecklistError("FORBIDDEN", "Нет прав для управления чек-листами", 403)


def _parse_type(raw: str) -> ChecklistType:
    try:
        return ChecklistType(raw)
    except ValueError:
        raise ChecklistError(
            "INVALID_TYPE",
            f"Тип должен быть: {', '.join(t.value for t in ChecklistType)}",
            400,
        ) from None


async def _get_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    *,
    with_items: bool = False,
    include_archived: bool = True,
) -> ChecklistTemplate:
    query = select(ChecklistTemplate).where(
        ChecklistTemplate.id == template_id,
        ChecklistTemplate.organization_id == org_id,
    )
    if not include_archived:
        query = query.where(ChecklistTemplate.is_archived.is_(False))
    if with_items:
        query = query.options(selectinload(ChecklistTemplate.items))
    result = await session.execute(query)
    template = result.scalar_one_or_none()
    if template is None:
        raise ChecklistError("TEMPLATE_NOT_FOUND", "Шаблон не найден", 404)
    return template


async def create_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    type_: str,
    is_required: bool,
    requester_id: uuid.UUID,
) -> ChecklistTemplate:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    template = ChecklistTemplate(
        organization_id=org_id,
        name=name,
        type=_parse_type(type_),
        is_required=is_required,
    )
    session.add(template)
    await session.flush()
    logger.info(
        "checklist_template_created",
        org_id=str(org_id),
        template_id=str(template.id),
    )
    return template


async def get_templates(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    include_archived: bool = False,
) -> list[tuple[ChecklistTemplate, int]]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    query = select(ChecklistTemplate).where(
        ChecklistTemplate.organization_id == org_id,
    )
    if not include_archived:
        query = query.where(ChecklistTemplate.is_archived.is_(False))
    query = query.order_by(ChecklistTemplate.created_at)

    result = await session.execute(query)
    templates = list(result.scalars().all())

    if not templates:
        return []

    counts_result = await session.execute(
        select(
            ChecklistTemplateItem.template_id,
            func.count(ChecklistTemplateItem.id),
        )
        .where(ChecklistTemplateItem.template_id.in_([t.id for t in templates]))
        .group_by(ChecklistTemplateItem.template_id)
    )
    counts = {tpl_id: n for tpl_id, n in counts_result.all()}
    return [(t, counts.get(t.id, 0)) for t in templates]


async def get_template_detail(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> ChecklistTemplate:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    return await _get_template(session, org_id, template_id, with_items=True)


async def update_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    name: str | None = None,
    type_: str | None = None,
    is_required: bool | None = None,
) -> tuple[ChecklistTemplate, int]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    template = await _get_template(session, org_id, template_id)

    if name is not None:
        template.name = name
    if type_ is not None:
        template.type = _parse_type(type_)
    if is_required is not None:
        template.is_required = is_required

    await session.flush()

    count_result = await session.execute(
        select(func.count(ChecklistTemplateItem.id)).where(
            ChecklistTemplateItem.template_id == template_id,
        )
    )
    items_count = count_result.scalar_one()
    logger.info(
        "checklist_template_updated",
        org_id=str(org_id),
        template_id=str(template_id),
    )
    return template, items_count


async def delete_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    template = await _get_template(session, org_id, template_id)
    template.is_archived = True
    await session.flush()
    logger.info(
        "checklist_template_archived",
        org_id=str(org_id),
        template_id=str(template_id),
    )


async def add_item(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    text: str,
    is_required: bool,
    requester_id: uuid.UUID,
) -> ChecklistTemplateItem:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_template(session, org_id, template_id)

    max_pos = await session.execute(
        select(func.coalesce(func.max(ChecklistTemplateItem.position), -1)).where(
            ChecklistTemplateItem.template_id == template_id,
        )
    )
    next_position = max_pos.scalar_one() + 1

    item = ChecklistTemplateItem(
        template_id=template_id,
        text=text,
        is_required=is_required,
        position=next_position,
    )
    session.add(item)
    await session.flush()
    logger.info(
        "checklist_item_added",
        template_id=str(template_id),
        item_id=str(item.id),
    )
    return item


async def _get_item(
    session: AsyncSession,
    template_id: uuid.UUID,
    item_id: uuid.UUID,
) -> ChecklistTemplateItem:
    result = await session.execute(
        select(ChecklistTemplateItem).where(
            ChecklistTemplateItem.id == item_id,
            ChecklistTemplateItem.template_id == template_id,
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise ChecklistError("ITEM_NOT_FOUND", "Пункт не найден", 404)
    return item


async def update_item(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    item_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    text: str | None = None,
    is_required: bool | None = None,
) -> ChecklistTemplateItem:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_template(session, org_id, template_id)
    item = await _get_item(session, template_id, item_id)

    if text is not None:
        item.text = text
    if is_required is not None:
        item.is_required = is_required

    await session.flush()
    return item


async def delete_item(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    item_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_template(session, org_id, template_id)
    item = await _get_item(session, template_id, item_id)
    await session.delete(item)
    await session.flush()


async def reorder_items(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    item_ids: list[uuid.UUID],
    requester_id: uuid.UUID,
) -> list[ChecklistTemplateItem]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_template(session, org_id, template_id)

    result = await session.execute(
        select(ChecklistTemplateItem).where(
            ChecklistTemplateItem.template_id == template_id,
        )
    )
    items = {it.id: it for it in result.scalars().all()}

    if set(items.keys()) != set(item_ids) or len(item_ids) != len(items):
        raise ChecklistError(
            "ITEMS_MISMATCH",
            "Передайте ровно все пункты шаблона без дубликатов",
            400,
        )

    for position, item_id in enumerate(item_ids):
        items[item_id].position = position

    await session.flush()
    ordered = sorted(items.values(), key=lambda it: it.position)
    return ordered
