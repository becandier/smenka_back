import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.config import get_settings
from src.app.models.shift import Pause, Shift, ShiftStatus

settings = get_settings()


class ShiftError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


def calculate_worked_seconds(shift: Shift) -> int:
    """Calculate total worked seconds for a shift (total duration minus pauses)."""
    now = datetime.now(UTC)
    end = shift.finished_at or now

    total = (end - shift.started_at).total_seconds()

    for pause in shift.pauses:
        pause_end = pause.finished_at or now
        total -= (pause_end - pause.started_at).total_seconds()

    return max(0, int(total))


async def _get_shift_with_pauses(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Load shift with pauses, verify ownership."""
    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(Shift.id == shift_id, Shift.user_id == user_id)
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise ShiftError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


async def _auto_finish_stale_shifts(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> None:
    """Auto-finish shifts that exceeded the timeout (default 16h)."""
    timeout_hours = settings.default_auto_finish_hours
    cutoff = datetime.now(UTC) - timedelta(hours=timeout_hours)

    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(
            Shift.user_id == user_id,
            Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
            Shift.started_at < cutoff,
        )
    )
    stale_shifts = result.scalars().all()

    for shift in stale_shifts:
        for pause in shift.pauses:
            if pause.finished_at is None:
                pause.finished_at = datetime.now(UTC)
        shift.status = ShiftStatus.finished
        shift.finished_at = datetime.now(UTC)

    if stale_shifts:
        await session.flush()


async def start_shift(session: AsyncSession, user_id: uuid.UUID) -> Shift:
    """Start a new shift. Only one active/paused shift allowed at a time."""
    await _auto_finish_stale_shifts(session, user_id)

    result = await session.execute(
        select(Shift).where(
            Shift.user_id == user_id,
            Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None:
        raise ShiftError(
            "SHIFT_ALREADY_ACTIVE",
            "У вас уже есть активная смена",
            409,
        )

    shift = Shift(user_id=user_id)
    session.add(shift)
    await session.flush()

    return await _get_shift_with_pauses(session, shift.id, user_id)


async def pause_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Pause an active shift."""
    shift = await _get_shift_with_pauses(session, shift_id, user_id)

    if shift.status != ShiftStatus.active:
        raise ShiftError("SHIFT_NOT_ACTIVE", "Смена не активна", 400)

    pause = Pause(shift_id=shift.id)
    session.add(pause)
    shift.status = ShiftStatus.paused
    await session.flush()
    session.expire(shift, ["pauses"])

    return await _get_shift_with_pauses(session, shift.id, user_id)


async def resume_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Resume a paused shift."""
    shift = await _get_shift_with_pauses(session, shift_id, user_id)

    if shift.status != ShiftStatus.paused:
        raise ShiftError("SHIFT_NOT_PAUSED", "Смена не на паузе", 400)

    for pause in shift.pauses:
        if pause.finished_at is None:
            pause.finished_at = datetime.now(UTC)
            break

    shift.status = ShiftStatus.active
    await session.flush()

    return await _get_shift_with_pauses(session, shift.id, user_id)


async def finish_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Finish an active or paused shift."""
    shift = await _get_shift_with_pauses(session, shift_id, user_id)

    if shift.status == ShiftStatus.finished:
        raise ShiftError("SHIFT_ALREADY_FINISHED", "Смена уже завершена", 400)

    for pause in shift.pauses:
        if pause.finished_at is None:
            pause.finished_at = datetime.now(UTC)

    shift.status = ShiftStatus.finished
    shift.finished_at = datetime.now(UTC)
    await session.flush()

    return await _get_shift_with_pauses(session, shift.id, user_id)
