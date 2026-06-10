"""Ставки участников и расчёт зарплаты (payroll).

История ставок — источник истины: одна строка `organization_member_rates` =
ставка, действующая с `effective_from`. Расчёты выполняются «на лету» при
запросе и нигде не кэшируются: правка/удаление записи истории сразу отражается
на следующих расчётах. Деньги — только целые копейки; накопление по сменам
точным Decimal, округление half-up ровно один раз на итог сотрудника.
"""

import uuid
from bisect import bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.member_rate import OrganizationMemberRate, RateType
from src.app.models.organization import OrganizationMember
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User
from src.app.services import organization as org_service
from src.app.services.common import ensure_admin_or_owner
from src.app.services.shift import (
    calculate_worked_seconds,
    ensure_utc,
    validate_date_range,
)

logger = get_logger(__name__)

SECONDS_PER_HOUR = Decimal(3600)
PAYROLL_CURRENCY = "RUB"


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
) -> dict[str, Any]:
    """Агрегаты по завершённым сменам: каждая смена — по ставке на её started_at.

    Сумма накапливается точным Decimal; half-up до целой копейки — один раз
    на итог. Смены без действующей ставки в gross не входят (unpaid_*).
    """
    amount = Decimal(0)
    worked_seconds = 0
    unpaid_seconds = 0
    unpaid_shifts_count = 0

    for shift in shifts:
        seconds = calculate_worked_seconds(shift)
        worked_seconds += seconds
        rate = _rate_for_moment(rates_asc, shift.started_at)
        if rate is None:
            unpaid_seconds += seconds
            unpaid_shifts_count += 1
            continue
        if rate.rate_type == RateType.hourly:
            amount += Decimal(seconds) * rate.rate_amount_minor / SECONDS_PER_HOUR
        else:  # per_shift
            amount += Decimal(rate.rate_amount_minor)

    gross = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {
        "worked_seconds": worked_seconds,
        "shifts_count": len(shifts),
        "gross_amount_minor": gross,
        "unpaid_seconds": unpaid_seconds,
        "unpaid_shifts_count": unpaid_shifts_count,
        "has_missing_rate": unpaid_shifts_count > 0,
    }


async def _get_finished_shifts(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> list[Shift]:
    conditions = [
        Shift.organization_id == org_id,
        Shift.status == ShiftStatus.finished,
    ]
    if user_id is not None:
        conditions.append(Shift.user_id == user_id)
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


async def get_org_payroll(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """Отчёт «сколько кому заплатить» за период (owner/admin).

    Учитываются все завершённые смены организации в периоде, включая смены
    исключённых сотрудников (их история ставок удалена каскадом → unpaid).
    Owner строкой items не фигурирует (ADR-001: owner != member).
    """
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id)

    validate_date_range(date_from, date_to)
    norm_from = ensure_utc(date_from) if date_from is not None else None
    norm_to = ensure_utc(date_to) if date_to is not None else None

    shifts = await _get_finished_shifts(
        session,
        org_id,
        date_from=norm_from,
        date_to=norm_to,
    )

    shifts_by_user: dict[uuid.UUID, list[Shift]] = defaultdict(list)
    for shift in shifts:
        shifts_by_user[shift.user_id].append(shift)

    user_ids = list(shifts_by_user)
    users_map: dict[uuid.UUID, str] = {}
    member_id_by_user: dict[uuid.UUID, uuid.UUID] = {}
    rates_by_member: dict[uuid.UUID, list[OrganizationMemberRate]] = {}
    if user_ids:
        users_result = await session.execute(
            select(User.id, User.name).where(User.id.in_(user_ids))
        )
        users_map = dict(users_result.tuples().all())

        members_result = await session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id.in_(user_ids),
            )
        )
        members = list(members_result.scalars().all())
        member_id_by_user = {m.user_id: m.id for m in members}
        rates_by_member = await _load_rates_asc(session, [m.id for m in members])

    items: list[dict[str, Any]] = []
    for uid, user_shifts in shifts_by_user.items():
        member_id = member_id_by_user.get(uid)
        rates_asc = rates_by_member.get(member_id, []) if member_id else []
        earnings = _calc_earnings(user_shifts, rates_asc)
        items.append(
            {
                "user_id": str(uid),
                "user_name": users_map.get(uid, "Unknown"),
                **earnings,
            }
        )
    items.sort(key=lambda item: (item["user_name"], item["user_id"]))

    totals = {
        "worked_seconds": sum(i["worked_seconds"] for i in items),
        "shifts_count": sum(i["shifts_count"] for i in items),
        "gross_amount_minor": sum(i["gross_amount_minor"] for i in items),
    }
    return {
        "period": {"date_from": norm_from, "date_to": norm_to},
        "currency": PAYROLL_CURRENCY,
        "items": items,
        "totals": totals,
    }


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
    await org_service.get_organization(session, org_id)

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

    earnings = _calc_earnings(shifts, rates_asc)
    current_rate = _rate_for_moment(rates_asc, datetime.now(UTC))

    return {
        "period": {"date_from": norm_from, "date_to": norm_to},
        "currency": PAYROLL_CURRENCY,
        "worked_seconds": earnings["worked_seconds"],
        "shifts_count": earnings["shifts_count"],
        "gross_amount_minor": earnings["gross_amount_minor"],
        "has_missing_rate": earnings["has_missing_rate"],
        "current_rate": current_rate,
    }
