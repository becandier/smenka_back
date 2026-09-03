import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from src.app.core.celery_app import celery_app
from src.app.core.database import get_sync_session
from src.app.core.logging import get_logger
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.models.checklist import ChecklistInstance, ChecklistInstanceStatus
from src.app.models.organization_settings import (
    DEFAULT_CHECKLIST_GRACE_MINUTES,
    OrganizationSettings,
)
from src.app.models.shift import Shift, ShiftFinishReason, ShiftStatus
from src.app.services import audit as audit_service

logger = get_logger(__name__)


def _has_live_incomplete_required_sync(session: Session, shift_id: uuid.UUID) -> bool:
    """Sync-версия checklist_instance._has_live_incomplete_required: есть ли у
    смены обязательный экземпляр, ещё не completed (pending — окно ещё
    открыто, incomplete — уже терминально зафиксирован)."""
    result = session.execute(
        select(func.count()).where(
            ChecklistInstance.shift_id == shift_id,
            ChecklistInstance.is_required.is_(True),
            ChecklistInstance.status != ChecklistInstanceStatus.completed,
        )
    )
    return result.scalar_one() > 0


def _finalize_shift_checklists_sync(session: Session, shift_id: uuid.UUID) -> bool:
    """Sync-версия checklist_instance.finalize_shift_checklists: терминально
    зафиксировать незакрытые обязательные pending → incomplete.

    Returns True if the shift now has incomplete required checklists.
    """
    session.execute(
        update(ChecklistInstance)
        .where(
            ChecklistInstance.shift_id == shift_id,
            ChecklistInstance.status == ChecklistInstanceStatus.pending,
            ChecklistInstance.is_required.is_(True),
        )
        .values(status=ChecklistInstanceStatus.incomplete)
    )
    return _has_live_incomplete_required_sync(session, shift_id)


def _close_shift_checklists_sync(
    session: Session,
    shift_id: uuid.UUID,
    grace_minutes: int,
) -> bool:
    """Sync-версия checklist_instance.close_shift_checklists (checklist_grace_period):
    вызывается при завершении смены (авто-финиш по графику). `grace_minutes <= 0` —
    прежнее поведение, терминальная фиксация сразу; иначе экземпляры остаются
    `pending` (окно дозаполнения открыто), а возвращается live-снимок."""
    if grace_minutes <= 0:
        return _finalize_shift_checklists_sync(session, shift_id)
    return _has_live_incomplete_required_sync(session, shift_id)


@celery_app.task(name="auto_finish_stale_shifts")
def auto_finish_stale_shifts() -> None:
    """Авто-завершение org-смен ровно в плановый конец графика (backend.md, R4).

    Персональные смены (`organization_id is None`) больше не авто-завершаются
    вообще — персональный трекер работает только по ручному завершению.
    `finished_at` ставится равным `scheduled_end_at` (момент срабатывания
    задачи в расчёт не идёт), `finish_reason = auto_schedule`.
    """
    with get_sync_session() as session:
        now = datetime.now(UTC)

        org_settings_result = session.execute(select(OrganizationSettings))
        all_org_settings = {s.organization_id: s for s in org_settings_result.scalars().all()}

        result = session.execute(
            select(Shift)
            .options(selectinload(Shift.pauses))
            .where(
                Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
                Shift.organization_id.isnot(None),
                Shift.is_deleted.is_(False),
                Shift.scheduled_end_at.isnot(None),
                Shift.scheduled_end_at <= now,
            )
        )

        stale: list[Shift] = []
        for shift in result.scalars().all():
            org_s = all_org_settings.get(cast(uuid.UUID, shift.organization_id))
            # Запись настроек отсутствует → считаем true (server_default).
            auto_finish_enabled = org_s.auto_finish_by_schedule if org_s is not None else True
            if auto_finish_enabled:
                stale.append(shift)

        for shift in stale:
            org_s = all_org_settings.get(cast(uuid.UUID, shift.organization_id))
            grace_minutes = (
                org_s.checklist_grace_minutes
                if org_s is not None
                else DEFAULT_CHECKLIST_GRACE_MINUTES
            )
            has_incomplete = _close_shift_checklists_sync(session, shift.id, grace_minutes)
            shift.has_incomplete_required_checklists = has_incomplete

            finish_at = shift.scheduled_end_at
            if finish_at is None:
                continue  # narrowing для mypy — уже отфильтровано в WHERE
            for pause in shift.pauses:
                if pause.finished_at is None:
                    pause.finished_at = finish_at
            shift.status = ShiftStatus.finished
            shift.finished_at = finish_at
            shift.finish_reason = ShiftFinishReason.auto_schedule

            audit_service.record_sync(
                session,
                action=AuditAction.shift_auto_finish,
                resource_type=AuditResource.shift,
                organization_id=shift.organization_id,
                actor_user_id=None,
                resource_id=shift.id,
                summary={
                    "finished_at": finish_at.isoformat(),
                    "work_schedule_id": (
                        str(shift.work_schedule_id) if shift.work_schedule_id else None
                    ),
                    "schedule_name": shift.schedule_name,
                },
            )

        if stale:
            logger.info("stale_shifts_finished", count=len(stale))


