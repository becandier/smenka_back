import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.config import get_settings
from src.app.core.logging import get_logger
from src.app.models.shift import Pause, Shift, ShiftStatus

logger = get_logger(__name__)
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


async def _auto_finish_stale_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> None:
    """Inline safety net: auto-finish stale shifts for this user before starting a new one.

    The main cleanup is done by the Celery background task every 5 min.
    This ensures the user is never blocked by their own stale shift.
    """
    from src.app.models.organization_settings import OrganizationSettings

    now = datetime.now(UTC)
    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(
            Shift.user_id == user_id,
            Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
        )
    )
    active_shifts = list(result.scalars().all())

    for shift in active_shifts:
        if shift.organization_id is not None:
            org_result = await session.execute(
                select(OrganizationSettings).where(
                    OrganizationSettings.organization_id == shift.organization_id,
                )
            )
            org_settings = org_result.scalar_one_or_none()
            hours = org_settings.auto_finish_hours if org_settings else settings.default_auto_finish_hours
        else:
            hours = settings.default_auto_finish_hours

        cutoff = now - timedelta(hours=hours)
        if shift.started_at < cutoff:
            for pause in shift.pauses:
                if pause.finished_at is None:
                    pause.finished_at = now
            shift.status = ShiftStatus.finished
            shift.finished_at = now
            logger.info("stale_shift_auto_finished_inline", shift_id=str(shift.id), user_id=str(user_id))

    await session.flush()


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
    await _auto_finish_stale_for_user(session, user_id)

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

    logger.info(
        "shift_started",
        shift_id=str(shift.id),
        user_id=str(user_id),
        org_id=str(organization_id) if organization_id else None,
    )

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

    # Check max pauses for org shifts
    if shift.organization_id is not None:
        from src.app.services.organization_settings import get_settings_for_org

        org_settings = await get_settings_for_org(session, shift.organization_id)
        if org_settings is not None and org_settings.max_pauses_per_shift is not None:
            pause_count = len(shift.pauses)
            if pause_count >= org_settings.max_pauses_per_shift:
                raise ShiftError(
                    "MAX_PAUSES_REACHED",
                    f"Достигнут лимит пауз: {org_settings.max_pauses_per_shift}",
                    400,
                )

    pause = Pause(shift_id=shift.id)
    session.add(pause)
    shift.status = ShiftStatus.paused
    await session.flush()
    session.expire(shift, ["pauses"])

    logger.info("shift_paused", shift_id=str(shift_id), user_id=str(user_id))

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

    logger.info("shift_resumed", shift_id=str(shift_id), user_id=str(user_id))

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

    logger.info("shift_finished", shift_id=str(shift_id), user_id=str(user_id))

    return await _get_shift_with_pauses(session, shift.id, user_id)


async def get_org_stats(
    session: AsyncSession,
    organization_id: uuid.UUID,
    period: str,
) -> dict:
    """Calculate org-wide shift statistics."""
    if period not in VALID_PERIODS:
        raise ShiftError("INVALID_PERIOD", f"Период должен быть: {', '.join(VALID_PERIODS)}", 400)

    now = datetime.now(UTC)
    if period == "day":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(
            Shift.organization_id == organization_id,
            Shift.started_at >= start,
        )
    )
    shifts = list(result.scalars().all())

    total_seconds = sum(calculate_worked_seconds(s) for s in shifts)
    count = len(shifts)
    avg = total_seconds // count if count > 0 else 0

    from collections import defaultdict
    from src.app.models.user import User

    by_user: dict[uuid.UUID, list[Shift]] = defaultdict(list)
    for s in shifts:
        by_user[s.user_id].append(s)

    per_employee = []
    if by_user:
        user_ids = list(by_user.keys())
        users_result = await session.execute(
            select(User).where(User.id.in_(user_ids))
        )
        users_map = {u.id: u for u in users_result.scalars().all()}

        for uid, user_shifts in by_user.items():
            user = users_map.get(uid)
            user_total = sum(calculate_worked_seconds(s) for s in user_shifts)
            user_count = len(user_shifts)
            per_employee.append({
                "user_id": str(uid),
                "user_name": user.name if user else "Unknown",
                "user_email": user.email if user else "",
                "shift_count": user_count,
                "total_worked_seconds": user_total,
                "average_shift_seconds": user_total // user_count if user_count > 0 else 0,
            })

    return {
        "period": period,
        "total_worked_seconds": total_seconds,
        "shift_count": count,
        "average_shift_seconds": avg,
        "per_employee": per_employee,
    }


async def get_org_shifts(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    status: ShiftStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Shift], int]:
    """Get shifts for an organization (admin view)."""
    conditions = [Shift.organization_id == organization_id]

    if user_id is not None:
        conditions.append(Shift.user_id == user_id)
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
