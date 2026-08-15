"""Ручные начисления/удержания организации (manual_time_entry).

Знаковая сумма (`amount_minor`): `> 0` — доплата, `< 0` — удержание. Симметрично
`services/penalty.py`, но без шаблонов и с обеими сторонами знака. Отмена —
soft-delete (`is_deleted = true`). Каждая операция (создание/правка/отмена)
пишет уведомление сотруднику (`payroll_adjustment_changed`) и запись в
`audit_logs` в той же транзакции — прозрачность для сотрудника (R7).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.adjustment import PayrollAdjustment
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.models.notification import NotificationType
from src.app.models.organization import OrganizationMember
from src.app.models.shift import Shift
from src.app.services import audit as audit_service
from src.app.services import notification as notification_service
from src.app.services import organization as org_service
from src.app.services.common import ensure_admin_or_owner
from src.app.services.shift import ensure_utc, validate_date_range

logger = get_logger(__name__)

ADJUSTMENT_CURRENCY = "RUB"


class AdjustmentError(Exception):
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
        raise AdjustmentError("MEMBER_NOT_FOUND", "Участник не найден", 404)
    return member


async def _get_adjustment(
    session: AsyncSession,
    org_id: uuid.UUID,
    adjustment_id: uuid.UUID,
) -> PayrollAdjustment:
    """Активное начисление организации; отменённое/чужое → ADJUSTMENT_NOT_FOUND."""
    result = await session.execute(
        select(PayrollAdjustment).where(
            PayrollAdjustment.id == adjustment_id,
            PayrollAdjustment.organization_id == org_id,
            PayrollAdjustment.is_deleted.is_(False),
        )
    )
    adjustment = result.scalar_one_or_none()
    if adjustment is None:
        raise AdjustmentError("ADJUSTMENT_NOT_FOUND", "Начисление не найдено", 404)
    return adjustment


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
        raise AdjustmentError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


async def _notify_adjustment_changed(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    action: str,
    adjustment: PayrollAdjustment,
) -> None:
    title = {
        "created": "Вам начислена корректировка зарплаты",
        "updated": "Ваша корректировка зарплаты изменена",
        "deleted": "Ваша корректировка зарплаты отменена",
    }[action]
    await notification_service.create_notification(
        session,
        user_id=user_id,
        type=NotificationType.payroll_adjustment_changed.value,
        title=title,
        body=adjustment.reason,
        payload={
            "adjustment_id": str(adjustment.id),
            "action": action,
            "amount_minor": adjustment.amount_minor,
            "occurred_at": adjustment.occurred_at.isoformat(),
        },
        organization_id=org_id,
    )


# --- CRUD ----------------------------------------------------------------------
async def create_adjustment(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    member_id: uuid.UUID,
    amount_minor: int,
    currency: str | None,
    reason: str,
    occurred_at: datetime | None,
    shift_id: uuid.UUID | None,
    comment: str | None,
) -> PayrollAdjustment:
    """Начислить/удержать. occurred_at по умолчанию = started_at смены при shift_id."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    member = await _get_member(session, org_id, member_id)

    if amount_minor == 0:
        raise AdjustmentError("VALIDATION_ERROR", "amount_minor не может быть равен 0", 422)

    shift = None
    if shift_id is not None:
        shift = await _validate_shift_for_member(session, org_id, member, shift_id)

    if occurred_at is not None:
        final_occurred = ensure_utc(occurred_at)
    elif shift is not None:
        final_occurred = shift.started_at
    else:
        raise AdjustmentError(
            "VALIDATION_ERROR",
            "occurred_at обязателен, если начисление не привязано к смене",
            422,
        )

    adjustment = PayrollAdjustment(
        organization_id=org_id,
        member_id=member.id,
        shift_id=shift_id,
        amount_minor=amount_minor,
        currency=currency or ADJUSTMENT_CURRENCY,
        reason=reason,
        comment=comment,
        occurred_at=final_occurred,
        created_by_user_id=requester_id,
    )
    session.add(adjustment)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.adjustment_create,
        resource_type=AuditResource.adjustment,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=adjustment.id,
        summary={
            "member_id": str(member_id),
            "amount_minor": amount_minor,
            "reason": reason,
            "occurred_at": final_occurred.isoformat(),
        },
    )
    await _notify_adjustment_changed(
        session,
        org_id=org_id,
        user_id=member.user_id,
        action="created",
        adjustment=adjustment,
    )

    logger.info(
        "adjustment_created",
        org_id=str(org_id),
        adjustment_id=str(adjustment.id),
        member_id=str(member.id),
        amount_minor=amount_minor,
    )
    return adjustment


