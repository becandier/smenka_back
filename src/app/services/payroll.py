"""Ставки участников и расчёт зарплаты (payroll).

История ставок — источник истины: одна строка `organization_member_rates` =
ставка, действующая с `effective_from`. Расчёты выполняются «на лету» при
запросе и нигде не кэшируются: правка/удаление записи истории сразу отражается
на следующих расчётах. Деньги — только целые копейки; накопление по сменам
точным Decimal. Округление half-up: в режиме `granularity=none` — ровно один раз
на итог сотрудника; в детальном режиме/экспорте — посуточно (атом = день), см.
`_build_breakdown` и ADR-002.
"""

import io
import re
import uuid
from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openpyxl import Workbook
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.member_rate import OrganizationMemberRate, RateType
from src.app.models.organization import OrganizationMember
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User
from src.app.models.work_location import WorkLocation
from src.app.schemas.payroll import Granularity
from src.app.services import organization as org_service
from src.app.services.common import ensure_admin_or_owner
from src.app.services.shift import (
    calculate_worked_seconds,
    compute_late_seconds,
    ensure_utc,
    validate_date_range,
)

logger = get_logger(__name__)

SECONDS_PER_HOUR = Decimal(3600)
PAYROLL_CURRENCY = "RUB"
NO_LOCATION_TOKENS = frozenset({"none", "null"})

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class PayrollError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


async def _get_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
) -> OrganizationMember:
    """Участник по organization_members.id строго в пределах организации."""
    result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == org_id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        raise PayrollError("MEMBER_NOT_FOUND", "Участник не найден", 404)
    return member


async def _ensure_no_rate_at(
    session: AsyncSession,
    member_id: uuid.UUID,
    effective_from: datetime,
    exclude_id: uuid.UUID | None = None,
) -> None:
    conditions = [
        OrganizationMemberRate.member_id == member_id,
        OrganizationMemberRate.effective_from == effective_from,
    ]
    if exclude_id is not None:
        conditions.append(OrganizationMemberRate.id != exclude_id)
    result = await session.execute(select(OrganizationMemberRate.id).where(*conditions))
    if result.scalar_one_or_none() is not None:
        raise PayrollError(
            "RATE_EFFECTIVE_FROM_TAKEN",
            "На эту дату начала действия у участника уже есть ставка",
            409,
        )


async def create_rate(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    rate_amount_minor: int,
    rate_type: RateType,
    currency: str,
    effective_from: datetime,
    note: str | None,
) -> OrganizationMemberRate:
    """Добавить новую строку истории (назначение ставки «с даты»)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    member = await _get_member(session, org_id, member_id)

    effective_from = ensure_utc(effective_from)
    await _ensure_no_rate_at(session, member.id, effective_from)

    rate = OrganizationMemberRate(
        member_id=member.id,
        rate_amount_minor=rate_amount_minor,
        rate_type=rate_type,
        currency=currency,
        effective_from=effective_from,
        note=note,
    )
    session.add(rate)
    try:
        # UNIQUE (member_id, effective_from) закрывает гонку двух параллельных POST
        await session.flush()
    except IntegrityError:
        raise PayrollError(
            "RATE_EFFECTIVE_FROM_TAKEN",
            "На эту дату начала действия у участника уже есть ставка",
            409,
        ) from None

    logger.info(
        "member_rate_created",
        org_id=str(org_id),
        member_id=str(member.id),
        rate_id=str(rate.id),
        rate_type=rate_type.value,
        effective_from=effective_from.isoformat(),
    )
    return rate


async def list_rates(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[OrganizationMemberRate]:
    """Вся история ставок участника, effective_from DESC."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    member = await _get_member(session, org_id, member_id)

    result = await session.execute(
        select(OrganizationMemberRate)
        .where(OrganizationMemberRate.member_id == member.id)
        .order_by(OrganizationMemberRate.effective_from.desc())
    )
    return list(result.scalars().all())


async def _get_rate(
    session: AsyncSession,
    member_id: uuid.UUID,
    rate_id: uuid.UUID,
) -> OrganizationMemberRate:
    result = await session.execute(
        select(OrganizationMemberRate).where(
            OrganizationMemberRate.id == rate_id,
            OrganizationMemberRate.member_id == member_id,
        )
    )
    rate = result.scalar_one_or_none()
    if rate is None:
        raise PayrollError("RATE_NOT_FOUND", "Запись ставки не найдена", 404)
    return rate


