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
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.shift import Shift, ShiftFinishReason, ShiftStatus
from src.app.services import audit as audit_service

logger = get_logger(__name__)


def _finalize_shift_checklists_sync(session: Session, shift_id: uuid.UUID) -> bool:
    """Sync version of checklist_instance.finalize_shift_checklists.

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
    result = session.execute(
        select(func.count()).where(
            ChecklistInstance.shift_id == shift_id,
            ChecklistInstance.status == ChecklistInstanceStatus.incomplete,
            ChecklistInstance.is_required.is_(True),
        )
    )
    return result.scalar_one() > 0


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
            has_incomplete = _finalize_shift_checklists_sync(session, shift.id)
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
