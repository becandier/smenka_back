# tests/test_payroll_location_extras.py
"""payroll_location_filter_extras: фильтр отчёта по точке сужает штрафы и начисления.

Запись (штраф/ручное начисление) учитывается по точке привязанной смены
(`shift_id` → `shifts.work_location_id`, удалённость смены не важна); запись
без смены или по смене без точки — только при `none`. Без фильтра — все записи.
"""

import uuid
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.adjustment import PayrollAdjustment
from src.app.models.member_rate import OrganizationMemberRate, RateType
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.penalty import Penalty
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User
from src.app.models.work_location import WorkLocation
from src.app.services import adjustment as adjustment_service
from src.app.services import penalty as penalty_service

RATE_EFF_JAN = datetime(2026, 1, 1, tzinfo=UTC)
JUNE = {"date_from": "2026-06-01T00:00:00Z", "date_to": "2026-06-30T23:59:59Z"}
JUNE_FROM = datetime(2026, 6, 1, tzinfo=UTC)
JUNE_TO = datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC)


async def _make_user(db_session: AsyncSession, email: str, name: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("Test1234"),
        name=name,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "plx_owner@example.com", "Owner")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "plx_owner@example.com", "password": "Test1234"}
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
async def org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="Location Extras Org", owner_id=owner.id)
    db_session.add(organization)
    await db_session.commit()
    return organization


async def _make_member(
    db_session: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID
) -> OrganizationMember:
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=MemberRole.employee)
    db_session.add(member)
    await db_session.commit()
    db_session.add(
        OrganizationMemberRate(
            member_id=member.id,
            rate_amount_minor=10000,
            rate_type=RateType.hourly,
            currency="RUB",
            effective_from=RATE_EFF_JAN,
        )
    )
    await db_session.commit()
    return member


async def _make_location(db_session: AsyncSession, org_id: uuid.UUID, name: str) -> WorkLocation:
    loc = WorkLocation(
        organization_id=org_id, name=name, latitude=55.75, longitude=37.62, radius_meters=100
    )
    db_session.add(loc)
    await db_session.commit()
    return loc


async def _make_shift(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    day: int,
    hours: int,
    location_id: uuid.UUID | None,
    *,
    is_deleted: bool = False,
) -> Shift:
    shift = Shift(
        user_id=user_id,
        organization_id=org_id,
        started_at=datetime(2026, 6, day, 9, 0, tzinfo=UTC),
        finished_at=datetime(2026, 6, day, 9 + hours, 0, tzinfo=UTC),
        status=ShiftStatus.finished,
        work_location_id=location_id,
        is_deleted=is_deleted,
    )
    db_session.add(shift)
    await db_session.commit()
    return shift


async def _make_extras(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    created_by: uuid.UUID,
    shift_id: uuid.UUID | None,
    adjustment_minor: int,
    penalty_minor: int,
    label: str,
) -> None:
    """Пара «начисление + штраф» с одной привязкой к смене."""
    occurred = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    db_session.add(
        PayrollAdjustment(
            organization_id=org_id,
            member_id=member_id,
            shift_id=shift_id,
            reason=f"Доплата {label}",
            amount_minor=adjustment_minor,
            occurred_at=occurred,
            created_by_user_id=created_by,
        )
    )
    db_session.add(
        Penalty(
            organization_id=org_id,
            member_id=member_id,
            shift_id=shift_id,
            reason=f"Штраф {label}",
            amount_minor=penalty_minor,
            occurred_at=occurred,
            created_by_user_id=created_by,
        )
    )
    await db_session.commit()


@pytest.fixture
async def seeded(db_session: AsyncSession, org: Organization, owner: User) -> dict[str, Any]:
    """Alice работает на A, B и без точки; Boris — только на B.

    Начисления/штрафы (суммы — разные степени двойки, чтобы сумма однозначно
    говорила, какие записи попали):
      Alice: смена A — 1000/100; смена B — 2000/200; без смены — 4000/400;
             смена без точки — 8000/800; удалённая смена на A — 16000/1600.
      Boris: смена B — 32000/3200.
    """
    alice = await _make_user(db_session, "plx_alice@example.com", "Alice")
    boris = await _make_user(db_session, "plx_boris@example.com", "Boris")
    alice_m = await _make_member(db_session, org.id, alice.id)
    boris_m = await _make_member(db_session, org.id, boris.id)
    loc_a = await _make_location(db_session, org.id, "Точка A")
    loc_b = await _make_location(db_session, org.id, "Точка B")

    shift_a = await _make_shift(db_session, alice.id, org.id, 1, 2, loc_a.id)
    shift_b = await _make_shift(db_session, alice.id, org.id, 2, 1, loc_b.id)
    shift_none = await _make_shift(db_session, alice.id, org.id, 3, 1, None)
    shift_a_deleted = await _make_shift(
        db_session, alice.id, org.id, 4, 1, loc_a.id, is_deleted=True
    )
    boris_b = await _make_shift(db_session, boris.id, org.id, 5, 1, loc_b.id)

    await _make_extras(db_session, org.id, alice_m.id, owner.id, shift_a.id, 1000, 100, "A")
    await _make_extras(db_session, org.id, alice_m.id, owner.id, shift_b.id, 2000, 200, "B")
    await _make_extras(db_session, org.id, alice_m.id, owner.id, None, 4000, 400, "без смены")
    await _make_extras(
        db_session, org.id, alice_m.id, owner.id, shift_none.id, 8000, 800, "без точки"
    )
    await _make_extras(
        db_session, org.id, alice_m.id, owner.id, shift_a_deleted.id, 16000, 1600, "удалённая A"
    )
    await _make_extras(
        db_session, org.id, boris_m.id, owner.id, boris_b.id, 32000, 3200, "Boris B"
    )
    return {"alice": alice, "boris": boris, "loc_a": loc_a, "loc_b": loc_b}