async def update_rate(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    rate_id: uuid.UUID,
    requester_id: uuid.UUID,
    fields: dict[str, Any],
) -> OrganizationMemberRate:
    """Исправить запись истории (опечатка). Для новой ставки «с даты» — POST."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    member = await _get_member(session, org_id, member_id)
    rate = await _get_rate(session, member.id, rate_id)

    # Явный null допустим только для nullable-поля note;
    # для остальных полей null трактуем как «не менять».
    fields = {k: v for k, v in fields.items() if v is not None or k == "note"}

    new_effective_from = fields.get("effective_from")
    if new_effective_from is not None:
        new_effective_from = ensure_utc(new_effective_from)
        fields["effective_from"] = new_effective_from
        if new_effective_from != rate.effective_from:
            await _ensure_no_rate_at(
                session,
                member.id,
                new_effective_from,
                exclude_id=rate.id,
            )

    for key, value in fields.items():
        setattr(rate, key, value)

    try:
        await session.flush()
    except IntegrityError:
        raise PayrollError(
            "RATE_EFFECTIVE_FROM_TAKEN",
            "На эту дату начала действия у участника уже есть ставка",
            409,
        ) from None

    logger.info(
        "member_rate_updated",
        org_id=str(org_id),
        member_id=str(member.id),
        rate_id=str(rate.id),
        fields=sorted(fields),
    )
    return rate


async def delete_rate(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    rate_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Удалить ошибочную запись истории. Расчёты сразу увидят изменение."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)
    member = await _get_member(session, org_id, member_id)
    rate = await _get_rate(session, member.id, rate_id)

    await session.delete(rate)
    await session.flush()
    logger.info(
        "member_rate_deleted",
        org_id=str(org_id),
        member_id=str(member.id),
        rate_id=str(rate_id),
    )


async def get_current_rates(
    session: AsyncSession,
    member_ids: list[uuid.UUID],
    at: datetime | None = None,
) -> dict[uuid.UUID, OrganizationMemberRate]:
    """member_id → действующая ставка (max effective_from <= at) одним запросом.

    Участники без действующей ставки (нет строк или все в будущем)
    в результат не попадают.
    """
    if not member_ids:
        return {}
    moment = at if at is not None else datetime.now(UTC)
    result = await session.execute(
        select(OrganizationMemberRate)
        .distinct(OrganizationMemberRate.member_id)
        .where(
            OrganizationMemberRate.member_id.in_(member_ids),
            OrganizationMemberRate.effective_from <= moment,
        )
        .order_by(
            OrganizationMemberRate.member_id,
            OrganizationMemberRate.effective_from.desc(),
        )
    )
    return {r.member_id: r for r in result.scalars().all()}


def _rate_for_moment(
    rates_asc: list[OrganizationMemberRate],
    moment: datetime,
) -> OrganizationMemberRate | None:
    """Ставка с максимальным effective_from <= moment (rates отсортированы ASC)."""
    idx = bisect_right([r.effective_from for r in rates_asc], moment)
    return rates_asc[idx - 1] if idx else None


def _calc_earnings(
    shifts: list[Shift],
    rates_asc: list[OrganizationMemberRate],
    *,
    overtime_minutes_by_shift: dict[uuid.UUID, int] | None = None,
    late_tolerance_minutes: int = 0,
) -> dict[str, Any]:
    """Агрегаты по завершённым сменам: каждая смена — по ставке на её started_at.

    Сумма накапливается точным Decimal; half-up до целой копейки — один раз
    на итог (и для факта, и для плана — round-once применяется одинаково к
    обеим суммам). Смены без действующей ставки в gross/planned не входят
    (unpaid_*). `overtime_minutes_by_shift` — согласованная переработка
    (backend.md, R6/R8): добавляется к оплачиваемому времени `hourly`-ставки,
    `per_shift` игнорирует (план = факт, дельта 0). `late_tolerance_minutes` —
    допуск организации для подсчёта опозданий (R5/R8).
    """
    overtime_map = overtime_minutes_by_shift or {}

    amount = Decimal(0)
    planned_amount = Decimal(0)
    worked_seconds = 0
    overtime_seconds = 0
    planned_seconds = 0
    unpaid_seconds = 0
    unpaid_shifts_count = 0
    late_count = 0
    late_seconds_total = 0

    for shift in shifts:
        seconds = calculate_worked_seconds(shift)
        worked_seconds += seconds

        shift_overtime_seconds = overtime_map.get(shift.id, 0) * 60
        overtime_seconds += shift_overtime_seconds
        paid_seconds = seconds + shift_overtime_seconds

        # План: у смены есть график → длительность окна; иначе план = факт
        # (backend.md, R8 — иначе дельта врала бы на весь объём таких смен).
        if shift.scheduled_start_at is not None and shift.scheduled_end_at is not None:
            shift_planned_seconds = int(
                (shift.scheduled_end_at - shift.scheduled_start_at).total_seconds()
            )
        else:
            shift_planned_seconds = seconds
        planned_seconds += shift_planned_seconds

        late = compute_late_seconds(shift, late_tolerance_minutes)
        if late is not None and late > 0:
            late_count += 1
            late_seconds_total += late

        rate = _rate_for_moment(rates_asc, shift.started_at)
        if rate is None:
            unpaid_seconds += seconds
            unpaid_shifts_count += 1
            continue

        if rate.rate_type == RateType.hourly:
            amount += Decimal(paid_seconds) * rate.rate_amount_minor / SECONDS_PER_HOUR
            planned_amount += (
                Decimal(shift_planned_seconds) * rate.rate_amount_minor / (SECONDS_PER_HOUR)
            )
        else:  # per_shift: переработка не влияет на деньги, план = факт (дельта 0)
            amount += Decimal(rate.rate_amount_minor)
            planned_amount += Decimal(rate.rate_amount_minor)

    gross = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    planned = int(planned_amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "worked_seconds": worked_seconds,
        "overtime_seconds": overtime_seconds,
        "shifts_count": len(shifts),
        "gross_amount_minor": gross,
        "unpaid_seconds": unpaid_seconds,
        "unpaid_shifts_count": unpaid_shifts_count,
        "has_missing_rate": unpaid_shifts_count > 0,
        "planned_seconds": planned_seconds,
        "planned_amount_minor": planned,
        "delta_amount_minor": gross - planned,
        "late_count": late_count,
        "late_seconds_total": late_seconds_total,
    }


async def _get_finished_shifts(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    user_ids: list[uuid.UUID] | None = None,
    location_ids: list[uuid.UUID] | None = None,
    include_no_location: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[Shift]:
    conditions = [
        Shift.organization_id == org_id,
        Shift.status == ShiftStatus.finished,
        Shift.is_deleted.is_(False),
    ]
    if user_id is not None:
        conditions.append(Shift.user_id == user_id)
    if user_ids:
        conditions.append(Shift.user_id.in_(user_ids))
    if location_ids or include_no_location:
        loc_clauses = []
        if location_ids:
            loc_clauses.append(Shift.work_location_id.in_(location_ids))
        if include_no_location:
            loc_clauses.append(Shift.work_location_id.is_(None))
        conditions.append(or_(*loc_clauses))
    if date_from is not None:
        conditions.append(Shift.started_at >= date_from)
    if date_to is not None:
        conditions.append(Shift.started_at <= date_to)

    result = await session.execute(
        select(Shift).options(selectinload(Shift.pauses)).where(*conditions)
    )
    return list(result.scalars().all())


async def _load_rates_asc(
    session: AsyncSession,
    member_ids: list[uuid.UUID],
) -> dict[uuid.UUID, list[OrganizationMemberRate]]:
    """member_id → вся история ставок, отсортированная по effective_from ASC."""
    if not member_ids:
        return {}
    result = await session.execute(
        select(OrganizationMemberRate)
        .where(OrganizationMemberRate.member_id.in_(member_ids))
        .order_by(OrganizationMemberRate.effective_from)
    )
    rates_by_member: dict[uuid.UUID, list[OrganizationMemberRate]] = defaultdict(list)
    for rate in result.scalars().all():
        rates_by_member[rate.member_id].append(rate)
    return rates_by_member


def _validate_tz(tz: str) -> ZoneInfo:
    """IANA-таймзона нарезки корзин; неизвестная/битая → 422 VALIDATION_ERROR."""
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise PayrollError("VALIDATION_ERROR", f"Неизвестная таймзона: {tz}", 422) from None


def _parse_uuid_list(values: list[str] | None, field: str) -> list[uuid.UUID]:
    """Список uuid из повторов параметра и/или CSV; битый элемент → 422."""
    if not values:
        return []
    out: list[uuid.UUID] = []
    for raw in values:
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            try:
                out.append(uuid.UUID(token))
            except ValueError:
                raise PayrollError(
                    "VALIDATION_ERROR", f"Некорректный {field}: {token}", 422
                ) from None
    return out


def _parse_location_filter(values: list[str] | None) -> tuple[list[uuid.UUID], bool]:
    """Разобрать location_ids: реальные точки + спец-значение none/null («без точки»)."""
    if not values:
        return [], False
    ids: list[uuid.UUID] = []
    include_no_location = False
    for raw in values:
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            if token.lower() in NO_LOCATION_TOKENS:
                include_no_location = True
                continue
            try:
                ids.append(uuid.UUID(token))
            except ValueError:
                raise PayrollError(
                    "VALIDATION_ERROR", f"Некорректный location_ids: {token}", 422
                ) from None
    return ids, include_no_location


async def _validate_location_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
    location_ids: list[uuid.UUID],
) -> None:
    """Точки фильтра должны принадлежать организации; чужие/несуществующие → 422."""
    if not location_ids:
        return
    result = await session.execute(
        select(WorkLocation.id).where(
            WorkLocation.organization_id == org_id,
            WorkLocation.id.in_(location_ids),
        )
    )
    found = set(result.scalars().all())
    missing = set(location_ids) - found
    if missing:
        raise PayrollError(
            "VALIDATION_ERROR",
            "Точка не найдена в организации",
            422,
        )


def _bucket_start(day: date, granularity: str) -> date:
    """Начало корзины, в которую попадает локальный день."""
    if granularity == Granularity.week:
        return day - timedelta(days=day.weekday())
    if granularity == Granularity.month:
        return day.replace(day=1)
    return day  # granularity == day


_BREAKDOWN_SUM_FIELDS = (
    "worked_seconds",
    "overtime_seconds",
    "shifts_count",
    "gross_amount_minor",
    "unpaid_seconds",
    "unpaid_shifts_count",
    "planned_seconds",
    "planned_amount_minor",
    "late_count",
    "late_seconds_total",
)


def _build_breakdown(
    user_shifts: list[Shift],
    rates_asc: list[OrganizationMemberRate],
    zone: ZoneInfo,
    granularity: str,
    *,
    overtime_minutes_by_shift: dict[uuid.UUID, int] | None = None,
    late_tolerance_minutes: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Суточная разбивка сотрудника: округление денег — атомарно по дню.

    Каждая смена попадает в день по `started_at` в таймзоне `zone`. По дню сумма
    округляется half-up один раз (факт И план — тем же правилом); корзины
    week/month и итог сотрудника — суммы уже округлённых дневных значений
    (день → корзина → сотрудник → всего). `delta_amount_minor` считается ПОСЛЕ
    суммирования (простое вычитание уже округлённых целых, доп. округление не
    нужно) — и на уровне корзины, и на уровне агрегата.
    """
    shifts_by_day: dict[date, list[Shift]] = defaultdict(list)
    for shift in user_shifts:
        shifts_by_day[shift.started_at.astimezone(zone).date()].append(shift)

    buckets: dict[date, dict[str, Any]] = {}
    for day, day_shifts in shifts_by_day.items():
        daily = _calc_earnings(
            day_shifts,
            rates_asc,
            overtime_minutes_by_shift=overtime_minutes_by_shift,
            late_tolerance_minutes=late_tolerance_minutes,
        )
        start = _bucket_start(day, granularity)
        acc = buckets.setdefault(
            start,
            dict.fromkeys(_BREAKDOWN_SUM_FIELDS, 0) | {"has_missing_rate": False},
        )
        for field in _BREAKDOWN_SUM_FIELDS:
            acc[field] += daily[field]
        acc["has_missing_rate"] = acc["has_missing_rate"] or daily["has_missing_rate"]

    breakdown = [
        {
            "bucket_start": start.isoformat(),
            **{field: acc[field] for field in _BREAKDOWN_SUM_FIELDS},
            "delta_amount_minor": acc["gross_amount_minor"] - acc["planned_amount_minor"],
            "has_missing_rate": acc["has_missing_rate"],
        }
        for start, acc in sorted(buckets.items())
    ]
    aggregate = {
        field: sum(acc[field] for acc in buckets.values()) for field in _BREAKDOWN_SUM_FIELDS
    }
    aggregate["delta_amount_minor"] = (
        aggregate["gross_amount_minor"] - aggregate["planned_amount_minor"]
    )
    aggregate["has_missing_rate"] = any(acc["has_missing_rate"] for acc in buckets.values())
    return breakdown, aggregate


