"""Ручные начисления/удержания организации (manual_time_entry).

Знаковая сумма (`amount_minor`): `> 0` — доплата, `< 0` — удержание. Симметрично
`services/penalty.py`, но без шаблонов и с обеими сторонами знака. Отмена —
soft-delete (`is_deleted = true`). Каждая операция (создание/правка/отмена)
пишет уведомление сотруднику (`payroll_adjustment_changed`) и запись в
`audit_logs` в той же транзакции — прозрачность для сотрудника (R7).

Категории начислений (payroll_breakdown) — справочник организации
(`PayrollAdjustmentCategory`, soft-delete по ADR-003): CRUD здесь же, назначение
категории начислению — `category_id` в create/update. Категория знак не задаёт.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.adjustment import PayrollAdjustment, PayrollAdjustmentCategory
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.models.notification import NotificationType
from src.app.models.organization import OrganizationMember
from src.app.models.shift import Shift
from src.app.models.user import User
from src.app.services import audit as audit_service
from src.app.services import entitlements
from src.app.services import notification as notification_service
from src.app.services import organization as org_service
from src.app.services.common import ensure_admin_or_owner
from src.app.services.shift import ensure_utc, validate_date_range

logger = get_logger(__name__)

ADJUSTMENT_CURRENCY = "RUB"
CATEGORY_NAME_MAX_LENGTH = 100
CATEGORY_UNIQUE_INDEX = "uq_payroll_adjustment_categories_org_name"
NO_CATEGORY_TOKEN = "none"  # noqa: S105 — спецзначение фильтра, не секрет


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
    *,
    include_deleted: bool = False,
) -> PayrollAdjustment:
    """Начисление организации; по умолчанию только активное — отменённое/чужое →
    ADJUSTMENT_NOT_FOUND. `include_deleted=True` — для restore."""
    conditions = [
        PayrollAdjustment.id == adjustment_id,
        PayrollAdjustment.organization_id == org_id,
    ]
    if not include_deleted:
        conditions.append(PayrollAdjustment.is_deleted.is_(False))
    result = await session.execute(select(PayrollAdjustment).where(*conditions))
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


async def _get_live_category(
    session: AsyncSession,
    org_id: uuid.UUID,
    category_id: uuid.UUID,
) -> PayrollAdjustmentCategory:
    """Неудалённая категория этой организации; чужая/удалённая/несуществующая →
    404 ADJUSTMENT_CATEGORY_NOT_FOUND (для PATCH/DELETE и назначения начислению)."""
    result = await session.execute(
        select(PayrollAdjustmentCategory).where(
            PayrollAdjustmentCategory.id == category_id,
            PayrollAdjustmentCategory.organization_id == org_id,
            PayrollAdjustmentCategory.is_deleted.is_(False),
        )
    )
    category = result.scalar_one_or_none()
    if category is None:
        raise AdjustmentError(
            "ADJUSTMENT_CATEGORY_NOT_FOUND", "Категория начислений не найдена", 404
        )
    return category


def _normalize_category_name(name: str) -> str:
    """trim + 1..100 символов; иначе 422 VALIDATION_ERROR (defense in depth к схеме)."""
    normalized = name.strip()
    if not normalized or len(normalized) > CATEGORY_NAME_MAX_LENGTH:
        raise AdjustmentError(
            "VALIDATION_ERROR",
            f"Название категории — от 1 до {CATEGORY_NAME_MAX_LENGTH} символов",
            422,
        )
    return normalized


def _category_duplicate_error() -> AdjustmentError:
    return AdjustmentError(
        "ADJUSTMENT_CATEGORY_DUPLICATE",
        "Категория с таким названием уже есть",
        409,
    )


async def _ensure_category_name_free(
    session: AsyncSession,
    org_id: uuid.UUID,
    name: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """Живой категории с таким именем (без учёта регистра) в org быть не должно.

    Быстрый путь с понятной ошибкой; гонку двух параллельных запросов закрывает
    частичный уникальный индекс (IntegrityError → тот же 409 в `_flush_category`).
    """
    conditions = [
        PayrollAdjustmentCategory.organization_id == org_id,
        PayrollAdjustmentCategory.is_deleted.is_(False),
        func.lower(PayrollAdjustmentCategory.name) == name.lower(),
    ]
    if exclude_id is not None:
        conditions.append(PayrollAdjustmentCategory.id != exclude_id)
    result = await session.execute(select(PayrollAdjustmentCategory.id).where(*conditions))
    if result.first() is not None:
        raise _category_duplicate_error()


async def _flush_category(session: AsyncSession) -> None:
    """flush с переводом нарушения уникального индекса имени в 409 (гонка POST/PATCH)."""
    try:
        await session.flush()
    except IntegrityError as exc:
        if CATEGORY_UNIQUE_INDEX in str(exc.orig):
            raise _category_duplicate_error() from None
        raise


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
        "restored": "Ваша корректировка зарплаты восстановлена",
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
    category_id: uuid.UUID | None = None,
) -> PayrollAdjustment:
    """Начислить/удержать. occurred_at по умолчанию = started_at смены при shift_id.
    `category_id` — живая категория этой org (иначе 404 ADJUSTMENT_CATEGORY_NOT_FOUND)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)
    member = await _get_member(session, org_id, member_id)

    if amount_minor == 0:
        raise AdjustmentError("VALIDATION_ERROR", "amount_minor не может быть равен 0", 422)

    shift = None
    if shift_id is not None:
        shift = await _validate_shift_for_member(session, org_id, member, shift_id)
    if category_id is not None:
        await _get_live_category(session, org_id, category_id)

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
        category_id=category_id,
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
            "category_id": str(category_id) if category_id is not None else None,
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
    category_filter: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    include_deleted: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[PayrollAdjustment], int]:
    """Активные начисления организации под фильтром. Returns (adjustments, total).

    `category_filter` — UUID категории (в т.ч. удалённой — фильтр по ссылке) или
    спецзначение `none` («Без категории», `category_id IS NULL`); разбирается
    после проверки прав (битое значение → 422 только для owner/admin)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    category_id, without_category = parse_category_filter(category_filter)

    validate_date_range(date_from, date_to)
    conditions = [PayrollAdjustment.organization_id == org_id]
    if not include_deleted:
        conditions.append(PayrollAdjustment.is_deleted.is_(False))
    if member_id is not None:
        conditions.append(PayrollAdjustment.member_id == member_id)
    if shift_id is not None:
        conditions.append(PayrollAdjustment.shift_id == shift_id)
    if without_category:
        conditions.append(PayrollAdjustment.category_id.is_(None))
    elif category_id is not None:
        conditions.append(PayrollAdjustment.category_id == category_id)
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
    await entitlements.require_active_subscription(session, org, requester_id)
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

    # «Не передано» (ключа нет) — не трогаем; явный null — сброс в «Без категории».
    # Назначаемая (новая) категория должна быть живой; оставить прежнюю (даже
    # удалённую) — допустимо, валидация не нужна.
    if "category_id" in fields:
        new_category_id = fields["category_id"]
        if new_category_id != adjustment.category_id:
            if new_category_id is not None:
                await _get_live_category(session, org_id, new_category_id)
            changed["category_id"] = {
                "from": str(adjustment.category_id) if adjustment.category_id else None,
                "to": str(new_category_id) if new_category_id else None,
            }
            adjustment.category_id = new_category_id

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
    """Отменить начисление (soft-delete). Повторный вызов на уже отменённом — 404."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)
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


