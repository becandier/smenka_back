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
from src.app.services.checklist_template import (
    ChecklistError,
    _check_admin_or_owner,
    _get_template,
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
    await _get_template(session, org_id, template_id)

    if role_ids:
        roles_result = await session.execute(
            select(OrganizationRole.id).where(
                OrganizationRole.id.in_(role_ids),
                OrganizationRole.organization_id == org_id,
            )
        )
        valid_ids = {row[0] for row in roles_result.all()}
        if valid_ids != set(role_ids):
            raise ChecklistError(
                "INVALID_ROLE",
                "Одна или несколько ролей не принадлежат организации",
                400,
            )

    existing_result = await session.execute(
        select(ChecklistRoleAssignment).where(
            ChecklistRoleAssignment.template_id == template_id,
        )
    )
    existing = {a.role_id: a for a in existing_result.scalars().all()}

    target = set(role_ids)
    current = set(existing.keys())

    for role_id in current - target:
        await session.delete(existing[role_id])

    for role_id in target - current:
        session.add(
            ChecklistRoleAssignment(template_id=template_id, role_id=role_id)
        )

    await session.flush()
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
) -> tuple[list[uuid.UUID], list[OrganizationMember], list[OrganizationMember]]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_template(session, org_id, template_id)

    roles_result = await session.execute(
        select(ChecklistRoleAssignment.role_id).where(
            ChecklistRoleAssignment.template_id == template_id,
        )
    )
    role_ids = [row[0] for row in roles_result.all()]

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

    return role_ids, personal_add, personal_remove


async def set_member_overrides(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    overrides: list[tuple[uuid.UUID, str]],
    requester_id: uuid.UUID,
) -> list[tuple[uuid.UUID, OverrideType]]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
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

    if parsed:
        tpl_ids = [t[0] for t in parsed]
        tpls_result = await session.execute(
            select(ChecklistTemplate.id).where(
                ChecklistTemplate.id.in_(tpl_ids),
                ChecklistTemplate.organization_id == org_id,
            )
        )
        valid_tpl_ids = {row[0] for row in tpls_result.all()}
        if valid_tpl_ids != set(tpl_ids):
            raise ChecklistError(
                "INVALID_TEMPLATE",
                "Один или несколько шаблонов не принадлежат организации",
                400,
            )

    existing_result = await session.execute(
        select(ChecklistMemberOverride).where(
            ChecklistMemberOverride.member_id == member.id,
        )
    )
    existing = {o.template_id: o for o in existing_result.scalars().all()}
    target = {tpl_id: t for tpl_id, t in parsed}

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
) -> list[tuple[ChecklistTemplate, str]]:
    org = await get_organization(session, org_id)

    if requester_id != user_id:
        await _check_admin_or_owner(session, org, requester_id)
    else:
        await _check_org_access(session, org, requester_id)

    member = await _get_member(session, org_id, user_id)

    return await _compute_effective(session, org_id, member)


async def _compute_effective(
    session: AsyncSession,
    org_id: uuid.UUID,
    member: OrganizationMember,
) -> list[tuple[ChecklistTemplate, str]]:
    role_template_ids: set[uuid.UUID] = set()
    if member.role_id is not None:
        role_result = await session.execute(
            select(ChecklistRoleAssignment.template_id).where(
                ChecklistRoleAssignment.role_id == member.role_id,
            )
        )
        role_template_ids = {row[0] for row in role_result.all()}

    overrides_result = await session.execute(
        select(ChecklistMemberOverride).where(
            ChecklistMemberOverride.member_id == member.id,
        )
    )
    overrides = list(overrides_result.scalars().all())
    add_ids = {o.template_id for o in overrides if o.override_type == OverrideType.add}
    remove_ids = {
        o.template_id for o in overrides if o.override_type == OverrideType.remove
    }

    effective_role_ids = role_template_ids - remove_ids

    all_ids = effective_role_ids | add_ids
    if not all_ids:
        return []

    templates_result = await session.execute(
        select(ChecklistTemplate).where(
            ChecklistTemplate.id.in_(all_ids),
            ChecklistTemplate.organization_id == org_id,
            ChecklistTemplate.is_archived.is_(False),
        )
    )
    templates = {t.id: t for t in templates_result.scalars().all()}

    result: list[tuple[ChecklistTemplate, str]] = []
    for tpl_id in effective_role_ids:
        if tpl_id in templates:
            result.append((templates[tpl_id], "role"))
    for tpl_id in add_ids:
        if tpl_id in templates and tpl_id not in effective_role_ids:
            result.append((templates[tpl_id], "personal_add"))
    result.sort(key=lambda pair: pair[0].created_at)
    return result