async def get_org_payroll(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    granularity: str = Granularity.none,
    user_ids: list[str] | None = None,
    location_ids: list[str] | None = None,
    tz: str = "UTC",
    only_missing_rate: bool = False,
    include_penalties: bool = True,
    include_adjustments: bool = True,
) -> dict[str, Any]:
    """Отчёт «сколько кому заплатить» за период (owner/admin).

    Учитываются все завершённые смены организации в периоде, включая смены
    исключённых сотрудников (их история ставок удалена каскадом → unpaid).
    Owner строкой items не фигурирует (ADR-001: owner != member).

    При `granularity != none` к каждому сотруднику добавляется `breakdown` —
    разбивка по дням/неделям/месяцам в таймзоне `tz`, а деньги округляются
    посуточно (см. `_build_breakdown` и ADR-002). Фильтры `user_ids`,
    `location_ids` (вкл. спец-значение none — «без точки»), `only_missing_rate`
    сужают выборку. При `granularity == none` ответ байт-в-байт совместим с
    прежним контрактом (поля breakdown/granularity/tz отсутствуют).
    `include_adjustments` (manual_time_entry) — учитывать ли ручные начисления
    (`payroll_adjustments`) в `net`; знаковая сумма — на `gross` не влияет.
    """
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)

    if granularity not in set(Granularity):
        raise PayrollError("VALIDATION_ERROR", f"Неизвестная granularity: {granularity}", 422)
    zone = _validate_tz(tz)

    validate_date_range(date_from, date_to)
    norm_from = ensure_utc(date_from) if date_from is not None else None
    norm_to = ensure_utc(date_to) if date_to is not None else None

    parsed_user_ids = _parse_uuid_list(user_ids, "user_ids")
    loc_ids, include_no_location = _parse_location_filter(location_ids)
    await _validate_location_ids(session, org_id, loc_ids)

    shifts = await _get_finished_shifts(
        session,
        org_id,
        user_ids=parsed_user_ids or None,
        location_ids=loc_ids or None,
        include_no_location=include_no_location,
        date_from=norm_from,
        date_to=norm_to,
    )

    shifts_by_user: dict[uuid.UUID, list[Shift]] = defaultdict(list)
    for shift in shifts:
        shifts_by_user[shift.user_id].append(shift)

    shift_user_ids = set(shifts_by_user)

    # Согласованная переработка (R6/R8) и допуск по опозданию организации (R5/R8).
    from src.app.services import overtime as overtime_service

    overtime_minutes_by_shift = await overtime_service.get_approved_overtime_minutes_by_shift(
        session, [s.id for s in shifts]
    )
    late_tolerance_minutes = org.settings.late_tolerance_minutes if org.settings is not None else 0

    # Штрафы периода: атрибутируются сотруднику (member → user), вычитаются из net.
    # Сотрудник только со штрафами (без завершённых смен) тоже попадает в items —
    # иначе штраф «потеряется» (см. backend.md).
    penalties_by_user: dict[uuid.UUID, tuple[int, int]] = {}
    if include_penalties:
        from src.app.services import penalty as penalty_service

        penalties_by_user = await penalty_service.aggregate_penalties_by_user(
            session, org_id, date_from=norm_from, date_to=norm_to
        )
        if parsed_user_ids:
            allowed = set(parsed_user_ids)
            penalties_by_user = {u: v for u, v in penalties_by_user.items() if u in allowed}

    # Ручные начисления периода (manual_time_entry): знаковые, на gross не влияют,
    # только на net. Сотрудник только с начислением (без смен/штрафов) тоже
    # попадает в items — иначе начисление «потеряется» из отчёта (то же правило,
    # что и для штрафов).
    adjustments_by_user: dict[uuid.UUID, tuple[int, int]] = {}
    if include_adjustments:
        from src.app.services import adjustment as adjustment_service

        adjustments_by_user = await adjustment_service.aggregate_adjustments_by_user(
            session, org_id, date_from=norm_from, date_to=norm_to
        )
        if parsed_user_ids:
            allowed = set(parsed_user_ids)
            adjustments_by_user = {u: v for u, v in adjustments_by_user.items() if u in allowed}

    all_user_ids = list(shift_user_ids | set(penalties_by_user) | set(adjustments_by_user))
    users_map: dict[uuid.UUID, str] = {}
    member_id_by_user: dict[uuid.UUID, uuid.UUID] = {}
    display_name_by_user: dict[uuid.UUID, str | None] = {}
    rates_by_member: dict[uuid.UUID, list[OrganizationMemberRate]] = {}
    if all_user_ids:
        users_result = await session.execute(
            select(User.id, User.name).where(User.id.in_(all_user_ids))
        )
        users_map = dict(users_result.tuples().all())

        members_result = await session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id.in_(all_user_ids),
            )
        )
        members = list(members_result.scalars().all())
        member_id_by_user = {m.user_id: m.id for m in members}
        display_name_by_user = {m.user_id: m.display_name for m in members}
        rates_by_member = await _load_rates_asc(session, [m.id for m in members])

    detailed = granularity != Granularity.none
    items: list[dict[str, Any]] = []
    for uid in all_user_ids:
        user_shifts = shifts_by_user.get(uid, [])
        member_id = member_id_by_user.get(uid)
        rates_asc = rates_by_member.get(member_id, []) if member_id else []
        base = {
            "user_id": str(uid),
            "user_name": users_map.get(uid, "Unknown"),
            "display_name": display_name_by_user.get(uid),
        }
        if detailed:
            breakdown, aggregate = _build_breakdown(
                user_shifts,
                rates_asc,
                zone,
                granularity,
                overtime_minutes_by_shift=overtime_minutes_by_shift,
                late_tolerance_minutes=late_tolerance_minutes,
            )
            entry = {**base, **aggregate, "breakdown": breakdown}
        else:
            entry = {
                **base,
                **_calc_earnings(
                    user_shifts,
                    rates_asc,
                    overtime_minutes_by_shift=overtime_minutes_by_shift,
                    late_tolerance_minutes=late_tolerance_minutes,
                ),
            }
        penalty_amount, penalties_count = penalties_by_user.get(uid, (0, 0))
        adjustment_amount, adjustments_count = adjustments_by_user.get(uid, (0, 0))
        entry["penalty_amount_minor"] = penalty_amount
        entry["penalties_count"] = penalties_count
        entry["adjustment_amount_minor"] = adjustment_amount
        entry["adjustments_count"] = adjustments_count
        entry["net_amount_minor"] = (
            entry["gross_amount_minor"] - penalty_amount + adjustment_amount
        )
        items.append(entry)

    if only_missing_rate:
        # Сотрудник только со штрафами/начислениями остаётся в выборке, иначе
        # они «потеряются» из items и totals (см. backend.md).
        items = [
            item
            for item in items
            if item["has_missing_rate"]
            or item["penalties_count"] > 0
            or item["adjustments_count"] > 0
        ]
    items.sort(key=lambda item: (item["user_name"], item["user_id"]))

    totals = {
        "worked_seconds": sum(i["worked_seconds"] for i in items),
        "overtime_seconds": sum(i["overtime_seconds"] for i in items),
        "shifts_count": sum(i["shifts_count"] for i in items),
        "gross_amount_minor": sum(i["gross_amount_minor"] for i in items),
        "penalty_amount_minor": sum(i["penalty_amount_minor"] for i in items),
        "penalties_count": sum(i["penalties_count"] for i in items),
        "adjustment_amount_minor": sum(i["adjustment_amount_minor"] for i in items),
        "adjustments_count": sum(i["adjustments_count"] for i in items),
        "net_amount_minor": sum(i["net_amount_minor"] for i in items),
        "planned_seconds": sum(i["planned_seconds"] for i in items),
        "planned_amount_minor": sum(i["planned_amount_minor"] for i in items),
        "delta_amount_minor": sum(i["delta_amount_minor"] for i in items),
        "late_count": sum(i["late_count"] for i in items),
        "late_seconds_total": sum(i["late_seconds_total"] for i in items),
    }
    report: dict[str, Any] = {
        "period": {"date_from": norm_from, "date_to": norm_to},
        "currency": PAYROLL_CURRENCY,
        "items": items,
        "totals": totals,
    }
    if detailed:
        report["granularity"] = granularity
        report["tz"] = tz
    return report