async def restore_adjustment(
    session: AsyncSession,
    org_id: uuid.UUID,
    adjustment_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> PayrollAdjustment:
    """Восстановить отменённое начисление. На не-отменённом — 409 ADJUSTMENT_NOT_DELETED."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)
    adjustment = await _get_adjustment(session, org_id, adjustment_id, include_deleted=True)
    if not adjustment.is_deleted:
        raise AdjustmentError("ADJUSTMENT_NOT_DELETED", "Начисление не отменено", 409)

    adjustment.is_deleted = False
    adjustment.deleted_by_user_id = None
    adjustment.deleted_at = None
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.adjustment_restore,
        resource_type=AuditResource.adjustment,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=adjustment.id,
        summary={"restored": True},
    )
    member_result = await session.execute(
        select(OrganizationMember.user_id).where(OrganizationMember.id == adjustment.member_id)
    )
    user_id = member_result.scalar_one()
    await _notify_adjustment_changed(
        session,
        org_id=org_id,
        user_id=user_id,
        action="restored",
        adjustment=adjustment,
    )

    logger.info(
        "adjustment_restored",
        org_id=str(org_id),
        adjustment_id=str(adjustment_id),
    )
    return adjustment


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


# --- Категории начислений (payroll_breakdown) --------------------------------
async def count_adjustments_by_category(
    session: AsyncSession,
    category_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """category_id → число неотменённых начислений с этой категорией (один запрос)."""
    if not category_ids:
        return {}
    result = await session.execute(
        select(PayrollAdjustment.category_id, func.count(PayrollAdjustment.id))
        .where(
            PayrollAdjustment.category_id.in_(category_ids),
            PayrollAdjustment.is_deleted.is_(False),
        )
        .group_by(PayrollAdjustment.category_id)
    )
    return {
        category_id: int(count) for category_id, count in result.all() if category_id is not None
    }


async def get_category_names(
    session: AsyncSession,
    category_ids: set[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """category_id → имя, включая удалённые категории (для ответов и отчётов)."""
    if not category_ids:
        return {}
    result = await session.execute(
        select(PayrollAdjustmentCategory.id, PayrollAdjustmentCategory.name).where(
            PayrollAdjustmentCategory.id.in_(category_ids)
        )
    )
    return dict(result.tuples().all())


async def list_categories(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> list[PayrollAdjustmentCategory]:
    """Категории организации, сортировка по lower(name). Owner/admin."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)

    conditions = [PayrollAdjustmentCategory.organization_id == org_id]
    if not include_deleted:
        conditions.append(PayrollAdjustmentCategory.is_deleted.is_(False))
    result = await session.execute(
        select(PayrollAdjustmentCategory)
        .where(*conditions)
        .order_by(
            func.lower(PayrollAdjustmentCategory.name),
            PayrollAdjustmentCategory.created_at,
        )
    )
    return list(result.scalars().all())


async def create_category(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    name: str,
) -> PayrollAdjustmentCategory:
    """Создать категорию. Дубль живого имени без учёта регистра → 409."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)

    normalized = _normalize_category_name(name)
    await _ensure_category_name_free(session, org_id, normalized)

    category = PayrollAdjustmentCategory(
        organization_id=org_id,
        name=normalized,
        created_by_user_id=requester_id,
    )
    session.add(category)
    await _flush_category(session)
    await audit_service.record(
        session,
        action=AuditAction.adjustment_category_create,
        resource_type=AuditResource.adjustment_category,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=category.id,
        summary={"name": normalized},
    )
    logger.info(
        "adjustment_category_created",
        org_id=str(org_id),
        category_id=str(category.id),
    )
    return category


async def update_category(
    session: AsyncSession,
    org_id: uuid.UUID,
    category_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    name: str,
) -> PayrollAdjustmentCategory:
    """Переименовать живую категорию (удалённую → 404). Ссылка, не снимок:
    новое имя видно во всех начислениях и отчётах."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)
    category = await _get_live_category(session, org_id, category_id)

    normalized = _normalize_category_name(name)
    if normalized != category.name:
        await _ensure_category_name_free(session, org_id, normalized, exclude_id=category.id)
        previous_name = category.name
        category.name = normalized
        await _flush_category(session)
        await audit_service.record(
            session,
            action=AuditAction.adjustment_category_update,
            resource_type=AuditResource.adjustment_category,
            organization_id=org_id,
            actor_user_id=requester_id,
            resource_id=category.id,
            summary={"changed": {"name": {"from": previous_name, "to": normalized}}},
        )
    logger.info(
        "adjustment_category_updated",
        org_id=str(org_id),
        category_id=str(category_id),
    )
    return category


async def delete_category(
    session: AsyncSession,
    org_id: uuid.UUID,
    category_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Soft-delete (ADR-003). Начисления сохраняют ссылку на категорию.
    Повторный вызов на удалённой — 404."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)
    category = await _get_live_category(session, org_id, category_id)

    category.is_deleted = True
    category.deleted_at = datetime.now(UTC)
    category.deleted_by_user_id = requester_id
    await session.flush()
    await audit_service.record(
        session,
        action=AuditAction.adjustment_category_delete,
        resource_type=AuditResource.adjustment_category,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=category.id,
        summary={"name": category.name},
    )
    logger.info(
        "adjustment_category_deleted",
        org_id=str(org_id),
        category_id=str(category_id),
        deleted_by=str(requester_id),
    )


def parse_category_filter(value: str | None) -> tuple[uuid.UUID | None, bool]:
    """Фильтр `category_id` списка начислений: UUID или спецзначение `none`
    («Без категории»). Returns (category_id, only_without_category); битое → 422."""
    if value is None:
        return None, False
    token = value.strip()
    if token.lower() == NO_CATEGORY_TOKEN:
        return None, True
    try:
        return uuid.UUID(token), False
    except ValueError:
        raise AdjustmentError(
            "VALIDATION_ERROR", f"Некорректный category_id: {value}", 422
        ) from None


# --- Агрегаты для payroll ----------------------------------------------------
@dataclass(frozen=True)
class AdjustmentReportRow:
    """Неотменённое начисление, попавшее в отчёт payroll (payroll_breakdown).

    Единственный источник и для агрегатов `items[]`/`totals` (суммы, доплаты,
    удержания, `adjustments_by_category`), и для построчного листа Excel
    «Начисления и удержания» — фильтры не могут разъехаться.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    amount_minor: int
    reason: str
    comment: str | None
    occurred_at: datetime
    category_id: uuid.UUID | None
    category_name: str | None
    shift_started_at: datetime | None
    created_by_name: str | None


async def fetch_report_adjustments(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    date_from: datetime | None,
    date_to: datetime | None,
    user_ids: list[uuid.UUID] | None = None,
) -> list[AdjustmentReportRow]:
    """Неотменённые начисления организации за период для отчёта payroll.

    Атрибуция к сотруднику — через member_id → organization_members.user_id.
    Период — по `occurred_at`, `date_to` включительно (UTC). `user_ids` —
    фильтр сотрудников отчёта (пусто/None — все). Имя категории — в т.ч.
    удалённой; время начала привязанной смены и имя внёсшего — одним запросом.
    """
    conditions = [
        PayrollAdjustment.organization_id == org_id,
        PayrollAdjustment.is_deleted.is_(False),
    ]
    if date_from is not None:
        conditions.append(PayrollAdjustment.occurred_at >= date_from)
    if date_to is not None:
        conditions.append(PayrollAdjustment.occurred_at <= date_to)
    if user_ids:
        conditions.append(OrganizationMember.user_id.in_(user_ids))

    result = await session.execute(
        select(
            PayrollAdjustment.id,
            OrganizationMember.user_id,
            PayrollAdjustment.amount_minor,
            PayrollAdjustment.reason,
            PayrollAdjustment.comment,
            PayrollAdjustment.occurred_at,
            PayrollAdjustment.category_id,
            PayrollAdjustmentCategory.name,
            Shift.started_at,
            User.name,
        )
        .join(OrganizationMember, PayrollAdjustment.member_id == OrganizationMember.id)
        .outerjoin(
            PayrollAdjustmentCategory,
            PayrollAdjustment.category_id == PayrollAdjustmentCategory.id,
        )
        .outerjoin(Shift, PayrollAdjustment.shift_id == Shift.id)
        .outerjoin(User, PayrollAdjustment.created_by_user_id == User.id)
        .where(*conditions)
        .order_by(PayrollAdjustment.occurred_at, PayrollAdjustment.id)
    )
    return [
        AdjustmentReportRow(
            id=row[0],
            user_id=row[1],
            amount_minor=int(row[2]),
            reason=row[3],
            comment=row[4],
            occurred_at=row[5],
            category_id=row[6],
            category_name=row[7],
            shift_started_at=row[8],
            created_by_name=row[9],
        )
        for row in result.all()
    ]


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


async def aggregate_adjustments_by_shift(
    session: AsyncSession,
    shift_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[int, int]]:
    """shift_id → (знаковая сумма активных начислений в копейках, число), привязанных
    именно к этой смене (`payroll_adjustments.shift_id`).

    Одним запросом для всей страницы смен (shift_history_earnings, ADR-005 п.4) —
    непривязанные к смене начисления сюда не попадают (они видны только в
    `fetch_report_adjustments`/`aggregate_member_adjustments` за период). Только
    is_deleted=false.
    """
    if not shift_ids:
        return {}
    result = await session.execute(
        select(
            PayrollAdjustment.shift_id,
            func.coalesce(func.sum(PayrollAdjustment.amount_minor), 0),
            func.count(PayrollAdjustment.id),
        )
        .where(
            PayrollAdjustment.shift_id.in_(shift_ids),
            PayrollAdjustment.is_deleted.is_(False),
        )
        .group_by(PayrollAdjustment.shift_id)
    )
    return {shift_id: (int(total), int(count)) for shift_id, total, count in result.all()}