async def _report(
    client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID, location_ids: str | None
) -> dict[str, Any]:
    params: dict[str, Any] = dict(JUNE)
    if location_ids is not None:
        params["location_ids"] = location_ids
    resp = await client.get(
        f"/api/v1/organizations/{org_id}/payroll", headers=headers, params=params
    )
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()["data"]
    return data


def _by_name(report: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    """Имя → (смен, начисления, штрафы)."""
    return {
        i["user_name"]: (
            i["shifts_count"],
            i["adjustment_amount_minor"],
            i["penalty_amount_minor"],
        )
        for i in report["items"]
    }


async def test_location_filter_narrows_adjustments_and_penalties(
    client: AsyncClient, owner_headers: dict[str, str], org: Organization, seeded: dict[str, Any]
) -> None:
    loc_a, loc_b = seeded["loc_a"].id, seeded["loc_b"].id

    # Точка A: Boris (только B) не попадает; у Alice — только записи смен на A,
    # включая привязанную к удалённой смене на A.
    report_a = await _report(client, owner_headers, org.id, str(loc_a))
    assert _by_name(report_a) == {"Alice": (1, 1000 + 16000, 100 + 1600)}
    assert report_a["totals"]["adjustment_amount_minor"] == 17000
    assert report_a["totals"]["penalty_amount_minor"] == 1700
    assert report_a["totals"]["net_amount_minor"] == 20000 - 1700 + 17000

    # Точка B: обоим — только записи смен на B.
    report_b = await _report(client, owner_headers, org.id, str(loc_b))
    assert _by_name(report_b) == {"Alice": (1, 2000, 200), "Boris": (1, 32000, 3200)}

    # none: записи без смены и по сменам без точки.
    report_none = await _report(client, owner_headers, org.id, "none")
    assert _by_name(report_none) == {"Alice": (1, 4000 + 8000, 400 + 800)}

    # A,none — объединение.
    report_a_none = await _report(client, owner_headers, org.id, f"{loc_a},none")
    assert _by_name(report_a_none) == {"Alice": (2, 1000 + 16000 + 4000 + 8000, 2900)}

    # Без фильтра — все записи периода, как раньше.
    report_all = await _report(client, owner_headers, org.id, None)
    assert _by_name(report_all) == {"Alice": (3, 31000, 3100), "Boris": (1, 32000, 3200)}

    # Сумма по точкам + none = отчёт без фильтра (без задвоений и потерь).
    for key in ("adjustment_amount_minor", "penalty_amount_minor", "net_amount_minor"):
        parts = sum(r["totals"][key] for r in (report_a, report_b, report_none))
        assert parts == report_all["totals"][key], key


async def test_location_filter_employee_with_only_foreign_extras_absent(
    client: AsyncClient, owner_headers: dict[str, str], org: Organization, seeded: dict[str, Any]
) -> None:
    """Сценарий жалобы: у сотрудника нет смен на точке, только доплата «чужой» точки."""
    loc_a = seeded["loc_a"].id
    report = await _report(client, owner_headers, org.id, str(loc_a))
    assert "Boris" not in _by_name(report)
    assert all(i["shifts_count"] > 0 for i in report["items"])


async def test_service_filter_is_optional_for_other_consumers(
    db_session: AsyncSession, org: Organization, seeded: dict[str, Any]
) -> None:
    """Без новых аргументов функции возвращают все записи периода."""
    adjustments = await adjustment_service.fetch_report_adjustments(
        db_session, org.id, date_from=JUNE_FROM, date_to=JUNE_TO
    )
    penalties = await penalty_service.fetch_report_penalties(
        db_session, org.id, date_from=JUNE_FROM, date_to=JUNE_TO
    )
    assert sum(r.amount_minor for r in adjustments) == 63000
    assert sum(r.amount_minor for r in penalties) == 6300

    only_b = await adjustment_service.fetch_report_adjustments(
        db_session,
        org.id,
        date_from=JUNE_FROM,
        date_to=JUNE_TO,
        location_ids=[seeded["loc_b"].id],
    )
    assert sorted(r.amount_minor for r in only_b) == [2000, 32000]


async def test_export_location_filter_narrows_operation_sheets(
    client: AsyncClient, owner_headers: dict[str, str], org: Organization, seeded: dict[str, Any]
) -> None:
    resp = await client.get(
        f"/api/v1/organizations/{org.id}/payroll/export",
        headers=owner_headers,
        params={**JUNE, "location_ids": str(seeded["loc_a"].id)},
    )
    assert resp.status_code == 200, resp.text
    wb = load_workbook(BytesIO(resp.content))

    adj_rows = list(wb["Начисления и удержания"].iter_rows(values_only=True))[1:]
    *adj_body, adj_total = adj_rows
    assert {r[0] for r in adj_body} == {"Alice"}
    assert sorted(r[3] for r in adj_body) == ["Доплата A", "Доплата удалённая A"]
    assert adj_total[0] == "ИТОГО"
    assert adj_total[5] == 170.0

    pen_rows = list(wb["Штрафы"].iter_rows(values_only=True))[1:]
    *pen_body, pen_total = pen_rows
    assert {r[0] for r in pen_body} == {"Alice"}
    assert len(pen_body) == 2
    assert pen_total[0] == "ИТОГО"
    assert pen_total[4] == 17.0

    summary_names = {
        r[0] for r in wb["Сводка"].iter_rows(values_only=True) if r and r[0] in {"Alice", "Boris"}
    }
    assert summary_names == {"Alice"}