async def get_my_earnings(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """Личный заработок участника за период + действующая ставка.

    Доступ только реальным участникам организации: owner (не member)
    и посторонние получают 403 FORBIDDEN.
    """
    org = await org_service.get_organization(session, org_id)

    member_result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == org_id,
            OrganizationMember.user_id == user_id,
        )
    )
    member = member_result.scalar_one_or_none()
    if member is None:
        raise PayrollError(
            "FORBIDDEN",
            "Вы не являетесь участником организации",
            403,
        )

    validate_date_range(date_from, date_to)
    norm_from = ensure_utc(date_from) if date_from is not None else None
    norm_to = ensure_utc(date_to) if date_to is not None else None

    shifts = await _get_finished_shifts(
        session,
        org_id,
        user_id=user_id,
        date_from=norm_from,
        date_to=norm_to,
    )
    rates_by_member = await _load_rates_asc(session, [member.id])
    rates_asc = rates_by_member.get(member.id, [])

    from src.app.services import overtime as overtime_service

    overtime_minutes_by_shift = await overtime_service.get_approved_overtime_minutes_by_shift(
        session, [s.id for s in shifts]
    )
    late_tolerance_minutes = org.settings.late_tolerance_minutes if org.settings is not None else 0

    earnings = _calc_earnings(
        shifts,
        rates_asc,
        overtime_minutes_by_shift=overtime_minutes_by_shift,
        late_tolerance_minutes=late_tolerance_minutes,
    )
    current_rate = _rate_for_moment(rates_asc, datetime.now(UTC))

    # Для self штрафы/начисления учитываются всегда (флагов include_* здесь нет).
    from src.app.services import adjustment as adjustment_service
    from src.app.services import penalty as penalty_service

    penalty_amount, penalties_count = await penalty_service.aggregate_member_penalties(
        session, member.id, date_from=norm_from, date_to=norm_to
    )
    adjustment_amount, adjustments_count = await adjustment_service.aggregate_member_adjustments(
        session, member.id, date_from=norm_from, date_to=norm_to
    )

    return {
        "period": {"date_from": norm_from, "date_to": norm_to},
        "currency": PAYROLL_CURRENCY,
        "worked_seconds": earnings["worked_seconds"],
        "overtime_seconds": earnings["overtime_seconds"],
        "shifts_count": earnings["shifts_count"],
        "gross_amount_minor": earnings["gross_amount_minor"],
        "penalty_amount_minor": penalty_amount,
        "penalties_count": penalties_count,
        "adjustment_amount_minor": adjustment_amount,
        "adjustments_count": adjustments_count,
        "net_amount_minor": earnings["gross_amount_minor"] - penalty_amount + adjustment_amount,
        "has_missing_rate": earnings["has_missing_rate"],
        "current_rate": current_rate,
        "planned_seconds": earnings["planned_seconds"],
        "planned_amount_minor": earnings["planned_amount_minor"],
        "delta_amount_minor": earnings["delta_amount_minor"],
        "late_count": earnings["late_count"],
        "late_seconds_total": earnings["late_seconds_total"],
    }


