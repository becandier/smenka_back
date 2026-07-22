import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.checklist import ChecklistInstance, ChecklistInstanceStatus
from src.app.models.organization import OrganizationMember
from src.app.models.shift import Pause, Shift, ShiftFinishReason, ShiftStatus
from src.app.models.shift_overtime_request import OvertimeRequestStatus, ShiftOvertimeRequest

logger = get_logger(__name__)


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


def compute_late_seconds(shift: Shift, late_tolerance_minutes: int) -> int | None:
    """R5: опоздание сотрудника относительно планового начала.

    `null` (в API) для смен без графика — здесь это `None`. Вычисляется на
    лету (не хранится): допуск — настройка организации, которая может
    меняться, а хранимое значение немедленно разошлось бы с ней.
    """
    if shift.scheduled_start_at is None:
        return None
    diff_seconds = (shift.started_at - shift.scheduled_start_at).total_seconds()
    return max(0, int(diff_seconds) - late_tolerance_minutes * 60)


async def _get_shift_with_pauses(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Load shift with pauses, verify ownership."""
    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses), selectinload(Shift.work_location))
        .where(
            Shift.id == shift_id,
            Shift.user_id == user_id,
            Shift.is_deleted.is_(False),
        )
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise ShiftError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


def _parse_work_location_id(raw: str) -> uuid.UUID:
    """Распарсить присланный клиентом id точки; невалидный формат → 404 (точки нет)."""
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError, TypeError):
        raise ShiftError("WORK_LOCATION_NOT_FOUND", "Рабочая точка не найдена", 404) from None


async def _require_org_location(
    session: AsyncSession,
    organization_id: uuid.UUID,
    work_location_id: uuid.UUID,
) -> uuid.UUID:
    """Проверить, что точка существует и принадлежит организации; иначе 404."""
    from src.app.models.work_location import WorkLocation

    result = await session.execute(
        select(WorkLocation.id).where(
            WorkLocation.id == work_location_id,
            WorkLocation.organization_id == organization_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ShiftError("WORK_LOCATION_NOT_FOUND", "Рабочая точка не найдена", 404)
    return work_location_id


async def _resolve_org_shift_start(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    latitude: float | None,
    longitude: float | None,
    work_location_id: str | None,
) -> uuid.UUID | None:
    """Проверить членство/гео и определить точку смены по матрице гео×обязательность.

    Возвращает `work_location_id` для сохранения в смене (или `None`).
    - гео вкл: точку определяет сервер (ближайшая из совпавших зон), присланное игнорируется;
    - гео выкл + require: точка обязательна (422 если не передана), валидируется на org;
    - гео выкл + не require: точка опциональна, при наличии — валидируется на org.
    """
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

    org_settings = await get_settings_for_org(session, organization_id)
    geo_enabled = org_settings is not None and org_settings.geo_check_enabled
    require_location = org_settings is not None and org_settings.require_work_location

    # Гео вкл: точка определяется сервером, присланный work_location_id игнорируется.
    if geo_enabled:
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

        from src.app.utils.geo import haversine_distance

        matched = [
            (loc, haversine_distance(latitude, longitude, loc.latitude, loc.longitude))
            for loc in locations
            if haversine_distance(latitude, longitude, loc.latitude, loc.longitude)
            <= loc.radius_meters
        ]
        if not matched:
            raise ShiftError(
                "GEO_CHECK_FAILED",
                "Вы находитесь вне зоны рабочих точек",
                403,
            )
        nearest = min(matched, key=lambda pair: pair[1])[0]
        return nearest.id

    # Гео выкл: точка берётся из выбора сотрудника.
    if work_location_id is not None:
        return await _require_org_location(
            session, organization_id, _parse_work_location_id(work_location_id)
        )
    if require_location:
        raise ShiftError(
            "WORK_LOCATION_REQUIRED",
            "Необходимо выбрать рабочую точку",
            422,
        )
    return None


async def _auto_finish_stale_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> None:
    """Inline safety net: авто-завершение org-смен пользователя, у которых плановое
    окно уже закрылось (R4), перед стартом новой. Та же логика, что и в фоновой
    Celery-задаче (`tasks/shifts.py::auto_finish_stale_shifts`), плюс запись в
    аудит — раньше inline-ветка аудит не писала, это расхождение чинится здесь.

    Персональные смены (`organization_id is None`) больше не авто-завершаются —
    персональный трекер работает только по ручному завершению.
    """
    from src.app.models.audit_log import AuditAction, AuditResource
    from src.app.models.organization_settings import OrganizationSettings
    from src.app.services import audit as audit_service
    from src.app.services.checklist_instance import finalize_shift_checklists

    now = datetime.now(UTC)
    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses))
        .where(
            Shift.user_id == user_id,
            Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
            Shift.is_deleted.is_(False),
            Shift.organization_id.isnot(None),
            Shift.scheduled_end_at.isnot(None),
            Shift.scheduled_end_at <= now,
        )
    )
    active_shifts = list(result.scalars().all())
    if not active_shifts:
        return

    org_ids = {s.organization_id for s in active_shifts if s.organization_id is not None}
    settings_result = await session.execute(
        select(OrganizationSettings).where(OrganizationSettings.organization_id.in_(org_ids))
    )
    settings_map = {s.organization_id: s for s in settings_result.scalars().all()}

    for shift in active_shifts:
        if shift.organization_id is None or shift.scheduled_end_at is None:
            continue  # narrowing для mypy — уже отфильтровано в WHERE, но явно

        org_settings = settings_map.get(shift.organization_id)
        auto_finish_enabled = (
            org_settings.auto_finish_by_schedule if org_settings is not None else True
        )
        if not auto_finish_enabled:
            continue

        has_incomplete = await finalize_shift_checklists(session, shift.id)
        shift.has_incomplete_required_checklists = has_incomplete

        finish_at = shift.scheduled_end_at
        for pause in shift.pauses:
            if pause.finished_at is None:
                pause.finished_at = finish_at
        shift.status = ShiftStatus.finished
        shift.finished_at = finish_at
        shift.finish_reason = ShiftFinishReason.auto_schedule

        await audit_service.record(
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
        logger.info(
            "stale_shift_auto_finished_inline",
            shift_id=str(shift.id),
            user_id=str(user_id),
        )

    await session.flush()


async def _resolve_org_shift_schedule(
    session: AsyncSession,
    organization_id: uuid.UUID,
    member: OrganizationMember,
    work_location_id: uuid.UUID | None,
    requested_schedule_id: str | None,
    started_at: datetime,
) -> tuple[uuid.UUID | None, str | None, datetime | None, datetime | None]:
    """R3: резолв графика при старте org-смены.

    Возвращает `(work_schedule_id, schedule_name, scheduled_start_at, scheduled_end_at)` —
    `(None, None, None, None)`, если смена стартует без графика.
    """
    from src.app.services.organization import get_organization
    from src.app.services.organization_settings import get_settings_for_org
    from src.app.services.work_schedule import (
        WorkScheduleError,
        _get_schedule,
        compute_scheduled_window,
        get_effective_schedules,
    )

    org = await get_organization(session, organization_id)
    org_settings = await get_settings_for_org(session, organization_id)
    require_schedule = org_settings.require_schedule if org_settings is not None else False

    effective = await get_effective_schedules(session, organization_id, member, work_location_id)

    if requested_schedule_id is not None:
        try:
            requested_uuid = uuid.UUID(requested_schedule_id)
        except (ValueError, AttributeError, TypeError):
            raise ShiftError("SCHEDULE_NOT_FOUND", "График не найден", 404) from None

        try:
            await _get_schedule(session, organization_id, requested_uuid)
        except WorkScheduleError as exc:
            raise ShiftError(exc.code, exc.message, exc.status_code) from None

        effective_ids = {s.id for s, _source in effective}
        if requested_uuid not in effective_ids:
            raise ShiftError(
                "SCHEDULE_NOT_AVAILABLE",
                "Этот график недоступен вам на выбранной точке",
                403,
            )
        chosen = next(s for s, _source in effective if s.id == requested_uuid)
    elif len(effective) == 1:
        chosen = effective[0][0]
    else:
        if require_schedule:
            raise ShiftError(
                "SCHEDULE_REQUIRED",
                "Выберите график работы",
                422,
            )
        return None, None, None, None

    start_utc, end_utc = compute_scheduled_window(
        started_at, ZoneInfo(org.timezone), chosen.start_time, chosen.end_time
    )
    return chosen.id, chosen.name, start_utc, end_utc


async def start_shift(
    session: AsyncSession,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    work_location_id: str | None = None,
    work_schedule_id: str | None = None,
) -> Shift:
    """Start a new shift.

    Rules:
    - One active personal shift + one active org shift per org allowed simultaneously.
    - If org has geo_check_enabled, latitude/longitude must be provided and within
      at least one WorkLocation radius; точка определяется сервером (ближайшая зона).
    - Если гео выкл, точка берётся из `work_location_id` (обязательна при включённом
      `require_work_location`). Персональная смена точку не привязывает.
    - Персональная смена (`organization_id is None`) графики не применяет — все
      `scheduled_*` остаются `null` (R3 backend.md).
    """
    await _auto_finish_stale_for_user(session, user_id)

    # Check for existing active shift in the same context
    conditions = [
        Shift.user_id == user_id,
        Shift.status.in_([ShiftStatus.active, ShiftStatus.paused]),
        Shift.is_deleted.is_(False),
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

    # Organization-specific checks + точка смены
    resolved_work_location_id: uuid.UUID | None = None
    member: OrganizationMember | None = None
    if organization_id is not None:
        resolved_work_location_id = await _resolve_org_shift_start(
            session,
            user_id,
            organization_id,
            latitude,
            longitude,
            work_location_id,
        )
        member_result = await session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user_id,
            )
        )
        member = member_result.scalar_one_or_none()

    now = datetime.now(UTC)
    schedule_id: uuid.UUID | None = None
    schedule_name: str | None = None
    scheduled_start_at: datetime | None = None
    scheduled_end_at: datetime | None = None
    if organization_id is not None and member is not None:
        (
            schedule_id,
            schedule_name,
            scheduled_start_at,
            scheduled_end_at,
        ) = await _resolve_org_shift_schedule(
            session,
            organization_id,
            member,
            resolved_work_location_id,
            work_schedule_id,
            now,
        )

    shift = Shift(
        user_id=user_id,
        organization_id=organization_id,
        work_location_id=resolved_work_location_id,
        started_at=now,
        work_schedule_id=schedule_id,
        schedule_name=schedule_name,
        scheduled_start_at=scheduled_start_at,
        scheduled_end_at=scheduled_end_at,
    )
    session.add(shift)
    await session.flush()

    if organization_id is not None and member is not None:
        from src.app.services.checklist_instance import create_instances_for_shift

        await create_instances_for_shift(session, shift, member)

    logger.info(
        "shift_started",
        shift_id=str(shift.id),
        user_id=str(user_id),
        org_id=str(organization_id) if organization_id else None,
        work_schedule_id=str(schedule_id) if schedule_id else None,
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


def ensure_utc(value: datetime) -> datetime:
    """Нормализовать границу окна к UTC (контракт: все даты в UTC).

    Naive datetime трактуется как UTC; aware — приводится к UTC, чтобы SQL-фильтры
    и эхо-поля ответа (`range_from`/`range_to`) были в едином поясе.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def validate_date_range(
    date_from: datetime | None,
    date_to: datetime | None,
) -> None:
    """400 INVALID_DATE_RANGE, если обе границы заданы и date_from > date_to.

    Открытый диапазон (только одна граница) допустим.
    """
    if date_from is None or date_to is None:
        return
    if ensure_utc(date_from) > ensure_utc(date_to):
        raise ShiftError(
            "INVALID_DATE_RANGE",
            "date_from не может быть позже date_to",
            400,
        )


VALID_CHECKLISTS_FILTERS = {"none", "all_completed", "has_incomplete", "required_incomplete"}


def validate_checklists_filter(checklists: str | None) -> None:
    """400 INVALID_CHECKLIST_FILTER для неизвестного значения `checklists` (checklist_reports)."""
    if checklists is not None and checklists not in VALID_CHECKLISTS_FILTERS:
        raise ShiftError(
            "INVALID_CHECKLIST_FILTER",
            "Фильтр должен быть: none, all_completed, has_incomplete, required_incomplete",
            400,
        )


VALID_HAS_OVERTIME_FILTERS = {"pending", "approved", "any"}


def validate_has_overtime_filter(has_overtime: str | None) -> None:
    """400 INVALID_OVERTIME_FILTER для неизвестного значения `has_overtime` (work_schedules)."""
    if has_overtime is not None and has_overtime not in VALID_HAS_OVERTIME_FILTERS:
        raise ShiftError(
            "INVALID_OVERTIME_FILTER",
            "has_overtime должен быть: pending, approved, any",
            400,
        )


def _preset_window_start(period: str, now: datetime) -> datetime:
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    # month
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class StatsWindow:
    """Окно статистики: пресет либо кастомный диапазон.

    `range_from`/`range_to` — фактические границы окна для ответа.
    Фильтр по `started_at` строится из `filter_from`/`filter_to`: для пресета
    верхняя граница не применяется (поведение пресетов не меняется), для
    кастомного диапазона обе границы включительны.
    """

    period: str | None
    range_from: datetime | None
    range_to: datetime | None
    filter_from: datetime | None
    filter_to: datetime | None


def resolve_stats_window(
    period: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
) -> StatsWindow:
    """Выбрать ровно один источник окна stats: пресет ЛИБО кастомный диапазон.

    Порядок валидации: MISSING_STATS_RANGE → AMBIGUOUS_STATS_RANGE →
    INVALID_PERIOD → INVALID_DATE_RANGE.
    """
    has_range = date_from is not None or date_to is not None
    if period is None and not has_range:
        raise ShiftError(
            "MISSING_STATS_RANGE",
            "Укажите period либо date_from/date_to",
            400,
        )
    if period is not None and has_range:
        raise ShiftError(
            "AMBIGUOUS_STATS_RANGE",
            "period и date_from/date_to взаимоисключающи",
            400,
        )
    if period is not None:
        if period not in VALID_PERIODS:
            raise ShiftError(
                "INVALID_PERIOD",
                f"Период должен быть: {', '.join(VALID_PERIODS)}",
                400,
            )
        now = datetime.now(UTC)
        start = _preset_window_start(period, now)
        return StatsWindow(
            period=period,
            range_from=start,
            range_to=now,
            filter_from=start,
            filter_to=None,
        )

    validate_date_range(date_from, date_to)
    norm_from = ensure_utc(date_from) if date_from is not None else None
    norm_to = ensure_utc(date_to) if date_to is not None else None
    return StatsWindow(
        period=None,
        range_from=norm_from,
        range_to=norm_to,
        filter_from=norm_from,
        filter_to=norm_to,
    )


_SHIFT_SORT_COLUMNS = {
    "started_at": Shift.started_at,
    "finished_at": Shift.finished_at,
}


def _shift_order_by(sort: str, order: str) -> Any:
    """Build the ORDER BY clause for shift lists (default: started_at desc)."""
    column = _SHIFT_SORT_COLUMNS.get(sort, Shift.started_at)
    return column.asc() if order.lower() == "asc" else column.desc()


async def get_shifts(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: ShiftStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "started_at",
    order: str = "desc",
) -> tuple[list[Shift], int]:
    """Get paginated shift list with optional filters. Returns (shifts, total_count)."""
    conditions = [Shift.user_id == user_id, Shift.is_deleted.is_(False)]

    if status is not None:
        conditions.append(Shift.status == status)
    if date_from is not None:
        conditions.append(Shift.started_at >= ensure_utc(date_from))
    if date_to is not None:
        conditions.append(Shift.started_at <= ensure_utc(date_to))

    count_query = select(func.count()).select_from(Shift).where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    query = (
        select(Shift)
        .options(selectinload(Shift.pauses), selectinload(Shift.work_location))
        .where(*conditions)
        .order_by(_shift_order_by(sort, order))
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(query)
    shifts = list(result.scalars().all())

    return shifts, total


async def get_shift_stats(
    session: AsyncSession,
    user_id: uuid.UUID,
    period: str | None = None,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """Статистика смен за пресет (day/week/month) либо кастомный диапазон."""
    window = resolve_stats_window(period, date_from, date_to)

    conditions = [Shift.user_id == user_id, Shift.is_deleted.is_(False)]
    if window.filter_from is not None:
        conditions.append(Shift.started_at >= window.filter_from)
    if window.filter_to is not None:
        conditions.append(Shift.started_at <= window.filter_to)

    result = await session.execute(
        select(Shift).options(selectinload(Shift.pauses)).where(*conditions)
    )
    shifts = list(result.scalars().all())

    total_seconds = sum(calculate_worked_seconds(s) for s in shifts)
    count = len(shifts)
    avg = total_seconds // count if count > 0 else 0

    return {
        "period": window.period,
        "total_worked_seconds": total_seconds,
        "shift_count": count,
        "average_shift_seconds": avg,
        "range_from": window.range_from,
        "range_to": window.range_to,
    }


async def finish_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> Shift:
    """Finish an active or paused shift."""
    from src.app.services.checklist_instance import finalize_shift_checklists

    shift = await _get_shift_with_pauses(session, shift_id, user_id)

    if shift.status == ShiftStatus.finished:
        raise ShiftError("SHIFT_ALREADY_FINISHED", "Смена уже завершена", 400)

    has_incomplete = await finalize_shift_checklists(session, shift.id)
    shift.has_incomplete_required_checklists = has_incomplete

    for pause in shift.pauses:
        if pause.finished_at is None:
            pause.finished_at = datetime.now(UTC)

    shift.status = ShiftStatus.finished
    shift.finished_at = datetime.now(UTC)
    shift.finish_reason = ShiftFinishReason.manual
    await session.flush()

    logger.info("shift_finished", shift_id=str(shift_id), user_id=str(user_id))

    return await _get_shift_with_pauses(session, shift.id, user_id)


async def get_org_stats(
    session: AsyncSession,
    organization_id: uuid.UUID,
    period: str | None = None,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """Орг-статистика за пресет (day/week/month) либо кастомный диапазон."""
    window = resolve_stats_window(period, date_from, date_to)

    conditions = [Shift.organization_id == organization_id, Shift.is_deleted.is_(False)]
    if window.filter_from is not None:
        conditions.append(Shift.started_at >= window.filter_from)
    if window.filter_to is not None:
        conditions.append(Shift.started_at <= window.filter_to)

    result = await session.execute(
        select(Shift).options(selectinload(Shift.pauses)).where(*conditions)
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
        from src.app.models.organization import OrganizationMember

        user_ids = list(by_user.keys())
        users_result = await session.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}

        # display_name — тем же batch-запросом по org_id, без N+1 (member_display_name).
        members_result = await session.execute(
            select(OrganizationMember.user_id, OrganizationMember.display_name).where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id.in_(user_ids),
            )
        )
        display_name_map = dict(members_result.tuples().all())

        for uid, user_shifts in by_user.items():
            user = users_map.get(uid)
            user_total = sum(calculate_worked_seconds(s) for s in user_shifts)
            user_count = len(user_shifts)
            per_employee.append(
                {
                    "user_id": str(uid),
                    "user_name": user.name if user else "Unknown",
                    "user_email": user.email if user else "",
                    "display_name": display_name_map.get(uid),
                    "shift_count": user_count,
                    "total_worked_seconds": user_total,
                    "average_shift_seconds": user_total // user_count if user_count > 0 else 0,
                }
            )

    return {
        "period": window.period,
        "total_worked_seconds": total_seconds,
        "shift_count": count,
        "average_shift_seconds": avg,
        "per_employee": per_employee,
        "range_from": window.range_from,
        "range_to": window.range_to,
    }


def _checklists_summary_subquery() -> Any:
    """shift_id -> (total, completed, required_incomplete) по её экземплярам чек-листов.

    Используется ТОЛЬКО для фильтра `checklists` в списке орг-смен (checklist_reports).
    Отдельно от `checklist_instance.get_checklists_summary_for_shifts` — та функция
    строит сводку ДЛЯ УЖЕ выбранной страницы, а этот подзапрос должен участвовать в
    WHERE/COUNT ДО пагинации.
    """
    completed_case = case(
        (ChecklistInstance.status == ChecklistInstanceStatus.completed, 1), else_=0
    )
    required_incomplete_case = case(
        (
            and_(
                ChecklistInstance.is_required.is_(True),
                ChecklistInstance.status != ChecklistInstanceStatus.completed,
            ),
            1,
        ),
        else_=0,
    )
    return (
        select(
            ChecklistInstance.shift_id.label("shift_id"),
            func.count().label("total"),
            func.sum(completed_case).label("completed"),
            func.sum(required_incomplete_case).label("required_incomplete"),
        )
        .group_by(ChecklistInstance.shift_id)
        .subquery()
    )


def _checklists_filter_condition(checklists: str, summary_subq: Any) -> Any:
    if checklists == "none":
        return summary_subq.c.total.is_(None)
    if checklists == "all_completed":
        return and_(
            summary_subq.c.total.isnot(None),
            summary_subq.c.completed == summary_subq.c.total,
        )
    if checklists == "has_incomplete":
        return and_(
            summary_subq.c.total.isnot(None),
            summary_subq.c.completed < summary_subq.c.total,
        )
    # required_incomplete
    return summary_subq.c.required_incomplete > 0


def _has_overtime_condition(value: str) -> Any:
    """EXISTS-условие по состоянию заявки на переработку смены (work_schedules).

    `any` — есть заявка в любом статусе; `pending`/`approved` — есть заявка
    именно в этом статусе."""
    base = (
        select(ShiftOvertimeRequest.id)
        .where(ShiftOvertimeRequest.shift_id == Shift.id)
        .correlate(Shift)
    )
    if value == "any":
        return base.exists()
    return base.where(ShiftOvertimeRequest.status == OvertimeRequestStatus(value)).exists()


async def get_org_shifts(
    session: AsyncSession,
    organization_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    status: ShiftStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    checklists: str | None = None,
    only_late: bool | None = None,
    late_tolerance_minutes: int = 0,
    work_schedule_id: uuid.UUID | None = None,
    has_overtime: str | None = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "started_at",
    order: str = "desc",
) -> tuple[list[Shift], int]:
    """Get shifts for an organization (admin view).

    `checklists` (checklist_reports) — фильтр по состоянию чек-листов смены, считается
    на лету по `checklist_instances` (none/all_completed/has_incomplete/required_incomplete);
    комбинируется с остальными фильтрами по И, пагинация/`total` учитывают его.
    `only_late`/`work_schedule_id`/`has_overtime` — фильтры work_schedules (R5/R6),
    также по И с остальными.
    """
    conditions = [Shift.organization_id == organization_id, Shift.is_deleted.is_(False)]

    if user_id is not None:
        conditions.append(Shift.user_id == user_id)
    if status is not None:
        conditions.append(Shift.status == status)
    if date_from is not None:
        conditions.append(Shift.started_at >= ensure_utc(date_from))
    if date_to is not None:
        conditions.append(Shift.started_at <= ensure_utc(date_to))
    if work_schedule_id is not None:
        conditions.append(Shift.work_schedule_id == work_schedule_id)
    if only_late:
        late_seconds_expr = (
            func.extract("epoch", Shift.started_at - Shift.scheduled_start_at)
            - late_tolerance_minutes * 60
        )
        conditions.append(Shift.scheduled_start_at.isnot(None))
        conditions.append(late_seconds_expr > 0)
    if has_overtime is not None:
        conditions.append(_has_overtime_condition(has_overtime))

    count_query = select(func.count()).select_from(Shift).where(*conditions)
    query = (
        select(Shift)
        .options(selectinload(Shift.pauses), selectinload(Shift.work_location))
        .where(*conditions)
        .order_by(_shift_order_by(sort, order))
        .limit(limit)
        .offset(offset)
    )

    if checklists is not None:
        summary_subq = _checklists_summary_subquery()
        condition = _checklists_filter_condition(checklists, summary_subq)
        count_query = count_query.outerjoin(
            summary_subq, summary_subq.c.shift_id == Shift.id
        ).where(condition)
        query = query.outerjoin(summary_subq, summary_subq.c.shift_id == Shift.id).where(condition)

    total = (await session.execute(count_query)).scalar_one()
    result = await session.execute(query)
    shifts = list(result.scalars().all())

    return shifts, total


async def get_org_shift_detail(
    session: AsyncSession,
    organization_id: uuid.UUID,
    shift_id: uuid.UUID,
) -> Shift:
    """Load a single org shift (with pauses) for owner/admin review.

    Возвращает только смену, принадлежащую данной организации. Любая другая
    смена (персональная `organization_id=null` или смена другой org) трактуется
    как отсутствующая — `SHIFT_NOT_FOUND`, чтобы не раскрывать существование
    чужих/персональных смен.
    """
    result = await session.execute(
        select(Shift)
        .options(selectinload(Shift.pauses), selectinload(Shift.work_location))
        .where(
            Shift.id == shift_id,
            Shift.organization_id == organization_id,
            Shift.is_deleted.is_(False),
        )
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise ShiftError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


@dataclass(frozen=True)
class ShiftIdentity:
    """Идентификация сотрудника для орг-обогащения ShiftResponse.

    Вычисляется на чтении: имя/почта из `users`, display_name/роль/кастомная
    роль из `organization_members`. В `shifts` ничего не денормализуется.
    """

    user_name: str | None
    user_email: str | None
    display_name: str | None
    role: str | None
    custom_role_name: str | None


async def build_org_shift_identities(
    session: AsyncSession,
    organization_id: uuid.UUID,
    shifts: list[Shift],
) -> dict[uuid.UUID, ShiftIdentity]:
    """Map user_id → ShiftIdentity для страницы орг-смен, без N+1.

    Два batch-запроса вне зависимости от размера страницы:
    - `users` (имя/почта) по множеству user_id страницы;
    - `organization_members` (системная роль + eager `custom_role`) по тому же
      множеству в пределах организации.

    Если сотрудник исключён из org (записи `OrganizationMember` нет) — имя/почта
    всё равно отдаются из `users`, а `display_name`/`role`/`custom_role_name`
    будут `null`.
    """
    from src.app.models.organization import OrganizationMember
    from src.app.models.user import User

    user_ids = {s.user_id for s in shifts}
    if not user_ids:
        return {}

    users_result = await session.execute(
        select(User.id, User.name, User.email).where(User.id.in_(user_ids))
    )
    users_map = {uid: (name, email) for uid, name, email in users_result.all()}

    members_result = await session.execute(
        select(OrganizationMember)
        .options(selectinload(OrganizationMember.custom_role))
        .where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id.in_(user_ids),
        )
    )
    members_map = {m.user_id: m for m in members_result.scalars().all()}

    identities: dict[uuid.UUID, ShiftIdentity] = {}
    for uid in user_ids:
        name, email = users_map.get(uid, (None, None))
        member = members_map.get(uid)
        role = member.role.value if member is not None else None
        custom_role_name = (
            member.custom_role.name
            if member is not None and member.custom_role is not None
            else None
        )
        identities[uid] = ShiftIdentity(
            user_name=name,
            user_email=email,
            display_name=member.display_name if member is not None else None,
            role=role,
            custom_role_name=custom_role_name,
        )
    return identities
