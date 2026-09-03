"""Графики работы (work_schedules): CRUD, назначения, резолв эффективного
набора (R1) и расчёт планового окна (R2) — backend.md фичи `work_schedules`.

Модель назначения — калька `services/checklist_assignment.py` +
`services/checklist_location.py`, с одним осознанным отличием: график без
единой привязки (ни роли, ни точки) действует на ВСЕХ сотрудников
организации (у чек-листов такой шаблон не выдаётся никому).
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, selectinload

from src.app.core.logging import get_logger
from src.app.models.organization import Organization, OrganizationMember
from src.app.models.organization_role import OrganizationRole
from src.app.models.work_location import WorkLocation
from src.app.models.work_schedule import (
    ScheduleOverrideType,
    WorkSchedule,
    WorkScheduleLocation,
    WorkScheduleMemberOverride,
    WorkScheduleRole,
)
from src.app.services import entitlements
from src.app.services.checklist_location import _get_org_location, matches_location
from src.app.services.common import ensure_admin_or_owner, ensure_member
from src.app.services.organization import get_organization

if TYPE_CHECKING:
    from src.app.models.shift import Shift

logger = get_logger(__name__)


class WorkScheduleError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


async def _check_admin_or_owner(
    session: AsyncSession,
    org: Organization,
    user_id: uuid.UUID,
) -> None:
    await ensure_admin_or_owner(
        session,
        org,
        user_id,
        message="Нет прав для управления графиками работы",
    )


async def _get_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
) -> OrganizationMember:
    result = await session.execute(
        select(OrganizationMember)
        .options(selectinload(OrganizationMember.user))
        .where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise WorkScheduleError("MEMBER_NOT_FOUND", "Участник не найден", 404)
    return member


async def _get_schedule(
    session: AsyncSession,
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
) -> WorkSchedule:
    """Любой график организации (включая архивный — нужно R7)."""
    result = await session.execute(
        select(WorkSchedule).where(
            WorkSchedule.id == schedule_id,
            WorkSchedule.organization_id == org_id,
        )
    )
    schedule = result.scalar_one_or_none()
    if schedule is None:
        raise WorkScheduleError("SCHEDULE_NOT_FOUND", "График не найден", 404)
    return schedule


async def _ensure_ids_belong_to_org(
    session: AsyncSession,
    *,
    id_column: InstrumentedAttribute[uuid.UUID],
    org_column: InstrumentedAttribute[uuid.UUID],
    org_id: uuid.UUID,
    ids: list[uuid.UUID],
    error_code: str,
    error_message: str,
) -> None:
    if not ids:
        return
    result = await session.execute(
        select(id_column).where(id_column.in_(ids), org_column == org_id)
    )
    valid_ids = {row[0] for row in result.all()}
    if valid_ids != set(ids):
        raise WorkScheduleError(error_code, error_message, 404)


async def _replace_links[T](
    session: AsyncSession,
    existing_rows: Sequence[T],
    *,
    key_of: Callable[[T], uuid.UUID],
    target_ids: set[uuid.UUID],
    make_new: Callable[[uuid.UUID], T],
) -> None:
    existing = {key_of(row): row for row in existing_rows}
    current = set(existing.keys())
    for id_ in current - target_ids:
        await session.delete(existing[id_])
    for id_ in target_ids - current:
        session.add(make_new(id_))
    await session.flush()


# --- CRUD графиков -----------------------------------------------------------


async def create_schedule(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    name: str,
    start_time: time,
    end_time: time,
) -> WorkSchedule:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)

    if start_time == end_time:
        raise WorkScheduleError(
            "SCHEDULE_INVALID_TIME",
            "Начало и конец графика не могут совпадать",
            400,
        )

    schedule = WorkSchedule(
        organization_id=org_id,
        name=name,
        start_time=start_time,
        end_time=end_time,
    )
    session.add(schedule)
    await session.flush()
    logger.info("work_schedule_created", org_id=str(org_id), schedule_id=str(schedule.id))
    return schedule


async def get_role_ids_for_schedules(
    session: AsyncSession,
    schedule_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not schedule_ids:
        return {}
    result = await session.execute(
        select(WorkScheduleRole.schedule_id, WorkScheduleRole.role_id).where(
            WorkScheduleRole.schedule_id.in_(schedule_ids)
        )
    )
    mapping: dict[uuid.UUID, list[uuid.UUID]] = {}
    for sid, rid in result.all():
        mapping.setdefault(sid, []).append(rid)
    return mapping


async def get_location_ids_for_schedules(
    session: AsyncSession,
    schedule_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not schedule_ids:
        return {}
    result = await session.execute(
        select(
            WorkScheduleLocation.schedule_id,
            WorkScheduleLocation.work_location_id,
        ).where(WorkScheduleLocation.schedule_id.in_(schedule_ids))
    )
    mapping: dict[uuid.UUID, list[uuid.UUID]] = {}
    for sid, lid in result.all():
        mapping.setdefault(sid, []).append(lid)
    return mapping


async def list_schedules(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    include_paused: bool = False,
) -> list[tuple[WorkSchedule, list[uuid.UUID], list[uuid.UUID]]]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)

    query = select(WorkSchedule).where(WorkSchedule.organization_id == org_id)
    if not include_paused:
        query = query.where(WorkSchedule.is_paused.is_(False))
    query = query.order_by(WorkSchedule.created_at)

    schedules = list((await session.execute(query)).scalars().all())
    if not schedules:
        return []

    ids = [s.id for s in schedules]
    role_map = await get_role_ids_for_schedules(session, ids)
    loc_map = await get_location_ids_for_schedules(session, ids)
    return [(s, role_map.get(s.id, []), loc_map.get(s.id, [])) for s in schedules]


async def get_schedule_detail(
    session: AsyncSession,
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> tuple[WorkSchedule, list[uuid.UUID], list[uuid.UUID]]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    schedule = await _get_schedule(session, org_id, schedule_id)

    role_ids = (await get_role_ids_for_schedules(session, [schedule.id])).get(schedule.id, [])
    location_ids = (await get_location_ids_for_schedules(session, [schedule.id])).get(
        schedule.id, []
    )
    return schedule, role_ids, location_ids


async def update_schedule(
    session: AsyncSession,
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    name: str | None = None,
    start_time: time | None = None,
    end_time: time | None = None,
    is_paused: bool | None = None,
) -> WorkSchedule:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    schedule = await _get_schedule(session, org_id, schedule_id)

    new_start = start_time if start_time is not None else schedule.start_time
    new_end = end_time if end_time is not None else schedule.end_time
    if new_start == new_end:
        raise WorkScheduleError(
            "SCHEDULE_INVALID_TIME",
            "Начало и конец графика не могут совпадать",
            400,
        )

    if name is not None:
        schedule.name = name
    schedule.start_time = new_start
    schedule.end_time = new_end
    if is_paused is not None:
        schedule.is_paused = is_paused

    await session.flush()
    logger.info("work_schedule_updated", org_id=str(org_id), schedule_id=str(schedule_id))
    return schedule


async def delete_schedule(
    session: AsyncSession,
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Физическое удаление. У смен, где график уже использован, `work_schedule_id`
    становится NULL (FK SET NULL), но `schedule_name`/`scheduled_*` — снимок,
    история не рушится. Привязки удаляются каскадом."""
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    schedule = await _get_schedule(session, org_id, schedule_id)

    await session.delete(schedule)
    await session.flush()
    logger.info("work_schedule_deleted", org_id=str(org_id), schedule_id=str(schedule_id))