def _hours(seconds: int) -> float:
    """Секунды → часы с 2 знаками (число для суммирования в Excel)."""
    return round(seconds / 3600, 2)


def _money(minor: int) -> float:
    """Копейки → рубли числом (для суммирования в Excel)."""
    return round(minor / 100, 2)


def _filename_date(value: datetime | None) -> str:
    return value.date().isoformat() if value is not None else "all"


def _org_filename_slug(name: str) -> str:
    """Латинский слаг имени org для имени файла; кириллица/пусто → 'org'."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    return slug or "org"


def _build_payroll_xlsx(report: dict[str, Any], org_name: str) -> bytes:
    """Книга Excel: лист «Сводка» (агрегат по сотрудникам) + «Детализация».

    Часы и деньги — числами (часы с 2 знаками, деньги в рублях), чтобы Excel
    суммировал колонки. Детализация уже отсортирована (сотрудник, bucket_start).
    """
    period = report["period"]
    date_from = _filename_date(period["date_from"])
    date_to = _filename_date(period["date_to"])

    wb = Workbook()
    summary = wb.active
    summary.title = "Сводка"
    summary.append([f"Организация: {org_name}"])
    summary.append([f"Период: {date_from} — {date_to}"])
    summary.append([f"Валюта: {report['currency']}"])
    summary.append([])
    summary.append(
        [
            "Сотрудник",
            "Часы",
            "Смены",
            "Начислено, ₽",
            "Штраф, ₽",
            "Начисления/удержания, ₽",
            "К выплате, ₽",
            "Без ставки (смен)",
            "Без ставки (часов)",
            "Переработка, ч",
            "По графику, ч",
            "По графику, ₽",
            "Разница, ₽",
            "Опозданий",
            "Опоздания, мин",
        ]
    )
    for item in report["items"]:
        summary.append(
            [
                item["user_name"],
                _hours(item["worked_seconds"]),
                item["shifts_count"],
                _money(item["gross_amount_minor"]),
                _money(item["penalty_amount_minor"]),
                _money(item["adjustment_amount_minor"]),
                _money(item["net_amount_minor"]),
                item["unpaid_shifts_count"],
                _hours(item["unpaid_seconds"]),
                _hours(item["overtime_seconds"]),
                _hours(item["planned_seconds"]),
                _money(item["planned_amount_minor"]),
                _money(item["delta_amount_minor"]),
                item["late_count"],
                round(item["late_seconds_total"] / 60, 1),
            ]
        )
    totals = report["totals"]
    summary.append(
        [
            "ИТОГО",
            _hours(totals["worked_seconds"]),
            totals["shifts_count"],
            _money(totals["gross_amount_minor"]),
            _money(totals["penalty_amount_minor"]),
            _money(totals["adjustment_amount_minor"]),
            _money(totals["net_amount_minor"]),
            "",
            "",
            _hours(totals["overtime_seconds"]),
            _hours(totals["planned_seconds"]),
            _money(totals["planned_amount_minor"]),
            _money(totals["delta_amount_minor"]),
            totals["late_count"],
            round(totals["late_seconds_total"] / 60, 1),
        ]
    )

    # В «Детализации» штрафы не разбиваются по дням (период-уровень) — суммарный
    # штраф/«к выплате» сотрудника смотрите в «Сводке»; здесь Штраф=0, К выплате=Начислено.
    detail = wb.create_sheet("Детализация")
    detail.append(
        [
            "Сотрудник",
            "Дата",
            "Часы",
            "Смены",
            "Начислено, ₽",
            "Штраф, ₽",
            "К выплате, ₽",
            "Без ставки (часов)",
            "По графику, ч",
            "По графику, ₽",
            "Разница, ₽",
        ]
    )
    for item in report["items"]:
        for bucket in item.get("breakdown", []):
            detail.append(
                [
                    item["user_name"],
                    bucket["bucket_start"],
                    _hours(bucket["worked_seconds"]),
                    bucket["shifts_count"],
                    _money(bucket["gross_amount_minor"]),
                    _money(0),
                    _money(bucket["gross_amount_minor"]),
                    _hours(bucket["unpaid_seconds"]),
                    _hours(bucket["planned_seconds"]),
                    _money(bucket["planned_amount_minor"]),
                    _money(bucket["delta_amount_minor"]),
                ]
            )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def export_org_payroll(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    granularity: str | None = None,
    user_ids: list[str] | None = None,
    location_ids: list[str] | None = None,
    tz: str = "UTC",
    only_missing_rate: bool = False,
    include_penalties: bool = True,
    include_adjustments: bool = True,
) -> tuple[bytes, str]:
    """Сформировать .xlsx отчёта payroll и имя файла.

    Детализация — смысл выгрузки: если `granularity` не задан (или none), берём
    `day`. Расчёт и фильтры идентичны `get_org_payroll`.
    """
    if granularity and granularity != Granularity.none:
        effective = granularity
    else:
        effective = Granularity.day.value
    report = await get_org_payroll(
        session,
        org_id,
        requester_id,
        date_from=date_from,
        date_to=date_to,
        granularity=effective,
        user_ids=user_ids,
        location_ids=location_ids,
        tz=tz,
        only_missing_rate=only_missing_rate,
        include_penalties=include_penalties,
        include_adjustments=include_adjustments,
    )

    org = await org_service.get_organization(session, org_id)
    content = _build_payroll_xlsx(report, org.name)
    filename = (
        f"payroll_{_org_filename_slug(org.name)}"
        f"_{_filename_date(report['period']['date_from'])}"
        f"_{_filename_date(report['period']['date_to'])}.xlsx"
    )
    return content, filename
