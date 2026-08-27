import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, selectinload

from src.app.core.logging import get_logger
from src.app.models.checklist import (
    ChecklistTemplate,
    ChecklistTemplateItem,
    ChecklistType,
    PhotoRequirement,
    PhotoSource,
)
from src.app.models.organization import Organization
from src.app.services import entitlements
from src.app.services.common import ensure_admin_or_owner
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
    """Владелец, admin или super_admin. Делегирует в services.common."""
    await ensure_admin_or_owner(
        session,
        org,
        user_id,
        message="Нет прав для управления чек-листами",
    )


async def _ensure_ids_belong_to_org(
    session: AsyncSession,
    *,
    id_column: InstrumentedAttribute[uuid.UUID],
    org_column: InstrumentedAttribute[uuid.UUID],
    org_id: uuid.UUID,
    ids: list[uuid.UUID],
    error_code: str,
    error_message: str,
) -> None:
    """Проверяет, что все `ids` существуют в таблице `id_column` и принадлежат
    `org_id` (по `org_column`); иначе `ChecklistError(error_code, ..., 400)`.
    Пустой список — не ошибка (нечего проверять), совместимо с PUT-семантикой
    «пустой список снимает все привязки».

    Общий шаг PUT-эндпоинтов замены (роли/точки шаблона, шаблоны точки,
    личные overrides) — раньше был продублирован по каждому сервису отдельно.
    """
    if not ids:
        return
    result = await session.execute(
        select(id_column).where(id_column.in_(ids), org_column == org_id)
    )
    valid_ids = {row[0] for row in result.all()}
    if valid_ids != set(ids):
        raise ChecklistError(error_code, error_message, 400)


async def _replace_m2m_links[T](
    session: AsyncSession,
    existing_rows: Sequence[T],
    *,
    key_of: Callable[[T], uuid.UUID],
    target_ids: set[uuid.UUID],
    make_new: Callable[[uuid.UUID], T],
) -> None:
    """PUT-семантика (замена) для строк-связей many-to-many: удаляет строки,
    отсутствующие в `target_ids`, добавляет недостающие, не трогает
    совпадающие. `existing_rows` — уже отфильтрованные вызывающим кодом по
    неизменной стороне связи (например, все строки данного `template_id`).

    Общий диф-шаг `assign_template_to_roles` / `set_template_locations` /
    `set_location_templates` — раньше был продублирован по сервисам.
    """
    existing = {key_of(row): row for row in existing_rows}
    current = set(existing.keys())

    for id_ in current - target_ids:
        await session.delete(existing[id_])
    for id_ in target_ids - current:
        session.add(make_new(id_))

    await session.flush()


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
    include_deleted: bool = True,
) -> ChecklistTemplate:
    query = select(ChecklistTemplate).where(
        ChecklistTemplate.id == template_id,
        ChecklistTemplate.organization_id == org_id,
    )
    if not include_deleted:
        query = query.where(ChecklistTemplate.is_deleted.is_(False))
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
    await entitlements.require_active_subscription(session, org, requester_id)

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
    include_deleted: bool = False,
) -> list[tuple[ChecklistTemplate, int]]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    query = select(ChecklistTemplate).where(
        ChecklistTemplate.organization_id == org_id,
    )
    if not include_deleted:
        query = query.where(ChecklistTemplate.is_deleted.is_(False))
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
    counts = dict(counts_result.tuples().all())
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
    await entitlements.require_active_subscription(session, org, requester_id)
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


async def _count_items(session: AsyncSession, template_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count(ChecklistTemplateItem.id)).where(
            ChecklistTemplateItem.template_id == template_id,
        )
    )
    return result.scalar_one()


async def delete_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Мягкое удаление. Повторный вызов на уже удалённом шаблоне — 404
    (`_get_template` с `include_deleted=False` его не находит)."""
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    template = await _get_template(session, org_id, template_id, include_deleted=False)
    template.is_deleted = True
    template.deleted_at = datetime.now(UTC)
    template.deleted_by_user_id = requester_id
    await session.flush()
    logger.info(
        "checklist_template_deleted",
        org_id=str(org_id),
        template_id=str(template_id),
        deleted_by=str(requester_id),
    )


async def restore_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> tuple[ChecklistTemplate, int]:
    """Восстановление удалённого шаблона. На не-удалённом — 409 TEMPLATE_NOT_DELETED."""
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    template = await _get_template(session, org_id, template_id, include_deleted=True)
    if not template.is_deleted:
        raise ChecklistError("TEMPLATE_NOT_DELETED", "Шаблон не удалён", 409)

    template.is_deleted = False
    template.deleted_at = None
    template.deleted_by_user_id = None
    await session.flush()
    logger.info(
        "checklist_template_restored",
        org_id=str(org_id),
        template_id=str(template_id),
    )
    return template, await _count_items(session, template_id)


async def add_item(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    text: str,
    is_required: bool,
    requester_id: uuid.UUID,
    *,
    photo_requirement: PhotoRequirement = PhotoRequirement.none,
    photo_source: PhotoSource = PhotoSource.camera,
) -> ChecklistTemplateItem:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    await _get_template(session, org_id, template_id)

    max_pos = await session.execute(
        select(func.coalesce(func.max(ChecklistTemplateItem.position), -1)).where(
            ChecklistTemplateItem.template_id == template_id,
        )
    )
    next_position = max_pos.scalar_one() + 1

    # photo_source имеет смысл только при photo_requirement != none.
    if photo_requirement == PhotoRequirement.none:
        photo_source = PhotoSource.camera

    item = ChecklistTemplateItem(
        template_id=template_id,
        text=text,
        is_required=is_required,
        position=next_position,
        photo_requirement=photo_requirement,
        photo_source=photo_source,
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
    photo_requirement: PhotoRequirement | None = None,
    photo_source: PhotoSource | None = None,
) -> ChecklistTemplateItem:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    await _get_template(session, org_id, template_id)
    item = await _get_item(session, template_id, item_id)

    if text is not None:
        item.text = text
    if is_required is not None:
        item.is_required = is_required
    if photo_requirement is not None:
        item.photo_requirement = photo_requirement
    if photo_source is not None:
        item.photo_source = photo_source
    # photo_source имеет смысл только при photo_requirement != none — нормализуем.
    if item.photo_requirement == PhotoRequirement.none:
        item.photo_source = PhotoSource.camera

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
    await entitlements.require_active_subscription(session, org, requester_id)
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
    await entitlements.require_active_subscription(session, org, requester_id)
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
    return sorted(items.values(), key=lambda it: it.position)