async def list_adjustments(
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
) -> tuple[list[PayrollAdjustment], int]:
    """Активные начисления организации под фильтром. Returns (adjustments, total)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)

    validate_date_range(date_from, date_to)
    conditions = [
        PayrollAdjustment.organization_id == org_id,
        PayrollAdjustment.is_deleted.is_(False),
    ]
    if member_id is not None:
        conditions.append(PayrollAdjustment.member_id == member_id)
    if shift_id is not None:
        conditions.append(PayrollAdjustment.shift_id == shift_id)
    if date_from is not None:
        conditions.append(PayrollAdjustment.occurred_at >= ensure_utc(date_from))
    if date_to is not None:
        conditions.append(PayrollAdjustment.occurred_at <= ensure_utc(date_to))

    count_query = select(func.count()).select_from(PayrollAdjustment).where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    query = (
        select(PayrollAdjustment)
        .where(*conditions)
        .order_by(PayrollAdjustment.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all()), total


async def update_adjustment(
    session: AsyncSession,
    org_id: uuid.UUID,
    adjustment_id: uuid.UUID,
    requester_id: uuid.UUID,
    fields: dict[str, Any],
) -> PayrollAdjustment:
    """Исправить запись начисления. member_id не меняется."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    adjustment = await _get_adjustment(session, org_id, adjustment_id)

    changed: dict[str, Any] = {}

    if "shift_id" in fields:
        new_shift_id = fields["shift_id"]
        if new_shift_id is not None:
            member = await _get_member(session, org_id, adjustment.member_id)
            await _validate_shift_for_member(session, org_id, member, new_shift_id)
        if new_shift_id != adjustment.shift_id:
            changed["shift_id"] = {
                "from": str(adjustment.shift_id) if adjustment.shift_id else None,
                "to": str(new_shift_id) if new_shift_id else None,
            }
            adjustment.shift_id = new_shift_id

    if fields.get("amount_minor") is not None:
        if fields["amount_minor"] == 0:
            raise AdjustmentError("VALIDATION_ERROR", "amount_minor не может быть равен 0", 422)
        if fields["amount_minor"] != adjustment.amount_minor:
            changed["amount_minor"] = {
                "from": adjustment.amount_minor,
                "to": fields["amount_minor"],
            }
            adjustment.amount_minor = fields["amount_minor"]
    if fields.get("occurred_at") is not None:
        new_occurred_at = ensure_utc(fields["occurred_at"])
        if new_occurred_at != adjustment.occurred_at:
            changed["occurred_at"] = {
                "from": adjustment.occurred_at.isoformat(),
                "to": new_occurred_at.isoformat(),
            }
            adjustment.occurred_at = new_occurred_at
    if fields.get("reason") is not None and fields["reason"] != adjustment.reason:
        changed["reason"] = {"from": adjustment.reason, "to": fields["reason"]}
        adjustment.reason = fields["reason"]
    if "comment" in fields and fields["comment"] != adjustment.comment:
        changed["comment"] = {"from": adjustment.comment, "to": fields["comment"]}
        adjustment.comment = fields["comment"]

    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.adjustment_update,
        resource_type=AuditResource.adjustment,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=adjustment.id,
        summary={"changed": changed},
    )
    member_result = await session.execute(
        select(OrganizationMember.user_id).where(OrganizationMember.id == adjustment.member_id)
    )
    user_id = member_result.scalar_one()
    await _notify_adjustment_changed(
        session,
        org_id=org_id,
        user_id=user_id,
        action="updated",
        adjustment=adjustment,
    )

    logger.info("adjustment_updated", org_id=str(org_id), adjustment_id=str(adjustment_id))
    return adjustment