# --- Назначения ----------------------------------------------------------------


async def set_schedule_roles(
    session: AsyncSession,
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    role_ids: list[uuid.UUID],
    requester_id: uuid.UUID,
) -> list[uuid.UUID]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    await _get_schedule(session, org_id, schedule_id)

    await _ensure_ids_belong_to_org(
        session,
        id_column=OrganizationRole.id,
        org_column=OrganizationRole.organization_id,
        org_id=org_id,
        ids=role_ids,
        error_code="ROLE_NOT_FOUND",
        error_message="Одна или несколько ролей не принадлежат организации",
    )

    existing_result = await session.execute(
        select(WorkScheduleRole).where(WorkScheduleRole.schedule_id == schedule_id)
    )
    target = set(role_ids)
    await _replace_links(
        session,
        existing_result.scalars().all(),
        key_of=lambda r: r.role_id,
        target_ids=target,
        make_new=lambda rid: WorkScheduleRole(schedule_id=schedule_id, role_id=rid),
    )
    logger.info(
        "work_schedule_roles_assigned",
        schedule_id=str(schedule_id),
        role_count=len(target),
    )
    return list(target)


async def set_schedule_locations(
    session: AsyncSession,
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    location_ids: list[uuid.UUID],
    requester_id: uuid.UUID,
) -> list[uuid.UUID]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    await _get_schedule(session, org_id, schedule_id)

    await _ensure_ids_belong_to_org(
        session,
        id_column=WorkLocation.id,
        org_column=WorkLocation.organization_id,
        org_id=org_id,
        ids=location_ids,
        error_code="WORK_LOCATION_NOT_FOUND",
        error_message="Одна или несколько точек не принадлежат организации",
    )

    existing_result = await session.execute(
        select(WorkScheduleLocation).where(WorkScheduleLocation.schedule_id == schedule_id)
    )
    target = set(location_ids)
    await _replace_links(
        session,
        existing_result.scalars().all(),
        key_of=lambda loc: loc.work_location_id,
        target_ids=target,
        make_new=lambda lid: WorkScheduleLocation(schedule_id=schedule_id, work_location_id=lid),
    )
    logger.info(
        "work_schedule_locations_assigned",
        schedule_id=str(schedule_id),
        location_count=len(target),
    )
    return list(target)


