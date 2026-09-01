# tests/test_shift_history_earnings.py
"""Фича shift_history_earnings: блок `earnings` в GET /shifts и GET /shifts/{id}.

Правила расчёта — ADR-005 (`docs/decisions/005-earnings-calculation.md` в
корне-оркестраторе): ядро уже покрыто `tests/test_payroll.py`, здесь проверяется
только сам блок `earnings` — состав полей, null-кейсы, привязка штрафов/
корректировок к смене (в отличие от периода в my-earnings), округление на
уровне одной смены и батч-загрузка без N+1.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.adjustment import PayrollAdjustment
from src.app.models.member_rate import OrganizationMemberRate, RateType
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.penalty import Penalty
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.shift_overtime_request import OvertimeRequestStatus, ShiftOvertimeRequest
from src.app.models.user import User

RATE_EFF_JAN = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _data(resp: Any) -> Any:
    return resp.json()["data"]


async def _make_shift(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID | None,
    started_at: datetime,
    finished_at: datetime | None,
    status: ShiftStatus = ShiftStatus.finished,
) -> Shift:
    shift = Shift(
        user_id=user_id,
        organization_id=org_id,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
    )
    db_session.add(shift)
    await db_session.commit()
    await db_session.refresh(shift)
    return shift


async def _make_rate(
    db_session: AsyncSession,
    member_id: uuid.UUID,
    amount: int,
    rate_type: RateType = RateType.hourly,
    effective_from: datetime = RATE_EFF_JAN,
) -> OrganizationMemberRate:
    rate = OrganizationMemberRate(
        member_id=member_id,
        rate_amount_minor=amount,
        rate_type=rate_type,
        currency="RUB",
        effective_from=effective_from,
    )
    db_session.add(rate)
    await db_session.commit()
    return rate


async def _make_penalty(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    created_by: uuid.UUID,
    amount_minor: int,
    occurred_at: datetime,
    shift_id: uuid.UUID | None = None,
) -> Penalty:
    penalty = Penalty(
        organization_id=org_id,
        member_id=member_id,
        shift_id=shift_id,
        reason="Опоздание",
        amount_minor=amount_minor,
        occurred_at=occurred_at,
        created_by_user_id=created_by,
    )
    db_session.add(penalty)
    await db_session.commit()
    return penalty


async def _make_adjustment(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    created_by: uuid.UUID,
    amount_minor: int,
    occurred_at: datetime,
    shift_id: uuid.UUID | None = None,
) -> PayrollAdjustment:
    adjustment = PayrollAdjustment(
        organization_id=org_id,
        member_id=member_id,
        shift_id=shift_id,
        amount_minor=amount_minor,
        reason="Премия",
        occurred_at=occurred_at,
        created_by_user_id=created_by,
    )
    db_session.add(adjustment)
    await db_session.commit()
    return adjustment


# --- fixtures ------------------------------------------------------------------
@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="she_owner@example.com",
        password_hash=hash_password("Test1234"),
        name="Owner",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="Shift Earnings Org", owner_id=owner.id)
    db_session.add(organization)
    await db_session.commit()
    return organization


@pytest.fixture
async def employee_member(
    db_session: AsyncSession,
    org: Organization,
    verified_user: User,
) -> OrganizationMember:
    """verified_user (conftest) как employee организации — тот же пользователь,
    от имени которого дергается GET /shifts через auth_headers."""
    member = OrganizationMember(
        organization_id=org.id,
        user_id=verified_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return member


class TestEarningsBlockCalculation:
    async def test_hourly_shift_with_approved_overtime(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """hourly: gross = (worked+approved overtime)/3600 * rate, half-up;
        overtime_seconds — переработка, уже учтённая в gross."""
        await _make_rate(db_session, employee_member.id, 18000)
        shift = await _make_shift(
            db_session,
            verified_user.id,
            org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 19, 0, tzinfo=UTC),  # 9h worked
        )
        db_session.add(
            ShiftOvertimeRequest(
                shift_id=shift.id,
                minutes=30,
                comment="Задержался",
                status=OvertimeRequestStatus.approved,
            )
        )
        await db_session.commit()

        resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        assert resp.status_code == 200
        item = next(i for i in _data(resp)["items"] if i["id"] == str(shift.id))
        earnings = item["earnings"]
        assert earnings is not None
        assert earnings["currency"] == "RUB"
        assert earnings["has_rate"] is True
        assert earnings["overtime_seconds"] == 1800
        assert earnings["gross_amount_minor"] == round((9 * 3600 + 30 * 60) * 18000 / 3600)
        assert earnings["penalty_amount_minor"] == 0
        assert earnings["penalties_count"] == 0
        assert earnings["adjustment_amount_minor"] == 0
        assert earnings["adjustments_count"] == 0
        assert earnings["net_amount_minor"] == earnings["gross_amount_minor"]

    async def test_hourly_shift_without_overtime(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(db_session, employee_member.id, 18000)
        shift = await _make_shift(
            db_session,
            verified_user.id,
            org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),  # 2h
        )

        resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        item = next(i for i in _data(resp)["items"] if i["id"] == str(shift.id))
        earnings = item["earnings"]
        assert earnings is not None
        assert earnings["overtime_seconds"] == 0
        assert earnings["gross_amount_minor"] == 36000

    async def test_per_shift_rate_ignores_overtime(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """per_shift: сумма фиксирована, переработка не влияет на gross, но
        overtime_seconds всё равно отображается (ADR-005 п.2/семантика)."""
        await _make_rate(db_session, employee_member.id, 300000, rate_type=RateType.per_shift)
        shift = await _make_shift(
            db_session,
            verified_user.id,
            org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
        )
        db_session.add(
            ShiftOvertimeRequest(
                shift_id=shift.id,
                minutes=45,
                comment="Задержался",
                status=OvertimeRequestStatus.approved,
            )
        )
        await db_session.commit()

        resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        item = next(i for i in _data(resp)["items"] if i["id"] == str(shift.id))
        earnings = item["earnings"]
        assert earnings["gross_amount_minor"] == 300000
        assert earnings["overtime_seconds"] == 2700

    async def test_shift_without_active_rate_has_rate_false_gross_zero(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """Ставка есть, но начинает действовать ПОСЛЕ смены → has_rate=false,
        gross=0 (не путать с «заработал 0»)."""
        await _make_rate(
            db_session,
            employee_member.id,
            18000,
            effective_from=datetime(2026, 7, 1, tzinfo=UTC),
        )
        shift = await _make_shift(
            db_session,
            verified_user.id,
            org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )

        resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        item = next(i for i in _data(resp)["items"] if i["id"] == str(shift.id))
        earnings = item["earnings"]
        assert earnings["has_rate"] is False
        assert earnings["gross_amount_minor"] == 0
        assert earnings["net_amount_minor"] == 0

    async def test_linked_penalty_and_adjustment_go_into_shift_net(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        owner: User,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(db_session, employee_member.id, 18000)
        shift = await _make_shift(
            db_session,
            verified_user.id,
            org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),  # gross 36000
        )
        await _make_penalty(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            5000,
            shift.started_at,
            shift_id=shift.id,
        )
        await _make_adjustment(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            2000,
            shift.started_at,
            shift_id=shift.id,
        )

        resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        item = next(i for i in _data(resp)["items"] if i["id"] == str(shift.id))
        earnings = item["earnings"]
        assert earnings["gross_amount_minor"] == 36000
        assert earnings["penalty_amount_minor"] == 5000
        assert earnings["penalties_count"] == 1
        assert earnings["adjustment_amount_minor"] == 2000
        assert earnings["adjustments_count"] == 1
        assert earnings["net_amount_minor"] == 36000 - 5000 + 2000

    async def test_negative_net_when_penalty_exceeds_gross(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        owner: User,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(db_session, employee_member.id, 18000)
        shift = await _make_shift(
            db_session,
            verified_user.id,
            org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 11, 0, tzinfo=UTC),  # gross 18000
        )
        await _make_penalty(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            50000,
            shift.started_at,
            shift_id=shift.id,
        )

        resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        item = next(i for i in _data(resp)["items"] if i["id"] == str(shift.id))
        earnings = item["earnings"]
        assert earnings["net_amount_minor"] == 18000 - 50000
        assert earnings["net_amount_minor"] < 0

    async def test_unlinked_penalty_and_adjustment_excluded_from_shift_but_seen_in_my_earnings(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        owner: User,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """Штраф/корректировка без shift_id не попадают в earnings смены, но
        видны в итоге периода my-earnings (ADR-005 п.4)."""
        await _make_rate(db_session, employee_member.id, 18000)
        shift = await _make_shift(
            db_session,
            verified_user.id,
            org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        await _make_penalty(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            7000,
            datetime(2026, 6, 15, tzinfo=UTC),
            shift_id=None,
        )
        await _make_adjustment(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            3000,
            datetime(2026, 6, 15, tzinfo=UTC),
            shift_id=None,
        )

        resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        item = next(i for i in _data(resp)["items"] if i["id"] == str(shift.id))
        earnings = item["earnings"]
        assert earnings["penalty_amount_minor"] == 0
        assert earnings["penalties_count"] == 0
        assert earnings["adjustment_amount_minor"] == 0
        assert earnings["adjustments_count"] == 0
        assert earnings["net_amount_minor"] == earnings["gross_amount_minor"]

        my_earnings = await client.get(
            f"/api/v1/organizations/{org.id}/my-earnings",
            headers=auth_headers,
            params={
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-30T23:59:59Z",
            },
        )
        earnings_period = _data(my_earnings)
        assert earnings_period["penalty_amount_minor"] == 7000
        assert earnings_period["adjustment_amount_minor"] == 3000

    async def test_personal_shift_earnings_null(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        verified_user: User,
    ) -> None:
        shift = await _make_shift(
            db_session,
            verified_user.id,
            None,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )

        resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        item = next(i for i in _data(resp)["items"] if i["id"] == str(shift.id))
        assert "earnings" in item
        assert item["earnings"] is None

    @pytest.mark.parametrize("status", [ShiftStatus.active, ShiftStatus.paused])
    async def test_non_finished_org_shift_earnings_null(
        self,
        status: ShiftStatus,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(db_session, employee_member.id, 18000)
        shift = await _make_shift(
            db_session,
            verified_user.id,
            org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            None,
            status=status,
        )

        resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        item = next(i for i in _data(resp)["items"] if i["id"] == str(shift.id))
        assert item["earnings"] is None

    async def test_shift_detail_endpoint_returns_same_earnings(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        owner: User,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(db_session, employee_member.id, 18000)
        shift = await _make_shift(
            db_session,
            verified_user.id,
            org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        await _make_penalty(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            1000,
            shift.started_at,
            shift_id=shift.id,
        )

        resp = await client.get(f"/api/v1/shifts/{shift.id}", headers=auth_headers)
        assert resp.status_code == 200
        earnings = _data(resp)["earnings"]
        assert earnings is not None
        assert earnings["gross_amount_minor"] == 36000
        assert earnings["penalty_amount_minor"] == 1000
        assert earnings["net_amount_minor"] == 35000

    async def test_rounding_shift_net_sum_matches_my_earnings_totals(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        owner: User,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """ADR-005 п.5 + приёмка backend.md: когда все штрафы/корректировки
        привязаны к сменам, сумма net_amount_minor по сменам периода совпадает
        с gross − penalties + adjustments из my-earnings за тот же период."""
        await _make_rate(db_session, employee_member.id, 10001)  # дробные копейки
        shifts = []
        for day in (1, 2, 3):
            shift = await _make_shift(
                db_session,
                verified_user.id,
                org.id,
                datetime(2026, 6, day, 10, 0, tzinfo=UTC),
                datetime(2026, 6, day, 10, 30, tzinfo=UTC),
            )
            shifts.append(shift)

        await _make_penalty(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            100,
            shifts[0].started_at,
            shift_id=shifts[0].id,
        )
        await _make_adjustment(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            50,
            shifts[0].started_at,
            shift_id=shifts[0].id,
        )
        await _make_penalty(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            200,
            shifts[1].started_at,
            shift_id=shifts[1].id,
        )
        await _make_adjustment(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            -30,
            shifts[2].started_at,
            shift_id=shifts[2].id,
        )

        list_resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        items = _data(list_resp)["items"]
        shift_ids = {str(s.id) for s in shifts}
        net_sum = sum(i["earnings"]["net_amount_minor"] for i in items if i["id"] in shift_ids)

        my_earnings = await client.get(
            f"/api/v1/organizations/{org.id}/my-earnings",
            headers=auth_headers,
            params={
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-30T23:59:59Z",
            },
        )
        period = _data(my_earnings)
        expected_net = (
            period["gross_amount_minor"]
            - period["penalty_amount_minor"]
            + period["adjustment_amount_minor"]
        )
        assert net_sum == expected_net
        # и сам gross тоже уже построчно округлён (ADR-005 п.5): 3×(5000.5→5001)
        assert period["gross_amount_minor"] == 15003


class TestEarningsNoNPlusOne:
    async def test_list_shifts_earnings_batched_regardless_of_page_size(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        org: Organization,
        owner: User,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """Ставки/переработка/штрафы/корректировки читаются батчами на всю
        страницу (backend.md, «Производительность») — ровно по одному запросу
        к каждой из трёх таблиц (organization_member_rates/penalties/
        payroll_adjustments), независимо от числа смен на странице."""
        await _make_rate(db_session, employee_member.id, 18000)
        shifts = []
        base = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
        for i in range(15):
            shift = await _make_shift(
                db_session,
                verified_user.id,
                org.id,
                base + timedelta(days=i),
                base + timedelta(days=i, hours=8),
            )
            shifts.append(shift)
        await _make_penalty(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            1000,
            shifts[0].started_at,
            shift_id=shifts[0].id,
        )
        await _make_adjustment(
            db_session,
            org.id,
            employee_member.id,
            owner.id,
            500,
            shifts[1].started_at,
            shift_id=shifts[1].id,
        )
        db_session.add(
            ShiftOvertimeRequest(
                shift_id=shifts[2].id,
                minutes=20,
                comment="Задержался",
                status=OvertimeRequestStatus.approved,
            )
        )
        await db_session.commit()

        counts = {"rates": 0, "penalties": 0, "adjustments": 0}

        def on_execute(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
            low = statement.lower()
            if "organization_member_rates" in low:
                counts["rates"] += 1
            if "from penalties" in low or "into penalties" in low:
                counts["penalties"] += 1
            if "payroll_adjustments" in low:
                counts["adjustments"] += 1

        sync_engine = db_session.bind.sync_engine
        event.listen(sync_engine, "before_cursor_execute", on_execute)
        try:
            resp = await client.get("/api/v1/shifts", headers=auth_headers, params={"limit": 100})
        finally:
            event.remove(sync_engine, "before_cursor_execute", on_execute)

        assert resp.status_code == 200
        assert len(_data(resp)["items"]) == 15
        # Ровно один batch-запрос на таблицу вне зависимости от 15 смен на странице —
        # N+1 добавил бы запрос на каждую смену.
        assert counts["rates"] == 1, f"Expected exactly 1 rates query, got {counts['rates']}"
        assert counts["penalties"] == 1, (
            f"Expected exactly 1 penalties query, got {counts['penalties']}"
        )
        assert counts["adjustments"] == 1, (
            f"Expected exactly 1 adjustments query, got {counts['adjustments']}"
        )
