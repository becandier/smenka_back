"""Ручной ввод/правка/удаление/восстановление смен администратором
(manual_time_entry).

Реализует инструмент А из ТЗ: владелец/админ организации заводит смену
задним числом, правит существующую (в т.ч. завершает зависшую активную),
удаляет ошибочную (soft-delete) и восстанавливает удалённую. Каждая операция
пишет запись в `audit_logs` и уведомление сотруднику (`shift_manual_changed`)
в той же транзакции (R7) — вызывающий код (роутер) сам делает `commit`.

Переиспользует `ShiftError` из `services/shift.py` — это тот же ресурс
(смена), тот же уже зарегистрированный exception-хендлер в `main.py`.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.models.notification import NotificationType
from src.app.models.organization import OrganizationMember
from src.app.models.shift import Pause, Shift, ShiftFinishReason, ShiftStatus
from src.app.services import audit as audit_service
from src.app.services import entitlements
from src.app.services import notification as notification_service
from src.app.services import organization as org_service
from src.app.services.common import ensure_admin_or_owner
from src.app.services.shift import ShiftError, calculate_worked_seconds, ensure_utc

logger = get_logger(__name__)

MAX_SHIFT_DURATION = timedelta(hours=48)
CLOCK_SKEW = timedelta(seconds=60)


# --- Внутренние помощники ----------------------------------------------------
async def _get_member_by_user(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> OrganizationMember:
    """Действующий участник организации. Owner != member (ADR-001) ⇒ 404."""
    result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise ShiftError("MEMBER_NOT_FOUND", "Участник не найден", 404)
    return member


async def _get_manual_shift(
    session: AsyncSession,
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    *,
    include_deleted: bool = False,
) -> Shift:
    conditions = [Shift.id == shift_id, Shift.organization_id == org_id]
    if not include_deleted:
        conditions.append(Shift.is_deleted.is_(False))
    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses), selectinload(Shift.work_location))
        .where(*conditions)
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise ShiftError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


async def _require_org_location(
    session: AsyncSession,
    org_id: uuid.UUID,
    work_location_id: uuid.UUID,
) -> uuid.UUID:
    """R5: точка должна принадлежать организации; архивных/удалённых у точек нет."""
    from src.app.models.work_location import WorkLocation

    result = await session.execute(
        select(WorkLocation.id).where(
            WorkLocation.id == work_location_id,
            WorkLocation.organization_id == org_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ShiftError("WORK_LOCATION_NOT_FOUND", "Рабочая точка не найдена", 404)
    return work_location_id


def _validate_started_at_not_future(started_at: datetime) -> None:
    if started_at > datetime.now(UTC) + CLOCK_SKEW:
        raise ShiftError("VALIDATION_ERROR", "started_at не может быть в будущем", 422)


def _validate_interval(started_at: datetime, finished_at: datetime) -> None:
    """R1: строгий порядок, потолок 48ч, не в будущем (допуск 60с)."""
    if started_at >= finished_at:
        raise ShiftError("VALIDATION_ERROR", "started_at должен быть раньше finished_at", 422)
    if finished_at - started_at > MAX_SHIFT_DURATION:
        raise ShiftError("VALIDATION_ERROR", "Длительность смены не может превышать 48 часов", 422)
    _validate_started_at_not_future(started_at)
    if finished_at > datetime.now(UTC) + CLOCK_SKEW:
        raise ShiftError("VALIDATION_ERROR", "finished_at не может быть в будущем", 422)


async def _check_overlap(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    started_at: datetime,
    finished_at: datetime,
    *,
    exclude_shift_id: uuid.UUID | None = None,
) -> None:
    """R2: запрет пересечений с любой другой неудалённой сменой того же сотрудника
    в этой же организации. Активная/паузная смена-сосед (finished_at IS NULL)
    считается открытой до бесконечности — эквивалентно «открыта до `now`»,
    поскольку R1 запрещает `started_at` проверяемого интервала быть в будущем
    (он неизбежно раньше произвольного «сейчас» соседа). Касание границами —
    не пересечение (строгие неравенства). Один индексируемый EXISTS-запрос —
    без загрузки всей истории смен сотрудника в Python.
    """
    conditions = [
        Shift.organization_id == org_id,
        Shift.user_id == user_id,
        Shift.is_deleted.is_(False),
        Shift.started_at < finished_at,
        or_(Shift.finished_at.is_(None), Shift.finished_at > started_at),
    ]
    if exclude_shift_id is not None:
        conditions.append(Shift.id != exclude_shift_id)
    result = await session.execute(select(Shift.id).where(*conditions).limit(1))
    if result.scalar_one_or_none() is not None:
        raise ShiftError("SHIFT_OVERLAP", "Интервал пересекается с другой сменой сотрудника", 409)


def _validate_pauses(
    pauses: list[Any],
    shift_started_at: datetime,
    shift_finished_at: datetime,
) -> None:
    """R3: границы внутри смены, не пересекаются между собой, суммарно короче смены.

    Работает как над Pydantic `ManualPauseInput`, так и над ORM `Pause` — нужны
    только атрибуты `started_at`/`finished_at`.
    """
    ordered = sorted(pauses, key=lambda p: ensure_utc(p.started_at))
    prev_end: datetime | None = None
    total_seconds = 0.0
    for p in ordered:
        started = ensure_utc(p.started_at)
        finished = ensure_utc(p.finished_at)
        if started >= finished:
            raise ShiftError("VALIDATION_ERROR", "Начало паузы должно быть раньше её конца", 422)
        if started < shift_started_at or finished > shift_finished_at:
            raise ShiftError("VALIDATION_ERROR", "Пауза должна быть внутри интервала смены", 422)
        if prev_end is not None and started < prev_end:
            raise ShiftError("VALIDATION_ERROR", "Паузы не должны пересекаться", 422)
        prev_end = finished
        total_seconds += (finished - started).total_seconds()

    shift_seconds = (shift_finished_at - shift_started_at).total_seconds()
    if total_seconds >= shift_seconds:
        raise ShiftError(
            "VALIDATION_ERROR",
            "Суммарная длительность пауз не может быть больше или равна длительности смены",
            422,
        )


def _validate_started_at_against_open_pauses(
    new_started_at: datetime,
    pauses: list[Pause],
) -> None:
    """Для ещё открытой (active/paused) смены: сдвиг `started_at` не может
    поставить существующую паузу (в т.ч. незакрытую) раньше нового начала —
    верхней границы для полной `_validate_pauses` здесь ещё нет (смена не
    завершена), поэтому проверяем только левую границу."""
    for p in pauses:
        if ensure_utc(p.started_at) < new_started_at:
            raise ShiftError(
                "VALIDATION_ERROR",
                "started_at не может быть позже начала существующей паузы",
                422,
            )


async def _resolve_manual_schedule(
    session: AsyncSession,
    org_id: uuid.UUID,
    org_timezone: str,
    work_schedule_id: uuid.UUID,
    started_at: datetime,
) -> tuple[uuid.UUID, str, datetime, datetime]:
    """Снимок графика — переиспользует ту же логику, что `PATCH .../shifts/{id}/schedule`."""
    from src.app.services.work_schedule import (
        WorkScheduleError,
        _get_schedule,
        compute_scheduled_window,
    )

    try:
        schedule = await _get_schedule(session, org_id, work_schedule_id)
    except WorkScheduleError as exc:
        raise ShiftError(exc.code, exc.message, exc.status_code) from None
    start_utc, end_utc = compute_scheduled_window(
        started_at, ZoneInfo(org_timezone), schedule.start_time, schedule.end_time
    )
    return schedule.id, schedule.name, start_utc, end_utc


async def _notify_shift_changed(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    action: str,
    shift: Shift,
    note: str | None,
) -> None:
    title = {
        "created": "Администратор добавил вам смену",
        "updated": "Администратор изменил вашу смену",
        "deleted": "Администратор удалил вашу смену",
        "restored": "Администратор восстановил вашу смену",
    }[action]
    await notification_service.create_notification(
        session,
        user_id=user_id,
        type=NotificationType.shift_manual_changed.value,
        title=title,
        body=note,
        payload={
            "shift_id": str(shift.id),
            "action": action,
            "started_at": shift.started_at.isoformat(),
            "finished_at": shift.finished_at.isoformat() if shift.finished_at else None,
            "note": note,
        },
        organization_id=org_id,
    )


# --- A1: создать смену вручную ------------------------------------------------
async def create_manual_shift(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    started_at: datetime,
    finished_at: datetime,
    work_location_id: uuid.UUID | None,
    work_schedule_id: uuid.UUID | None,
    pauses: list[Any],
    note: str | None,
) -> Shift:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)
    await _get_member_by_user(session, org_id, user_id)

    started_at = ensure_utc(started_at)
    finished_at = ensure_utc(finished_at)
    _validate_interval(started_at, finished_at)
    _validate_pauses(pauses, started_at, finished_at)

    resolved_location_id: uuid.UUID | None = None
    if work_location_id is not None:
        resolved_location_id = await _require_org_location(session, org_id, work_location_id)

    await _check_overlap(session, org_id, user_id, started_at, finished_at)

    schedule_id = schedule_name = None
    scheduled_start_at = scheduled_end_at = None
    if work_schedule_id is not None:
        (
            schedule_id,
            schedule_name,
            scheduled_start_at,
            scheduled_end_at,
        ) = await _resolve_manual_schedule(
            session, org_id, org.timezone, work_schedule_id, started_at
        )

    shift = Shift(
        user_id=user_id,
        organization_id=org_id,
        work_location_id=resolved_location_id,
        started_at=started_at,
        finished_at=finished_at,
        status=ShiftStatus.finished,
        finish_reason=ShiftFinishReason.manual,
        has_incomplete_required_checklists=False,
        created_by_user_id=requester_id,
        manual_note=note,
        work_schedule_id=schedule_id,
        schedule_name=schedule_name,
        scheduled_start_at=scheduled_start_at,
        scheduled_end_at=scheduled_end_at,
    )
    shift.pauses = [
        Pause(started_at=ensure_utc(p.started_at), finished_at=ensure_utc(p.finished_at))
        for p in pauses
    ]
    session.add(shift)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.shift_manual_create,
        resource_type=AuditResource.shift,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=shift.id,
        summary={
            "user_id": str(user_id),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "work_location_id": str(resolved_location_id) if resolved_location_id else None,
            "pauses_count": len(pauses),
            "note": note,
        },
    )
    await _notify_shift_changed(
        session, org_id=org_id, user_id=user_id, action="created", shift=shift, note=note
    )

    logger.info(
        "manual_shift_created", org_id=str(org_id), shift_id=str(shift.id), user_id=str(user_id)
    )
    return await _get_manual_shift(session, org_id, shift.id)


# --- A2: изменить смену вручную -----------------------------------------------
async def update_manual_shift(
    session: AsyncSession,
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    requester_id: uuid.UUID,
    fields: dict[str, Any],
) -> Shift:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)
    shift = await _get_manual_shift(session, org_id, shift_id)

    was_open = shift.status != ShiftStatus.finished
    finishing_now = was_open and fields.get("finished_at") is not None

    new_started_at = (
        ensure_utc(fields["started_at"])
        if fields.get("started_at") is not None
        else shift.started_at
    )

    new_finished_at: datetime | None
    if finishing_now:
        new_finished_at = ensure_utc(fields["finished_at"])
    elif not was_open:
        new_finished_at = (
            ensure_utc(fields["finished_at"])
            if fields.get("finished_at") is not None
            else shift.finished_at
        )
    else:
        new_finished_at = None  # смена остаётся active/paused

    # Полная валидация интервала (48ч/не-в-будущем) и пересечений нужна ТОЛЬКО
    # когда сам интервал реально меняется — иначе PATCH, трогающий лишь note/
    # work_location_id у уже существующей (в т.ч. легаси >48ч) смены, будет
    # навсегда заблокирован повторной проверкой уже принятых старых значений.
    interval_changed = new_started_at != shift.started_at or new_finished_at != shift.finished_at
    if interval_changed:
        if new_finished_at is not None:
            _validate_interval(new_started_at, new_finished_at)
        else:
            _validate_started_at_not_future(new_started_at)

        effective_end = new_finished_at if new_finished_at is not None else datetime.now(UTC)
        await _check_overlap(
            session,
            org_id,
            shift.user_id,
            new_started_at,
            effective_end,
            exclude_shift_id=shift.id,
        )

    resolved_location_id = shift.work_location_id
    if "work_location_id" in fields:
        resolved_location_id = (
            await _require_org_location(session, org_id, fields["work_location_id"])
            if fields["work_location_id"] is not None
            else None
        )

    pauses_replaced = False
    if fields.get("pauses") is not None:
        if new_finished_at is None:
            raise ShiftError(
                "VALIDATION_ERROR",
                "Правка пауз доступна только для смены с указанным finished_at "
                "(передайте его в этом же запросе)",
                422,
            )
        _validate_pauses(fields["pauses"], new_started_at, new_finished_at)
        shift.pauses = [
            Pause(started_at=ensure_utc(p.started_at), finished_at=ensure_utc(p.finished_at))
            for p in fields["pauses"]
        ]
        pauses_replaced = True
    elif new_finished_at is not None:
        if finishing_now:
            # R2 (A2): незакрытая пауза закрывается finished_at, если начата
            # раньше него, иначе удаляется (не может пережить конец смены).
            for pause in list(shift.pauses):
                if pause.finished_at is None:
                    if pause.started_at < new_finished_at:
                        pause.finished_at = new_finished_at
                    else:
                        shift.pauses.remove(pause)
        elif interval_changed:
            # started_at/finished_at поменялись без явной правки pauses —
            # существующие паузы обязаны остаться внутри нового интервала.
            _validate_pauses(list(shift.pauses), new_started_at, new_finished_at)
    elif interval_changed and shift.pauses:
        # Смена остаётся open (active/paused), но started_at сдвинулся:
        # верхней границы ещё нет, проверяем только левую (см. docstring).
        _validate_started_at_against_open_pauses(new_started_at, shift.pauses)

    changed: dict[str, Any] = {}
    if new_started_at != shift.started_at:
        changed["started_at"] = {
            "from": shift.started_at.isoformat(),
            "to": new_started_at.isoformat(),
        }
        shift.started_at = new_started_at
    if new_finished_at != shift.finished_at:
        changed["finished_at"] = {
            "from": shift.finished_at.isoformat() if shift.finished_at else None,
            "to": new_finished_at.isoformat() if new_finished_at else None,
        }
        shift.finished_at = new_finished_at
    if resolved_location_id != shift.work_location_id:
        changed["work_location_id"] = {
            "from": str(shift.work_location_id) if shift.work_location_id else None,
            "to": str(resolved_location_id) if resolved_location_id else None,
        }
        shift.work_location_id = resolved_location_id
    if pauses_replaced:
        changed["pauses"] = {"count": len(fields["pauses"])}
    if finishing_now:
        changed["status"] = {"from": shift.status.value, "to": ShiftStatus.finished.value}
        shift.status = ShiftStatus.finished
        shift.finish_reason = ShiftFinishReason.manual
    if "note" in fields:
        changed["note"] = {"from": shift.manual_note, "to": fields["note"]}
        shift.manual_note = fields["note"]

    shift.edited_by_user_id = requester_id
    shift.edited_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.shift_manual_update,
        resource_type=AuditResource.shift,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=shift.id,
        summary={"changed": changed, "note": shift.manual_note},
    )
    await _notify_shift_changed(
        session,
        org_id=org_id,
        user_id=shift.user_id,
        action="updated",
        shift=shift,
        note=shift.manual_note,
    )

    logger.info("manual_shift_updated", org_id=str(org_id), shift_id=str(shift_id))
    return await _get_manual_shift(session, org_id, shift.id)


# --- A3: удалить смену (soft-delete) ------------------------------------------
async def delete_shift(
    session: AsyncSession,
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    note: str | None,
) -> Shift:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)
    shift = await _get_manual_shift(session, org_id, shift_id)

    now = datetime.now(UTC)
    if shift.status != ShiftStatus.finished:
        # Soft-delete не должен оставлять «вечно активную» смену: без
        # принудительного завершения finished_at остался бы NULL навсегда, и
        # restore_shift считал бы интервал открытым до текущего `now()` при
        # каждой проверке пересечений — восстановление стало бы практически
        # недостижимым (см. code-review).
        for pause in list(shift.pauses):
            if pause.finished_at is None:
                pause.finished_at = now
        shift.status = ShiftStatus.finished
        shift.finished_at = now
        shift.finish_reason = ShiftFinishReason.manual

    worked_seconds = calculate_worked_seconds(shift)
    shift.is_deleted = True
    shift.deleted_by_user_id = requester_id
    shift.deleted_at = now
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.shift_delete,
        resource_type=AuditResource.shift,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=shift.id,
        summary={
            "user_id": str(shift.user_id),
            "started_at": shift.started_at.isoformat(),
            "finished_at": shift.finished_at.isoformat() if shift.finished_at else None,
            "worked_seconds": worked_seconds,
            "note": note,
        },
    )
    await _notify_shift_changed(
        session, org_id=org_id, user_id=shift.user_id, action="deleted", shift=shift, note=note
    )

    logger.info(
        "manual_shift_deleted",
        org_id=str(org_id),
        shift_id=str(shift_id),
        deleted_by=str(requester_id),
    )
    return shift


# --- A4: восстановить удалённую смену ------------------------------------------
async def restore_shift(
    session: AsyncSession,
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> Shift:
    """Восстановление неудалённой смены — идемпотентно, возвращает её как есть
    (не ошибка). 404 — только если смена не существует вовсе (backend.md, A4)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, allow_super_admin=False)
    await entitlements.require_active_subscription(session, org, requester_id)
    shift = await _get_manual_shift(session, org_id, shift_id, include_deleted=True)

    if not shift.is_deleted:
        return shift

    effective_end = shift.finished_at if shift.finished_at is not None else datetime.now(UTC)
    await _check_overlap(
        session,
        org_id,
        shift.user_id,
        shift.started_at,
        effective_end,
        exclude_shift_id=shift.id,
    )

    shift.is_deleted = False
    shift.deleted_by_user_id = None
    shift.deleted_at = None
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.shift_restore,
        resource_type=AuditResource.shift,
        organization_id=org_id,
        actor_user_id=requester_id,
        resource_id=shift.id,
        summary={
            "user_id": str(shift.user_id),
            "started_at": shift.started_at.isoformat(),
            "finished_at": shift.finished_at.isoformat() if shift.finished_at else None,
        },
    )
    await _notify_shift_changed(
        session, org_id=org_id, user_id=shift.user_id, action="restored", shift=shift, note=None
    )

    logger.info("manual_shift_restored", org_id=str(org_id), shift_id=str(shift_id))
    return shift
