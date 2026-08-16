"""Привязка шаблонов чек-листов к рабочим точкам (checklist_work_location).

Владеет таблицей связей `checklist_template_locations` (many-to-many между
`ChecklistTemplate` и `WorkLocation`). Два симметричных набора операций пишут
в одну и ту же таблицу с разных сторон:

- `set_template_locations` / `get_location_ids_for_templates` — со стороны
  шаблона (какие точки у него привязаны).
- `set_location_templates` / `get_location_templates` — со стороны точки
  (какие шаблоны привязаны к ней).

Плюс переиспользуемые хелперы для фильтра «эффективного набора» по точке
(`get_location_ids_for_templates`, `matches_location`, `get_location_only_template_ids`),
которые нужны и вычислению эффективных чек-листов сотрудника
(`services/checklist_assignment.py`), и созданию экземпляров на старте смены
(`services/checklist_instance.py`).
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.checklist import (
    ChecklistRoleAssignment,
    ChecklistTemplate,
    ChecklistTemplateLocation,
)
from src.app.models.work_location import WorkLocation
from src.app.services.checklist_template import (
    ChecklistError,
    _check_admin_or_owner,
    _ensure_ids_belong_to_org,
    _get_template,
    _replace_m2m_links,
)
from src.app.services.organization import get_organization

logger = get_logger(__name__)


async def _get_org_location(
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
        raise ChecklistError("WORK_LOCATION_NOT_FOUND", "Точка не найдена", 404)
    return location


async def set_template_locations(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    location_ids: list[uuid.UUID],
    requester_id: uuid.UUID,
) -> list[uuid.UUID]:
    """PUT-семантика: полный список точек шаблона (замена)."""
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_template(session, org_id, template_id)

    await _ensure_ids_belong_to_org(
        session,
        id_column=WorkLocation.id,
        org_column=WorkLocation.organization_id,
        org_id=org_id,
        ids=location_ids,
        error_code="INVALID_LOCATION",
        error_message="Одна или несколько точек не принадлежат организации",
    )

    existing_result = await session.execute(
        select(ChecklistTemplateLocation).where(
            ChecklistTemplateLocation.template_id == template_id,
        )
    )
    target = set(location_ids)
    await _replace_m2m_links(
        session,
        existing_result.scalars().all(),
        key_of=lambda loc: loc.work_location_id,
        target_ids=target,
        make_new=lambda loc_id: ChecklistTemplateLocation(
            template_id=template_id, work_location_id=loc_id
        ),
    )
    logger.info(
        "checklist_template_locations_assigned",
        template_id=str(template_id),
        location_count=len(target),
    )
    return list(target)


async def get_location_ids_for_templates(
    session: AsyncSession,
    template_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Batch-версия `get_template_location_ids` — без N+1 по набору шаблонов."""
    ids = list(template_ids)
    if not ids:
        return {}
    result = await session.execute(
        select(
            ChecklistTemplateLocation.template_id,
            ChecklistTemplateLocation.work_location_id,
        ).where(ChecklistTemplateLocation.template_id.in_(ids))
    )
    mapping: dict[uuid.UUID, list[uuid.UUID]] = {}
    for tpl_id, loc_id in result.all():
        mapping.setdefault(tpl_id, []).append(loc_id)
    return mapping


def matches_location(
    location_ids: list[uuid.UUID] | None,
    shift_work_location_id: uuid.UUID | None,
) -> bool:
    """matches_location(T, shift) из backend.md.

    Шаблон без привязок (`location_ids` пуст) действует везде. Шаблон с
    привязками — только если точка смены входит в его список; смена без
    точки (`shift_work_location_id is None`) никогда не проходит для
    привязанного шаблона.
    """
    if not location_ids:
        return True
    if shift_work_location_id is None:
        return False
    return shift_work_location_id in location_ids


async def get_location_only_template_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Новый канал назначения (backend.md, матрица «нет ролей + есть точки»).

    Шаблон без единой привязки к роли, но с привязкой хотя бы к одной точке,
    действует на ВСЕХ сотрудников организации (независимо от их роли или её
    отсутствия), которые открыли смену на одной из этих точек. Архивные
    исключаются сразу — как и обычные ролевые шаблоны."""
    has_location = (
        select(ChecklistTemplateLocation.id)
        .where(ChecklistTemplateLocation.template_id == ChecklistTemplate.id)
        .correlate(ChecklistTemplate)
        .exists()
    )
    has_role = (
        select(ChecklistRoleAssignment.id)
        .where(ChecklistRoleAssignment.template_id == ChecklistTemplate.id)
        .correlate(ChecklistTemplate)
        .exists()
    )
    result = await session.execute(
        select(ChecklistTemplate.id).where(
            ChecklistTemplate.organization_id == org_id,
            ChecklistTemplate.is_deleted.is_(False),
            has_location,
            ~has_role,
        )
    )
    return {row[0] for row in result.all()}


async def set_location_templates(
    session: AsyncSession,
    org_id: uuid.UUID,
    location_id: uuid.UUID,
    template_ids: list[uuid.UUID],
    requester_id: uuid.UUID,
) -> list[uuid.UUID]:
    """PUT-семантика: полный список шаблонов, привязанных к точке (замена)."""
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_org_location(session, org_id, location_id)

    await _ensure_ids_belong_to_org(
        session,
        id_column=ChecklistTemplate.id,
        org_column=ChecklistTemplate.organization_id,
        org_id=org_id,
        ids=template_ids,
        error_code="INVALID_TEMPLATE",
        error_message="Один или несколько шаблонов не принадлежат организации",
    )

    existing_result = await session.execute(
        select(ChecklistTemplateLocation).where(
            ChecklistTemplateLocation.work_location_id == location_id,
        )
    )
    target = set(template_ids)
    await _replace_m2m_links(
        session,
        existing_result.scalars().all(),
        key_of=lambda loc: loc.template_id,
        target_ids=target,
        make_new=lambda tpl_id: ChecklistTemplateLocation(
            template_id=tpl_id, work_location_id=location_id
        ),
    )
    logger.info(
        "location_checklist_templates_assigned",
        location_id=str(location_id),
        template_count=len(target),
    )
    return list(target)


async def get_location_templates(
    session: AsyncSession,
    org_id: uuid.UUID,
    location_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[ChecklistTemplate]:
    """Обратный срез: шаблоны, привязанные к точке (включая архивные —
    админ должен видеть, что привязка существует; в расчёт эффективного
    набора архивные по-прежнему не идут)."""
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_org_location(session, org_id, location_id)

    result = await session.execute(
        select(ChecklistTemplate)
        .join(
            ChecklistTemplateLocation,
            ChecklistTemplateLocation.template_id == ChecklistTemplate.id,
        )
        .where(ChecklistTemplateLocation.work_location_id == location_id)
        .order_by(ChecklistTemplate.created_at)
    )
    return list(result.scalars().all())
