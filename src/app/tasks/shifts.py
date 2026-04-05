from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.app.core.celery_app import celery_app
from src.app.core.config import get_settings
from src.app.core.database import get_sync_session
from src.app.core.logging import get_logger
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.shift import Shift, ShiftStatus

logger = get_logger(__name__)
settings = get_settings()


@celery_app.task(name="auto_finish_stale_shifts")
def auto_finish_stale_shifts() -> None:
    """Auto-finish shifts that exceeded timeout."""
    with get_sync_session() as session:
        now = datetime.now(UTC)

        # 1. Personal shifts (no org) — use global default
        global_cutoff = now - timedelta(hours=settings.default_auto_finish_hours)
        result = session.execute(
            select(Shift)
            .options(selectinload(Shift.pauses))
            .where(
                Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
                Shift.organization_id.is_(None),
                Shift.started_at < global_cutoff,
            )
        )
        personal_shifts = list(result.scalars().all())

        # 2. Org shifts — use per-org auto_finish_hours
        org_settings_result = session.execute(select(OrganizationSettings))
        all_org_settings = {
            s.organization_id: s for s in org_settings_result.scalars().all()
        }

        org_result = session.execute(
            select(Shift)
            .options(selectinload(Shift.pauses))
            .where(
                Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
                Shift.organization_id.isnot(None),
            )
        )
        org_shifts = list(org_result.scalars().all())

        stale_org_shifts = []
        for shift in org_shifts:
            org_s = all_org_settings.get(shift.organization_id)
            hours = (
                org_s.auto_finish_hours if org_s else settings.default_auto_finish_hours
            )
            cutoff = now - timedelta(hours=hours)
            if shift.started_at < cutoff:
                stale_org_shifts.append(shift)

        all_stale = personal_shifts + stale_org_shifts

        for shift in all_stale:
            for pause in shift.pauses:
                if pause.finished_at is None:
                    pause.finished_at = now
            shift.status = ShiftStatus.finished
            shift.finished_at = now

        count = len(all_stale)
        if count > 0:
            logger.info("stale_shifts_finished", count=count)


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
            )
        )
        paused_shifts = list(result.scalars().all())

        # Load all relevant org settings in one query
        org_ids = {s.organization_id for s in paused_shifts}
        if not org_ids:
            return

        settings_result = session.execute(
            select(OrganizationSettings).where(
                OrganizationSettings.organization_id.in_(org_ids)
            )
        )
        org_settings_map = {
            s.organization_id: s for s in settings_result.scalars().all()
        }

        count = 0
        for shift in paused_shifts:
            org_s = org_settings_map.get(shift.organization_id)
            if org_s is None or org_s.max_pause_minutes is None:
                continue

            max_pause = timedelta(minutes=org_s.max_pause_minutes)
            for pause in shift.pauses:
                if pause.finished_at is None and (now - pause.started_at) > max_pause:
                    pause.finished_at = pause.started_at + max_pause
                    shift.status = ShiftStatus.active
                    count += 1
                    break

        if count > 0:
            logger.info("stale_pauses_finished", count=count)
