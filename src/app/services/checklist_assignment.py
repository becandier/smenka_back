import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.checklist import (
    ChecklistMemberOverride,
    ChecklistRoleAssignment,
    ChecklistTemplate,
    OverrideType,
)
from src.app.models.organization import OrganizationMember
from src.app.models.organization_role import OrganizationRole
from src.app.services import entitlements
from src.app.services.checklist_location import (
    _get_org_location,
    get_location_ids_for_templates,
    matches_location,
)
from src.app.services.checklist_schedule import get_schedule_ids_for_templates
from src.app.services.checklist_template import (
    ChecklistError,
    _check_admin_or_owner,
    _ensure_ids_belong_to_org,
    _get_template,
    _replace_m2m_links,
)
from src.app.services.organization import _check_org_access, get_organization

logger = get_logger(__name__)


async def _get_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> OrganizationMember:
    result = await session.execute(
        select(OrganizationMember)
        .options(selectinload(OrganizationMember.user))
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise ChecklistError("MEMBER_NOT_FOUND", "Участник не найден", 404)
    return member


async def assign_template_to_roles(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    role_ids: list[uuid.UUID],
    requester_id: uuid.UUID,
) -> list[uuid.UUID]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    await _get_template(session, org_id, template_id)

    await _ensure_ids_belong_to_org(
        session,
        id_column=OrganizationRole.id,
        org_column=OrganizationRole.organization_id,
        org_id=org_id,
        ids=role_ids,
        error_code="INVALID_ROLE",
        error_message="Одна или несколько ролей не принадлежат организации",
    )

    existing_result = await session.execute(
        select(ChecklistRoleAssignment).where(
            ChecklistRoleAssignment.template_id == template_id,
        )
    )
    target = set(role_ids)
    await _replace_m2m_links(
        session,
        existing_result.scalars().all(),
        key_of=lambda a: a.role_id,
        target_ids=target,
        make_new=lambda role_id: ChecklistRoleAssignment(template_id=template_id, role_id=role_id),
    )
    logger.info(
        "checklist_template_roles_assigned",
        template_id=str(template_id),
        role_count=len(target),
    )
    return list(target)


async def get_template_assignments(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> tuple[
    list[uuid.UUID],
    list[OrganizationMember],
    list[OrganizationMember],
    list[uuid.UUID],
    list[uuid.UUID],
]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_template(session, org_id, template_id)

    roles_result = await session.execute(
        select(ChecklistRoleAssignment.role_id).where(
            ChecklistRoleAssignment.template_id == template_id,
        )
    )
    role_ids = [row[0] for row in roles_result.all()]
    location_ids = (await get_location_ids_for_templates(session, [template_id])).get(
        template_id, []
    )
    schedule_ids = (await get_schedule_ids_for_templates(session, [template_id])).get(
        template_id, []
    )

    overrides_result = await session.execute(
        select(ChecklistMemberOverride).where(
            ChecklistMemberOverride.template_id == template_id,
        )
    )
    overrides = list(overrides_result.scalars().all())

    if overrides:
        member_ids = [o.member_id for o in overrides]
        members_result = await session.execute(
            select(OrganizationMember)
            .options(selectinload(OrganizationMember.user))
            .where(OrganizationMember.id.in_(member_ids))
        )
        by_id = {m.id: m for m in members_result.scalars().all()}
    else:
        by_id = {}

    personal_add: list[OrganizationMember] = []
    personal_remove: list[OrganizationMember] = []
    for o in overrides:
        member = by_id.get(o.member_id)
        if member is None:
            continue
        if o.override_type == OverrideType.add:
            personal_add.append(member)
        else:
            personal_remove.append(member)

    return role_ids, personal_add, personal_remove, location_ids, schedule_ids


async def set_member_overrides(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    overrides: list[tuple[uuid.UUID, str]],
    requester_id: uuid.UUID,
) -> list[tuple[uuid.UUID, OverrideType]]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    member = await _get_member(session, org_id, user_id)

    parsed: list[tuple[uuid.UUID, OverrideType]] = []
    seen_templates: set[uuid.UUID] = set()
    for tpl_id, raw_type in overrides:
        if tpl_id in seen_templates:
            raise ChecklistError(
                "DUPLICATE_TEMPLATE",
                "Каждый шаблон может встречаться только один раз",
                400,
            )
        seen_templates.add(tpl_id)
        try:
            t = OverrideType(raw_type)
        except ValueError:
            raise ChecklistError(
                "INVALID_OVERRIDE_TYPE",
                "type должен быть add или remove",
                400,
            ) from None
        parsed.append((tpl_id, t))

    await _ensure_ids_belong_to_org(
        session,
        id_column=ChecklistTemplate.id,
        org_column=ChecklistTemplate.organization_id,
        org_id=org_id,
        ids=[t[0] for t in parsed],
        error_code="INVALID_TEMPLATE",
        error_message="Один или несколько шаблонов не принадлежат организации",
    )

    existing_result = await session.execute(
        select(ChecklistMemberOverride).where(
            ChecklistMemberOverride.member_id == member.id,
        )
    )
    existing = {o.template_id: o for o in existing_result.scalars().all()}
    target = dict(parsed)

    for tpl_id in set(existing.keys()) - set(target.keys()):
        await session.delete(existing[tpl_id])

    for tpl_id, t in target.items():
        if tpl_id in existing:
            existing[tpl_id].override_type = t
        else:
            session.add(
                ChecklistMemberOverride(
                    template_id=tpl_id,
                    member_id=member.id,
                    override_type=t,
                )
            )

    await session.flush()
    logger.info(
        "member_overrides_set",
        member_id=str(member.id),
        count=len(target),
    )
    return parsed


async def get_effective_templates(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    work_location_id: uuid.UUID | None = None,
    work_schedule_id: uuid.UUID | None = None,
) -> list[tuple[ChecklistTemplate, str, list[uuid.UUID]]]:
    """Эффективные чек-листы сотрудника.

    Без `work_location_id` (обратная совместимость) — весь `by_assignment`
    набор, `location_ids` каждого шаблона отдаются информационно, без
    фильтра. С `work_location_id` — ровно то, что сотрудник получил бы,
    открыв смену на этой точке (`matches_location`, backend.md §5.2);
    точка валидируется на принадлежность org, иначе `WORK_LOCATION_NOT_FOUND`.
    """
    org = await get_organization(session, org_id)

    if requester_id != user_id:
        await _check_admin_or_owner(session, org, requester_id)
    else:
        await _check_org_access(session, org, requester_id)

    if work_location_id is not None:
        await _get_org_location(session, org_id, work_location_id)

    member = await _get_member(session, org_id, user_id)
    pairs = await _compute_effective(
        session,
        org_id,
        member,
        work_location_id=work_location_id,
        work_schedule_id=work_schedule_id,
    )

    location_ids_by_template = await get_location_ids_for_templates(
        session, [t.id for t, _ in pairs]
    )

    result: list[tuple[ChecklistTemplate, str, list[uuid.UUID]]] = []
    for template, source in pairs:
        location_ids = location_ids_by_template.get(template.id, [])
        if work_location_id is not None and not matches_location(location_ids, work_location_id):
            continue
        result.append((template, source, location_ids))
    return result


async def _compute_effective(
    session: AsyncSession,
    org_id: uuid.UUID,
    member: OrganizationMember,
    *,
    work_location_id: uuid.UUID | None = None,
    work_schedule_id: uuid.UUID | None = None,
    strict_schedule: bool = False,
) -> list[tuple[ChecklistTemplate, str]]:
    overrides_result = await session.execute(
        select(ChecklistMemberOverride).where(
            ChecklistMemberOverride.member_id == member.id,
        )
    )
    overrides = list(overrides_result.scalars().all())
    add_ids = {o.template_id for o in overrides if o.override_type == OverrideType.add}
    remove_ids = {o.template_id for o in overrides if o.override_type == OverrideType.remove}

    template_result = await session.execute(
        select(ChecklistTemplate).where(
            ChecklistTemplate.organization_id == org_id,
            ChecklistTemplate.is_deleted.is_(False),
        )
    )
    templates = list(template_result.scalars().all())
    if not templates:
        return []

    ids = [template.id for template in templates]
    role_result = await session.execute(
        select(ChecklistRoleAssignment.template_id, ChecklistRoleAssignment.role_id).where(
            ChecklistRoleAssignment.template_id.in_(ids)
        )
    )
    role_map: dict[uuid.UUID, set[uuid.UUID]] = {}
    for template_id, role_id in role_result.all():
        role_map.setdefault(template_id, set()).add(role_id)
    location_map = await get_location_ids_for_templates(session, ids)
    schedule_map = await get_schedule_ids_for_templates(session, ids)

    result: list[tuple[ChecklistTemplate, str]] = []
    for template in templates:
        role_ids = role_map.get(template.id, set())
        location_ids = location_map.get(template.id, [])
        schedule_ids = schedule_map.get(template.id, [])
        has_assignment = bool(role_ids or location_ids or schedule_ids or template.id in add_ids)
        if not has_assignment or template.id in remove_ids:
            continue
        role_matches = not role_ids or member.role_id in role_ids
        location_matches = (
            True if work_location_id is None else matches_location(location_ids, work_location_id)
        )
        schedule_matches = not schedule_ids or work_schedule_id in schedule_ids
        if work_schedule_id is None and not strict_schedule:
            schedule_matches = True
        if not role_matches or not location_matches or not schedule_matches:
            continue
        source = (
            "personal_add"
            if template.id in add_ids
            else ("role" if role_ids else "schedule" if schedule_ids else "location")
        )
        result.append((template, source))
    result.sort(key=lambda pair: pair[0].created_at)
    return result