async def get_schedule_assignments(
    session: AsyncSession,
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> tuple[list[uuid.UUID], list[OrganizationMember], list[OrganizationMember], list[uuid.UUID]]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await _get_schedule(session, org_id, schedule_id)

    role_ids = (await get_role_ids_for_schedules(session, [schedule_id])).get(schedule_id, [])
    location_ids = (await get_location_ids_for_schedules(session, [schedule_id])).get(
        schedule_id, []
    )

    overrides_result = await session.execute(
        select(WorkScheduleMemberOverride).where(
            WorkScheduleMemberOverride.schedule_id == schedule_id
        )
    )
    overrides = list(overrides_result.scalars().all())

    by_id: dict[uuid.UUID, OrganizationMember] = {}
    if overrides:
        member_ids = [o.member_id for o in overrides]
        members_result = await session.execute(
            select(OrganizationMember)
            .options(selectinload(OrganizationMember.user))
            .where(OrganizationMember.id.in_(member_ids))
        )
        by_id = {m.id: m for m in members_result.scalars().all()}

    personal_add: list[OrganizationMember] = []
    personal_remove: list[OrganizationMember] = []
    for o in overrides:
        member = by_id.get(o.member_id)
        if member is None:
            continue
        if o.override_type == ScheduleOverrideType.add:
            personal_add.append(member)
        else:
            personal_remove.append(member)

    return role_ids, personal_add, personal_remove, location_ids


async def set_member_schedule_overrides(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    overrides: list[tuple[uuid.UUID, str]],
    requester_id: uuid.UUID,
) -> list[tuple[uuid.UUID, ScheduleOverrideType]]:
    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    member = await _get_member(session, org_id, user_id)

    parsed: list[tuple[uuid.UUID, ScheduleOverrideType]] = []
    seen: set[uuid.UUID] = set()
    for schedule_id, raw_type in overrides:
        if schedule_id in seen:
            raise WorkScheduleError(
                "DUPLICATE_SCHEDULE",
                "Каждый график может встречаться только один раз",
                400,
            )
        seen.add(schedule_id)
        try:
            override_type = ScheduleOverrideType(raw_type)
        except ValueError:
            raise WorkScheduleError(
                "INVALID_OVERRIDE_TYPE",
                "type должен быть add или remove",
                400,
            ) from None
        parsed.append((schedule_id, override_type))

    await _ensure_ids_belong_to_org(
        session,
        id_column=WorkSchedule.id,
        org_column=WorkSchedule.organization_id,
        org_id=org_id,
        ids=[p[0] for p in parsed],
        error_code="SCHEDULE_NOT_FOUND",
        error_message="Один или несколько графиков не принадлежат организации",
    )

    existing_result = await session.execute(
        select(WorkScheduleMemberOverride).where(
            WorkScheduleMemberOverride.member_id == member.id,
        )
    )
    existing = {o.schedule_id: o for o in existing_result.scalars().all()}
    target = dict(parsed)

    for schedule_id in set(existing.keys()) - set(target.keys()):
        await session.delete(existing[schedule_id])

    for schedule_id, override_type in target.items():
        if schedule_id in existing:
            existing[schedule_id].override_type = override_type
        else:
            session.add(
                WorkScheduleMemberOverride(
                    schedule_id=schedule_id,
                    member_id=member.id,
                    override_type=override_type,
                )
            )

    await session.flush()
    logger.info(
        "work_schedule_member_overrides_set",
        member_id=str(member.id),
        count=len(target),
    )
    return parsed


# --- R1: резолв эффективного набора графиков ------------------------------------


async def _compute_effective_schedules(
    session: AsyncSession,
    org_id: uuid.UUID,
    member: OrganizationMember,
) -> list[tuple[WorkSchedule, str]]:
    """Без учёта фильтра по точке. `source` — global | location | role | personal_add."""
    role_schedule_ids: set[uuid.UUID] = set()
    if member.role_id is not None:
        role_result = await session.execute(
            select(WorkScheduleRole.schedule_id)
            .join(WorkSchedule, WorkSchedule.id == WorkScheduleRole.schedule_id)
            .where(
                WorkScheduleRole.role_id == member.role_id,
                WorkSchedule.organization_id == org_id,
            )
        )
        role_schedule_ids = {row[0] for row in role_result.all()}

    # Графики без роли: делим на "глобальные" (нет и точек) и "по точке" (R1) —
    # отличие от чек-листов: глобальные действуют на всех, а не ни на кого.
    has_location = (
        select(WorkScheduleLocation.id)
        .where(WorkScheduleLocation.schedule_id == WorkSchedule.id)
        .correlate(WorkSchedule)
        .exists()
    )
    has_role = (
        select(WorkScheduleRole.id)
        .where(WorkScheduleRole.schedule_id == WorkSchedule.id)
        .correlate(WorkSchedule)
        .exists()
    )
    no_role_result = await session.execute(
        select(WorkSchedule.id, has_location.label("has_location")).where(
            WorkSchedule.organization_id == org_id,
            WorkSchedule.is_paused.is_(False),
            ~has_role,
        )
    )
    global_ids: set[uuid.UUID] = set()
    location_only_ids: set[uuid.UUID] = set()
    for schedule_id, has_loc in no_role_result.all():
        (location_only_ids if has_loc else global_ids).add(schedule_id)

    candidate_ids = global_ids | location_only_ids | role_schedule_ids

    overrides_result = await session.execute(
        select(WorkScheduleMemberOverride).where(
            WorkScheduleMemberOverride.member_id == member.id,
        )
    )
    overrides = list(overrides_result.scalars().all())
    add_ids = {o.schedule_id for o in overrides if o.override_type == ScheduleOverrideType.add}
    remove_ids = {
        o.schedule_id for o in overrides if o.override_type == ScheduleOverrideType.remove
    }

    effective_candidate_ids = candidate_ids - remove_ids
    all_ids = effective_candidate_ids | add_ids
    if not all_ids:
        return []

    schedules_result = await session.execute(
        select(WorkSchedule).where(
            WorkSchedule.id.in_(all_ids),
            WorkSchedule.organization_id == org_id,
            WorkSchedule.is_paused.is_(False),
        )
    )
    schedules = {s.id: s for s in schedules_result.scalars().all()}

    def _source(schedule_id: uuid.UUID) -> str:
        if schedule_id in global_ids:
            return "global"
        if schedule_id in location_only_ids:
            return "location"
        return "role"

    result: list[tuple[WorkSchedule, str]] = [
        (schedules[sid], _source(sid)) for sid in effective_candidate_ids if sid in schedules
    ]
    result.extend(
        (schedules[sid], "personal_add")
        for sid in add_ids
        if sid in schedules and sid not in effective_candidate_ids
    )
    result.sort(key=lambda pair: pair[0].created_at)
    return result


async def get_effective_schedules(
    session: AsyncSession,
    org_id: uuid.UUID,
    member: OrganizationMember,
    work_location_id: uuid.UUID | None,
) -> list[tuple[WorkSchedule, str]]:
    """R1 полностью: резолв кандидатов + фильтр по точке (после, ко всем источникам)."""
    pairs = await _compute_effective_schedules(session, org_id, member)
    if not pairs:
        return []
    location_ids_by_schedule = await get_location_ids_for_schedules(
        session, [s.id for s, _ in pairs]
    )
    return [
        (s, source)
        for s, source in pairs
        if matches_location(location_ids_by_schedule.get(s.id), work_location_id)
    ]


async def get_member_effective_schedules(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    work_location_id: uuid.UUID | None,
) -> list[tuple[WorkSchedule, str]]:
    """`GET .../members/{user_id}/schedules` — только owner/admin."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(
        session,
        org,
        requester_id,
        message="Нет прав для просмотра графиков сотрудника",
    )
    if work_location_id is not None:
        await _get_org_location(session, org_id, work_location_id)
    member = await _get_member(session, org_id, user_id)
    return await get_effective_schedules(session, org_id, member, work_location_id)


# --- R2: расчёт планового окна ---------------------------------------------------


def _time_to_timedelta(value: time) -> timedelta:
    return timedelta(
        hours=value.hour,
        minutes=value.minute,
        seconds=value.second,
        microseconds=value.microsecond,
    )


def schedule_duration(start_time: time, end_time: time) -> timedelta:
    start_td = _time_to_timedelta(start_time)
    end_td = _time_to_timedelta(end_time)
    if end_td > start_td:
        return end_td - start_td
    return timedelta(hours=24) - (start_td - end_td)


def compute_scheduled_window(
    started_at: datetime,
    tz: ZoneInfo,
    start_time: time,
    end_time: time,
) -> tuple[datetime, datetime]:
    """R2: плановое окно графика для `started_at` (UTC на входе и выходе).

    Кандидаты строятся над наивным локальным временем (`datetime.combine(...,
    tzinfo=tz)`), длительность прибавляется ДО перевода в UTC — так корректно
    учитывается переход на летнее/зимнее время: `ZoneInfo` пересчитывает
    смещение по итоговому wall-clock времени при `.astimezone(UTC)`, а не по
    смещению на момент старта прибавления.

    Выбор окна — сначала то, что СОДЕРЖИТ `started_at` (`start <= started_at
    <= end`): для длинных графиков (>12ч от начала) ближайшее по модулю
    расстояние до начала окна может указывать на окно СЛЕДУЮЩИХ суток, хотя
    прямо сейчас идёт текущее. Если содержащего окна нет (ранний приход до
    начала или окно уже закрылось) — ближайшее будущее окно, как раньше.
    """
    duration = schedule_duration(start_time, end_time)
    local_date = started_at.astimezone(tz).date()

    def _candidate(day_offset: int) -> tuple[datetime, datetime]:
        candidate_date = local_date + timedelta(days=day_offset)
        start_local = datetime.combine(candidate_date, start_time, tzinfo=tz)
        end_local = start_local + duration
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    candidates = [_candidate(offset) for offset in (-1, 0, 1)]

    containing = [pair for pair in candidates if pair[0] <= started_at <= pair[1]]
    if containing:
        # При наложении (график ровно 24ч) — то, что стартовало последним.
        return max(containing, key=lambda pair: pair[0])

    valid = [pair for pair in candidates if pair[1] > started_at]
    if not valid:
        valid = [_candidate(2)]

    return min(valid, key=lambda pair: abs((started_at - pair[0]).total_seconds()))


# --- S1: «график стартуем сейчас» (schedule_window_enforcement/backend.md) ------


def is_schedule_startable(
    now: datetime,
    next_start_at: datetime,
    early_start_minutes: int,
) -> bool:
    """S1 — единственный источник истины для допуска к старту смены: используется
    и при старте (S2, `services/shift.py::_resolve_org_shift_schedule`), и в
    `my-schedules` (S3, `can_start_now` ниже).

    Верхняя граница (`now <= next_end_at`) выполняется по построению R2: если
    существует окно, содержащее `now`/`started_at` (`start <= now <= end`),
    R2 вернёт именно его — иначе ближайшее окно из тех, чей конец ещё не
    наступил. В обоих случаях конец возвращённого окна не может быть раньше
    того момента, для которого его считали.
    """
    return now >= next_start_at - timedelta(minutes=early_start_minutes)


# --- my-schedules (мобилка): эффективный набор + плановое окно "прямо сейчас" ---


@dataclass(frozen=True)
class MyScheduleItem:
    schedule: WorkSchedule
    next_start_at: datetime
    next_end_at: datetime
    is_current: bool
    starts_in_minutes: int
    can_start_now: bool


@dataclass(frozen=True)
class MySchedulesResult:
    """Результат `get_my_schedules`: эффективный набор графиков + резолв точки.

    `resolved_work_location` заполнен, когда точка определена — явно
    переданным `work_location_id` либо резолвом по `lat`/`lng` (см. R1
    `work_schedules_geo_resolve/backend.md`). `None`, если точка не
    определена (в т.ч. когда координаты не попали ни в одну зону — это НЕ
    ошибка здесь, в отличие от старта смены).

    `early_start_minutes` дублирует настройку организации (`schedule_window_
    enforcement/backend.md`) — мобилка пересчитывает `can_start_now` локально
    по таймеру, не дёргая сервер.
    """

    items: list[MyScheduleItem]
    require_schedule: bool
    early_start_minutes: int
    resolved_work_location: WorkLocation | None


def _build_my_schedule_items(
    pairs: list[tuple[WorkSchedule, str]],
    tz: ZoneInfo,
    moment: datetime,
    early_start_minutes: int,
) -> list[MyScheduleItem]:
    items: list[MyScheduleItem] = []
    for schedule, _source in pairs:
        start_utc, end_utc = compute_scheduled_window(
            moment, tz, schedule.start_time, schedule.end_time
        )
        is_current = start_utc <= moment <= end_utc
        starts_in_minutes = round((start_utc - moment).total_seconds() / 60)
        can_start_now = is_schedule_startable(moment, start_utc, early_start_minutes)
        items.append(
            MyScheduleItem(
                schedule=schedule,
                next_start_at=start_utc,
                next_end_at=end_utc,
                is_current=is_current,
                starts_in_minutes=starts_in_minutes,
                can_start_now=can_start_now,
            )
        )
    # S3: стартуемые сейчас — первыми, затем текущие по окну, затем ближайшие по времени.
    items.sort(key=lambda it: (not it.can_start_now, not it.is_current, abs(it.starts_in_minutes)))
    return items


async def get_my_schedules(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    work_location_id: uuid.UUID | None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> MySchedulesResult:
    """`GET .../my-schedules`. Owner (не member) → пустой список (не трекает время).

    Резолв точки (приоритет, `work_schedules_geo_resolve/backend.md`):
    1. `work_location_id` передан явно — используется как есть (обратная
       совместимость с текущим контрактом и ручным режимом).
    2. Иначе, если у организации включена геопроверка и переданы оба
       `lat`/`lng` — резолв ближайшей точки тем же хелпером, что и старт
       смены (`resolve_nearest_work_location`). Не найдена — НЕ ошибка
       здесь (в отличие от `/shifts/start`): сотрудник может ещё не дойти
       до точки, это только превью списка перед стартом. Резолв графиков
       идёт без точки — global/role видны, location-only нет.
    3. Иначе — без точки, как раньше.

    `lat`/`lng` при выключенной геопроверке игнорируются — у таких
    организаций точка выбирается вручную через `work_location_id`.
    """
    from src.app.services.organization_settings import get_settings_for_org
    from src.app.services.work_location import resolve_nearest_work_location

    org = await get_organization(session, org_id)
    await ensure_member(session, org, user_id)

    org_settings = await get_settings_for_org(session, org_id)
    require_schedule = org_settings.require_schedule if org_settings is not None else False
    early_start_minutes = org_settings.early_start_minutes if org_settings is not None else 0
    geo_check_enabled = org_settings is not None and org_settings.geo_check_enabled

    resolved_location: WorkLocation | None = None
    if work_location_id is not None:
        resolved_location = await _get_org_location(session, org_id, work_location_id)
    elif geo_check_enabled and latitude is not None and longitude is not None:
        resolved_location = await resolve_nearest_work_location(
            session, org_id, latitude, longitude
        )
    # effective_location_id полностью выводится из resolved_location: при явном
    # work_location_id resolved_location.id ему тождественен, при резолве по
    # lat/lng — это найденная точка, иначе обе стороны None.
    effective_location_id = (
        resolved_location.id if resolved_location is not None else work_location_id
    )

    member_result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = member_result.scalar_one_or_none()
    if member is None:
        return MySchedulesResult(
            items=[],
            require_schedule=require_schedule,
            early_start_minutes=early_start_minutes,
            resolved_work_location=None,
        )

    pairs = await get_effective_schedules(session, org_id, member, effective_location_id)
    tz = ZoneInfo(org.timezone)
    now = datetime.now(UTC)
    items = _build_my_schedule_items(pairs, tz, now, early_start_minutes)
    return MySchedulesResult(
        items=items,
        require_schedule=require_schedule,
        early_start_minutes=early_start_minutes,
        resolved_work_location=resolved_location,
    )


# --- R7: смена графика администратором ------------------------------------------


async def change_shift_schedule(
    session: AsyncSession,
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    requester_id: uuid.UUID,
    work_schedule_id: uuid.UUID | None,
) -> "Shift":
    """Пересчитывает `scheduled_*` от неизменного `started_at`. `finished_at`
    завершённой смены не трогается никогда — план меняется, факт остаётся фактом."""
    from src.app.services.shift import get_org_shift_detail

    org = await get_organization(session, org_id)
    await _check_admin_or_owner(session, org, requester_id)
    await entitlements.require_active_subscription(session, org, requester_id)
    shift = await get_org_shift_detail(session, org_id, shift_id)

    if work_schedule_id is None:
        shift.work_schedule_id = None
        shift.schedule_name = None
        shift.scheduled_start_at = None
        shift.scheduled_end_at = None
    else:
        schedule = await _get_schedule(session, org_id, work_schedule_id)
        start_utc, end_utc = compute_scheduled_window(
            shift.started_at, ZoneInfo(org.timezone), schedule.start_time, schedule.end_time
        )
        shift.work_schedule_id = schedule.id
        shift.schedule_name = schedule.name
        shift.scheduled_start_at = start_utc
        shift.scheduled_end_at = end_utc

    await session.flush()
    logger.info(
        "shift_schedule_changed",
        org_id=str(org_id),
        shift_id=str(shift_id),
        new_schedule_id=str(work_schedule_id) if work_schedule_id else None,
    )
    return shift
