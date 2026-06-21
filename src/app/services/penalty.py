"""Штрафы организации: шаблоны, назначение/снятие/правка, учёт в payroll.

Деньги — только целые копейки (`amount_minor`, инвариант `> 0`). Снятие штрафа и
удаление шаблона — soft-delete (`is_deleted = true`): для всех читающих запросов
снятый штраф/удалённый шаблон невидим. `reason`/`amount`/`currency`/`occurred_at`
штрафа — снимок на момент создания, не зависящий от дальнейшей судьбы шаблона.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.organization import OrganizationMember
from src.app.models.penalty import OrganizationPenaltyTemplate, Penalty
from src.app.models.shift import Shift
from src.app.services import organization as org_service
from src.app.services.common import ensure_admin_or_owner
from src.app.services.shift import ensure_utc, validate_date_range

logger = get_logger(__name__)

PENALTY_CURRENCY = "RUB"


class PenaltyError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


# --- Внутренние помощники ----------------------------------------------------
async def _get_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
) -> OrganizationMember:
    """Участник по organization_members.id строго в пределах организации.

    Owner != member (ADR-001): для owner записи нет → MEMBER_NOT_FOUND.
    """
    result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == org_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise PenaltyError("MEMBER_NOT_FOUND", "Участник не найден", 404)
    return member


async def _get_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
) -> OrganizationPenaltyTemplate:
    """Активный шаблон организации; снятый/чужой → PENALTY_TEMPLATE_NOT_FOUND."""
    result = await session.execute(
        select(OrganizationPenaltyTemplate).where(
            OrganizationPenaltyTemplate.id == template_id,
            OrganizationPenaltyTemplate.organization_id == org_id,
            OrganizationPenaltyTemplate.is_deleted.is_(False),
        )
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise PenaltyError("PENALTY_TEMPLATE_NOT_FOUND", "Шаблон штрафа не найден", 404)
    return template


async def _get_penalty(
    session: AsyncSession,
    org_id: uuid.UUID,
    penalty_id: uuid.UUID,
) -> Penalty:
    """Активный штраф организации; снятый/чужой → PENALTY_NOT_FOUND."""
    result = await session.execute(
        select(Penalty).where(
            Penalty.id == penalty_id,
            Penalty.organization_id == org_id,
            Penalty.is_deleted.is_(False),
        )
    )
    penalty = result.scalar_one_or_none()
    if penalty is None:
        raise PenaltyError("PENALTY_NOT_FOUND", "Штраф не найден", 404)
    return penalty


async def _validate_shift_for_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    member: OrganizationMember,
    shift_id: uuid.UUID,
) -> Shift:
    """Смена существует, принадлежит сотруднику и организации, не удалена."""
    result = await session.execute(
        select(Shift).where(
            Shift.id == shift_id,
            Shift.organization_id == org_id,
            Shift.user_id == member.user_id,
            Shift.is_deleted.is_(False),
        )
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise PenaltyError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


# --- Шаблоны штрафов ---------------------------------------------------------
async def create_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    reason: str,
    amount_minor: int,
    currency: str,
) -> OrganizationPenaltyTemplate:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)

    template = OrganizationPenaltyTemplate(
        organization_id=org_id,
        reason=reason,
        amount_minor=amount_minor,
        currency=currency,
    )
    session.add(template)
    await session.flush()
    logger.info("penalty_template_created", org_id=str(org_id), template_id=str(template.id))
    return template


async def list_templates(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[OrganizationPenaltyTemplate]:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)

    result = await session.execute(
        select(OrganizationPenaltyTemplate)
        .where(
            OrganizationPenaltyTemplate.organization_id == org_id,
            OrganizationPenaltyTemplate.is_deleted.is_(False),
        )
        .order_by(OrganizationPenaltyTemplate.created_at.desc())
    )
    return list(result.scalars().all())


async def update_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
    fields: dict[str, Any],
) -> OrganizationPenaltyTemplate:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    template = await _get_template(session, org_id, template_id)

    for key in ("reason", "amount_minor", "currency"):
        if fields.get(key) is not None:
            setattr(template, key, fields[key])
    await session.flush()
    logger.info("penalty_template_updated", org_id=str(org_id), template_id=str(template_id))
    return template


async def delete_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Soft-delete шаблона: уходит из списка выбора, выданные штрафы не затрагиваются."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    template = await _get_template(session, org_id, template_id)

    template.is_deleted = True
    await session.flush()
    logger.info("penalty_template_deleted", org_id=str(org_id), template_id=str(template_id))


# --- Штрафы ------------------------------------------------------------------
async def create_penalty(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    member_id: uuid.UUID,
    template_id: uuid.UUID | None,
    reason: str | None,
    amount_minor: int | None,
    currency: str | None,
    shift_id: uuid.UUID | None,
    occurred_at: datetime | None,
    comment: str | None,
) -> Penalty:
    """Назначить штраф. reason/amount/currency — снимок (из шаблона или кастом)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    member = await _get_member(session, org_id, member_id)

    template = None
    if template_id is not None:
        template = await _get_template(session, org_id, template_id)

    final_reason = reason if reason is not None else (template.reason if template else None)
    final_amount = (
        amount_minor if amount_minor is not None else (template.amount_minor if template else None)
    )
    final_currency = currency or (template.currency if template else None) or PENALTY_CURRENCY

    if final_reason is None or not final_reason.strip():
        raise PenaltyError("VALIDATION_ERROR", "Не указана причина штрафа", 422)
    if final_amount is None or final_amount <= 0:
        raise PenaltyError("VALIDATION_ERROR", "Сумма штрафа должна быть > 0", 422)

    shift = None
    if shift_id is not None:
        shift = await _validate_shift_for_member(session, org_id, member, shift_id)

    if occurred_at is not None:
        final_occurred = ensure_utc(occurred_at)
    elif shift is not None:
        final_occurred = shift.started_at
    else:
        raise PenaltyError(
            "VALIDATION_ERROR",
            "occurred_at обязателен, если штраф не привязан к смене",
            422,
        )

    penalty = Penalty(
        organization_id=org_id,
        member_id=member.id,
        shift_id=shift_id,
        template_id=template_id,
        reason=final_reason,
        amount_minor=final_amount,
        currency=final_currency,
        occurred_at=final_occurred,
        comment=comment,
        created_by_user_id=requester_id,
    )
    session.add(penalty)
    await session.flush()
    logger.info(
        "penalty_created",
        org_id=str(org_id),
        penalty_id=str(penalty.id),
        member_id=str(member.id),
        amount_minor=final_amount,
    )
    return penalty