@celery_app.task(name="finalize_expired_checklist_grace_periods")
def finalize_expired_checklist_grace_periods() -> None:
    """Терминальная фиксация чек-листов по истечении окна дозаполнения
    (checklist_grace_period).

    `finish_shift`/`auto_finish_stale_shifts`/inline-авто-финиш (см.
    `close_shift_checklists`) при `checklist_grace_minutes > 0` намеренно НЕ
    переводят незакрытые обязательные экземпляры в терминальный `incomplete` —
    они остаются `pending`, дозаполнение разрешено. Эта задача (расписание —
    как у `auto_finish_stale_shifts`, раз в минуту) находит завершённые смены,
    у которых окно `checklist_grace_minutes` истекло, и фиксирует статус так
    же, как `finalize_shift_checklists` при мгновенном (grace=0) завершении.

    Идемпотентна: после фиксации ни один экземпляр не остаётся pending, смена
    выпадает из кандидатов на следующем прогоне (частичный индекс
    `ix_checklist_instances_pending_required`).
    """
    with get_sync_session() as session:
        now = datetime.now(UTC)

        candidate_shift_ids = list(
            session.execute(
                select(ChecklistInstance.shift_id)
                .where(
                    ChecklistInstance.status == ChecklistInstanceStatus.pending,
                    ChecklistInstance.is_required.is_(True),
                )
                .distinct()
            )
            .scalars()
            .all()
        )
        if not candidate_shift_ids:
            return

        shifts = list(
            session.execute(
                select(Shift).where(
                    Shift.id.in_(candidate_shift_ids),
                    Shift.status == ShiftStatus.finished,
                    Shift.is_deleted.is_(False),
                    Shift.finished_at.isnot(None),
                )
            )
            .scalars()
            .all()
        )
        if not shifts:
            return

        org_ids = {s.organization_id for s in shifts if s.organization_id is not None}
        settings_map: dict[uuid.UUID, OrganizationSettings] = {}
        if org_ids:
            settings_result = session.execute(
                select(OrganizationSettings).where(
                    OrganizationSettings.organization_id.in_(org_ids)
                )
            )
            settings_map = {s.organization_id: s for s in settings_result.scalars().all()}

        finalized = 0
        for shift in shifts:
            if shift.organization_id is None or shift.finished_at is None:
                continue  # narrowing для mypy — уже отфильтровано в WHERE
            org_s = settings_map.get(shift.organization_id)
            grace_minutes = (
                org_s.checklist_grace_minutes
                if org_s is not None
                else DEFAULT_CHECKLIST_GRACE_MINUTES
            )
            deadline = shift.finished_at + timedelta(minutes=grace_minutes)
            if now < deadline:
                continue  # окно ещё открыто

            has_incomplete = _finalize_shift_checklists_sync(session, shift.id)
            shift.has_incomplete_required_checklists = has_incomplete
            finalized += 1

        if finalized:
            logger.info("checklist_grace_periods_finalized", count=finalized)


@celery_app.task(name="auto_finish_stale_pauses")
def auto_finish_stale_pauses() -> None:
    """Auto-finish pauses exceeding org max_pause_minutes."""
    with get_sync_session() as session:
        now = datetime.now(UTC)

        result = session.execute(
            select(Shift)
            .options(selectinload(Shift.pauses))
            .where(
                Shift.status == ShiftStatus.paused,
                Shift.organization_id.isnot(None),
                Shift.is_deleted.is_(False),
            )
        )
        paused_shifts = list(result.scalars().all())

        # Load all relevant org settings in one query
        org_ids = {s.organization_id for s in paused_shifts}
        if not org_ids:
            return

        settings_result = session.execute(
            select(OrganizationSettings).where(OrganizationSettings.organization_id.in_(org_ids))
        )
        org_settings_map = {s.organization_id: s for s in settings_result.scalars().all()}

        count = 0
        for shift in paused_shifts:
            org_s = org_settings_map.get(cast(uuid.UUID, shift.organization_id))
            if org_s is None or org_s.max_pause_minutes is None:
                continue

            max_pause = timedelta(minutes=org_s.max_pause_minutes)
            for pause in shift.pauses:
                if pause.finished_at is None and (now - pause.started_at) > max_pause:
                    finished = pause.started_at + max_pause
                    pause.finished_at = finished
                    shift.status = ShiftStatus.active
                    count += 1
                    audit_service.record_sync(
                        session,
                        action=AuditAction.pause_auto_finish,
                        resource_type=AuditResource.pause,
                        organization_id=shift.organization_id,
                        actor_user_id=None,
                        resource_id=pause.id,
                        summary={"finished_at": finished.isoformat()},
                    )
                    break

        if count > 0:
            logger.info("stale_pauses_finished", count=count)
