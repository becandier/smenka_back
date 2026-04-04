import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
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


async def _validate_org_shift_start(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    latitude: float | None,
    longitude: float | None,
) -> None:
    """Validate org membership and geo check for org shift."""
    from src.app.models.organization import Organization, OrganizationMember
    from src.app.models.work_location import WorkLocation
    from src.app.services.organization_settings import get_settings_for_org

    # Check org exists and not deleted
    org_result = await session.execute(
        select(Organization).where(
            Organization.id == organization_id,
            Organization.is_deleted.is_(False),
        )
    )
    org = org_result.scalar_one_or_none()
    if org is None:
        raise ShiftError("ORG_NOT_FOUND", "Организация не найдена", 404)

    # Check membership
    member_result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
    )
    if member_result.scalar_one_or_none() is None:
        raise ShiftError("FORBIDDEN", "Вы не являетесь участником организации", 403)

    # Check geo if enabled
    org_settings = await get_settings_for_org(session, organization_id)
    if org_settings is not None and org_settings.geo_check_enabled:
        if latitude is None or longitude is None:
            raise ShiftError(
                "COORDS_REQUIRED",
                "Необходимо указать координаты для организации с геопроверкой",
                400,
            )

        locations_result = await session.execute(
            select(WorkLocation).where(
                WorkLocation.organization_id == organization_id,
            )
        )
        locations = list(locations_result.scalars().all())

        from src.app.utils.geo import is_within_radius

        within_any = any(
            is_within_radius(latitude, longitude, loc.latitude, loc.longitude, loc.radius_meters)
            for loc in locations
        )
        if not within_any:
            raise ShiftError(
                "GEO_CHECK_FAILED",
                "Вы находитесь вне зоны рабочих точек",
                403,
            )


async def start_shift(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> Shift:
    """Start a new shift.

    Rules:
    - One active personal shift + one active org shift per org allowed simultaneously.
    - If org has geo_check_enabled, latitude/longitude must be provided and within
      at least one WorkLocation radius.
    """
    await _auto_finish_stale_shifts(session, user_id)

    # Check for existing active shift in the same context
    conditions = [
        Shift.user_id == user_id,
        Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
    ]
    if organization_id is not None:
        conditions.append(Shift.organization_id == organization_id)
    else:
        conditions.append(Shift.organization_id.is_(None))

    result = await session.execute(select(Shift).where(*conditions))
    if result.scalar_one_or_none() is not None:
        raise ShiftError(
            "SHIFT_ALREADY_ACTIVE",
            "У вас уже есть активная смена",
            409,
        )

    # Organization-specific checks
    if organization_id is not None:
        await _validate_org_shift_start(
            session, user_id, organization_id, latitude, longitude,
        )

    shift = Shift(user_id=user_id, organization_id=organization_id)
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


VALID_PERIODS = {"day", "week", "month"}


async def get_shifts(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: ShiftStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Shift], int]:
    """Get paginated shift list with optional filters. Returns (shifts, total_count)."""
    await _auto_finish_stale_shifts(session, user_id)

    conditions = [Shift.user_id == user_id]

    if status is not None:
        conditions.append(Shift.status == status)
    if date_from is not None:
        conditions.append(Shift.started_at >= date_from)
    if date_to is not None:
        conditions.append(Shift.started_at <= date_to)

    count_query = select(func.count()).select_from(Shift).where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    query = (
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(*conditions)
        .order_by(Shift.started_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    shifts = list(result.scalars().all())

    return shifts, total


async def get_shift_stats(
    session: AsyncSession,
    user_id: uuid.UUID,
    period: str,
) -> dict:
    """Calculate shift statistics for the given period."""
    if period not in VALID_PERIODS:
        raise ShiftError("INVALID_PERIOD", f"Период должен быть: {', '.join(VALID_PERIODS)}", 400)

    await _auto_finish_stale_shifts(session, user_id)

    now = datetime.now(UTC)
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:  # month
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(
            Shift.user_id == user_id,
            Shift.started_at >= start,
        )
    )
    shifts = list(result.scalars().all())

    total_seconds = sum(calculate_worked_seconds(s) for s in shifts)
    count = len(shifts)
    avg = total_seconds // count if count > 0 else 0

    return {
        "period": period,
        "total_worked_seconds": total_seconds,
        "shift_count": count,
        "average_shift_seconds": avg,
    }


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