async def list_penalties(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    member_id: uuid.UUID | None = None,
    shift_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Penalty], int]:
    """Активные штрафы организации под фильтром. Returns (penalties, total)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)

    validate_date_range(date_from, date_to)
    conditions = [Penalty.organization_id == org_id, Penalty.is_deleted.is_(False)]
    if member_id is not None:
        conditions.append(Penalty.member_id == member_id)
    if shift_id is not None:
        conditions.append(Penalty.shift_id == shift_id)
    if date_from is not None:
        conditions.append(Penalty.occurred_at >= ensure_utc(date_from))
    if date_to is not None:
        conditions.append(Penalty.occurred_at <= ensure_utc(date_to))

    count_query = select(func.count()).select_from(Penalty).where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    query = (
        select(Penalty)
        .where(*conditions)
        .order_by(Penalty.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all()), total


async def get_penalty(
    session: AsyncSession,
    org_id: uuid.UUID,
    penalty_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> Penalty:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    return await _get_penalty(session, org_id, penalty_id)


async def update_penalty(
    session: AsyncSession,
    org_id: uuid.UUID,
    penalty_id: uuid.UUID,
    requester_id: uuid.UUID,
    fields: dict[str, Any],
) -> Penalty:
    """Исправить запись штрафа. member_id не меняется (для переназначения — снять и создать)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    penalty = await _get_penalty(session, org_id, penalty_id)

    if "shift_id" in fields:
        new_shift_id = fields["shift_id"]
        if new_shift_id is not None:
            member = await _get_member(session, org_id, penalty.member_id)
            await _validate_shift_for_member(session, org_id, member, new_shift_id)
        penalty.shift_id = new_shift_id

    if fields.get("occurred_at") is not None:
        penalty.occurred_at = ensure_utc(fields["occurred_at"])
    if fields.get("reason") is not None:
        penalty.reason = fields["reason"]
    if fields.get("amount_minor") is not None:
        penalty.amount_minor = fields["amount_minor"]
    if fields.get("currency") is not None:
        penalty.currency = fields["currency"]
    if "comment" in fields:
        penalty.comment = fields["comment"]

    await session.flush()
    logger.info("penalty_updated", org_id=str(org_id), penalty_id=str(penalty_id))
    return penalty