async def delete_adjustment(
    session: AsyncSession,
    org_id: uuid.UUID,
    adjustment_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Отменить начисление (soft-delete)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    adjustment = await _get_adjustment(session, org_id, adjustment_id)

    adjustment.is_deleted = True
    adjustment.deleted_by_user_id = requester_id
    adjustment.deleted_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.adjustment_delete,
        resource_type=AuditResource.adjustment,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=adjustment.id,
        summary={
            "member_id": str(adjustment.member_id),
            "amount_minor": adjustment.amount_minor,
            "reason": adjustment.reason,
        },
    )
    member_result = await session.execute(
        select(OrganizationMember.user_id).where(OrganizationMember.id == adjustment.member_id)
    )
    user_id = member_result.scalar_one()
    await _notify_adjustment_changed(
        session,
        org_id=org_id,
        user_id=user_id,
        action="deleted",
        adjustment=adjustment,
    )

    logger.info(
        "adjustment_deleted",
        org_id=str(org_id),
        adjustment_id=str(adjustment_id),
        deleted_by=str(requester_id),
    )


async def list_my_adjustments(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[PayrollAdjustment], int]:
    """Свои активные начисления участника. Owner != member ⇒ 403 FORBIDDEN."""
    await org_service.get_organization(session, org_id)

    member_result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = member_result.scalar_one_or_none()
    if member is None:
        raise AdjustmentError("FORBIDDEN", "Вы не являетесь участником организации", 403)

    validate_date_range(date_from, date_to)
    conditions = [
        PayrollAdjustment.member_id == member.id,
        PayrollAdjustment.is_deleted.is_(False),
    ]
    if date_from is not None:
        conditions.append(PayrollAdjustment.occurred_at >= ensure_utc(date_from))
    if date_to is not None:
        conditions.append(PayrollAdjustment.occurred_at <= ensure_utc(date_to))

    count_query = select(func.count()).select_from(PayrollAdjustment).where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    query = (
        select(PayrollAdjustment)
        .where(*conditions)
        .order_by(PayrollAdjustment.occurred_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    return list(result.scalars().all()), total


# --- Агрегаты для payroll ----------------------------------------------------
async def aggregate_adjustments_by_user(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> dict[uuid.UUID, tuple[int, int]]:
    """user_id → (знаковая сумма активных начислений в копейках, число) за период.

    Атрибуция к сотруднику — через member_id → organization_members.user_id.
    Период — по `occurred_at`, `date_to` включительно (UTC). Только is_deleted=false.
    """
    conditions = [
        PayrollAdjustment.organization_id == org_id,
        PayrollAdjustment.is_deleted.is_(False),
    ]
    if date_from is not None:
        conditions.append(PayrollAdjustment.occurred_at >= date_from)
    if date_to is not None:
        conditions.append(PayrollAdjustment.occurred_at <= date_to)

    result = await session.execute(
        select(
            OrganizationMember.user_id,
            func.coalesce(func.sum(PayrollAdjustment.amount_minor), 0),
            func.count(PayrollAdjustment.id),
        )
        .join(OrganizationMember, PayrollAdjustment.member_id == OrganizationMember.id)
        .where(*conditions)
        .group_by(OrganizationMember.user_id)
    )
    return {user_id: (int(total), int(count)) for user_id, total, count in result.all()}


async def aggregate_member_adjustments(
    session: AsyncSession,
    member_id: uuid.UUID,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
) -> tuple[int, int]:
    """(знаковая сумма активных начислений в копейках, число) для одного участника."""
    conditions = [
        PayrollAdjustment.member_id == member_id,
        PayrollAdjustment.is_deleted.is_(False),
    ]
    if date_from is not None:
        conditions.append(PayrollAdjustment.occurred_at >= date_from)
    if date_to is not None:
        conditions.append(PayrollAdjustment.occurred_at <= date_to)

    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(PayrollAdjustment.amount_minor), 0),
                func.count(PayrollAdjustment.id),
            ).where(*conditions)
        )
    ).one()
    return int(row[0]), int(row[1])