async def delete_penalty(
    session: AsyncSession,
    org_id: uuid.UUID,
    penalty_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Снять штраф (soft-delete): любой admin/owner, фиксируем кто и когда снял."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    penalty = await _get_penalty(session, org_id, penalty_id)

    penalty.is_deleted = True
    penalty.deleted_by_user_id = requester_id
    penalty.deleted_at = datetime.now(UTC)
    await session.flush()
    logger.info(
        "penalty_deleted",
        org_id=str(org_id),
        penalty_id=str(penalty_id),
        deleted_by=str(requester_id),
    )


async def list_my_penalties(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Penalty], int]:
    """Свои активные штрафы участника. Owner != member ⇒ 403 FORBIDDEN."""
    await org_service.get_organization(session, org_id)

    member_result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = member_result.scalar_one_or_none()
    if member is None:
        raise PenaltyError("FORBIDDEN", "Вы не являетесь участником организации", 403)

    validate_date_range(date_from, date_to)
    conditions = [Penalty.member_id == member.id, Penalty.is_deleted.is_(False)]
    if date_from is not None:
        conditions.append(Penalty.occurred_at >= ensure_utc(date_from))
    if date_to is not None:
        conditions.append(Penalty.occurred_at <= ensure_utc(date_to))

    count_query = select(func.count()).select_from(Penalty).where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    query = (
        select(Penalty)
        .where(*conditions)
        .order_by(Penalty.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all()), total


# --- Агрегаты для payroll ----------------------------------------------------
async def aggregate_penalties_by_user(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> dict[uuid.UUID, tuple[int, int]]:
    """user_id → (сумма active-штрафов в копейках, число штрафов) за период.

    Атрибуция к сотруднику — через member_id → organization_members.user_id.
    Период — по `occurred_at`, `date_to` включительно (UTC). Только is_deleted=false.
    """
    conditions = [Penalty.organization_id == org_id, Penalty.is_deleted.is_(False)]
    if date_from is not None:
        conditions.append(Penalty.occurred_at >= date_from)
    if date_to is not None:
        conditions.append(Penalty.occurred_at <= date_to)

    result = await session.execute(
        select(
            OrganizationMember.user_id,
            func.coalesce(func.sum(Penalty.amount_minor), 0),
            func.count(Penalty.id),
        )
        .join(OrganizationMember, Penalty.member_id == OrganizationMember.id)
        .where(*conditions)
        .group_by(OrganizationMember.user_id)
    )
    return {user_id: (int(total), int(count)) for user_id, total, count in result.all()}


async def aggregate_member_penalties(
    session: AsyncSession,
    member_id: uuid.UUID,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[int, int]:
    """(сумма active-штрафов в копейках, число) для одного участника за период."""
    conditions = [Penalty.member_id == member_id, Penalty.is_deleted.is_(False)]
    if date_from is not None:
        conditions.append(Penalty.occurred_at >= date_from)
    if date_to is not None:
        conditions.append(Penalty.occurred_at <= date_to)

    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(Penalty.amount_minor), 0),
                func.count(Penalty.id),
            ).where(*conditions)
        )
    ).one()
    return int(row[0]), int(row[1])
