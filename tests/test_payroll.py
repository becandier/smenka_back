# tests/test_payroll.py
"""Фича payroll: история ставок участников и расчёт зарплаты."""
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.member_rate import OrganizationMemberRate, RateType
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.shift import Pause, Shift, ShiftStatus
from src.app.models.user import User

RATE_EFF_JAN = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


async def _make_finished_shift(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    started_at: datetime,
    finished_at: datetime,
    status: ShiftStatus = ShiftStatus.finished,
) -> Shift:
    shift = Shift(
        user_id=user_id,
        organization_id=org_id,
        started_at=started_at,
        finished_at=finished_at if status == ShiftStatus.finished else None,
        status=status,
    )
    db_session.add(shift)
    await db_session.commit()
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


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="owner@example.com",
        password_hash=hash_password("Test1234"),
        name="Owner",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "Test1234"},
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="orgadmin@example.com",
        password_hash=hash_password("Test1234"),
        name="Org Admin",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def admin_headers(admin_user: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "orgadmin@example.com", "password": "Test1234"},
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="Payroll Org", owner_id=owner.id)
    db_session.add(organization)
    await db_session.commit()
    return organization


@pytest.fixture
async def employee_member(
    db_session: AsyncSession, org: Organization, verified_user: User,
) -> OrganizationMember:
    """verified_user (conftest) как employee организации."""
    member = OrganizationMember(
        organization_id=org.id,
        user_id=verified_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def admin_member(
    db_session: AsyncSession, org: Organization, admin_user: User,
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id,
        user_id=admin_user.id,
        role=MemberRole.admin,
    )
    db_session.add(member)
    await db_session.commit()
    return member


def _rates_url(org_id: Any, member_id: Any) -> str:
    return f"/api/v1/organizations/{org_id}/members/{member_id}/rates"


VALID_RATE_BODY = {
    "rate_amount_minor": 18000,
    "rate_type": "hourly",
    "effective_from": "2026-03-01T00:00:00Z",
    "note": "повышение",
}


class TestCreateRate:
    async def test_owner_creates_rate(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.post(
            _rates_url(org.id, employee_member.id),
            headers=owner_headers,
            json=VALID_RATE_BODY,
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["member_id"] == str(employee_member.id)
        assert data["rate_amount_minor"] == 18000
        assert data["rate_type"] == "hourly"
        assert data["currency"] == "RUB"
        assert data["effective_from"].startswith("2026-03-01T00:00:00")
        assert data["note"] == "повышение"
        assert data["id"]
        assert data["created_at"]

    async def test_admin_creates_rate(
        self,
        client: AsyncClient,
        admin_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
        admin_member: OrganizationMember,
    ) -> None:
        resp = await client.post(
            _rates_url(org.id, employee_member.id),
            headers=admin_headers,
            json={**VALID_RATE_BODY, "rate_type": "per_shift"},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["rate_type"] == "per_shift"

    async def test_employee_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.post(
            _rates_url(org.id, employee_member.id),
            headers=auth_headers,
            json=VALID_RATE_BODY,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_duplicate_effective_from(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        first = await client.post(
            _rates_url(org.id, employee_member.id),
            headers=owner_headers,
            json=VALID_RATE_BODY,
        )
        assert first.status_code == 201
        second = await client.post(
            _rates_url(org.id, employee_member.id),
            headers=owner_headers,
            json={**VALID_RATE_BODY, "rate_amount_minor": 20000},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "RATE_EFFECTIVE_FROM_TAKEN"

    async def test_non_positive_amount_validation(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.post(
            _rates_url(org.id, employee_member.id),
            headers=owner_headers,
            json={**VALID_RATE_BODY, "rate_amount_minor": 0},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_unknown_rate_type_validation(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.post(
            _rates_url(org.id, employee_member.id),
            headers=owner_headers,
            json={**VALID_RATE_BODY, "rate_type": "monthly"},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_member_not_found(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        resp = await client.post(
            _rates_url(org.id, uuid.uuid4()),
            headers=owner_headers,
            json=VALID_RATE_BODY,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "MEMBER_NOT_FOUND"

    async def test_member_of_other_org_not_found(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        owner: User,
        org: Organization,
        admin_user: User,
    ) -> None:
        """member_id чужой организации в пути этой org → 404."""
        other_org = Organization(name="Other Org", owner_id=owner.id)
        db_session.add(other_org)
        await db_session.flush()
        other_member = OrganizationMember(
            organization_id=other_org.id,
            user_id=admin_user.id,
            role=MemberRole.employee,
        )
        db_session.add(other_member)
        await db_session.commit()

        resp = await client.post(
            _rates_url(org.id, other_member.id),
            headers=owner_headers,
            json=VALID_RATE_BODY,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "MEMBER_NOT_FOUND"

    async def test_org_not_found(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.post(
            _rates_url(uuid.uuid4(), employee_member.id),
            headers=owner_headers,
            json=VALID_RATE_BODY,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ORG_NOT_FOUND"


class TestRateHistory:
    async def test_list_sorted_desc(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(
            db_session, employee_member.id, 15000,
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await _make_rate(
            db_session, employee_member.id, 18000,
            effective_from=datetime(2026, 3, 1, tzinfo=UTC),
        )
        await _make_rate(
            db_session, employee_member.id, 16000,
            effective_from=datetime(2026, 2, 1, tzinfo=UTC),
        )
        resp = await client.get(
            _rates_url(org.id, employee_member.id), headers=owner_headers,
        )
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert [i["rate_amount_minor"] for i in items] == [18000, 16000, 15000]

    async def test_employee_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.get(
            _rates_url(org.id, employee_member.id), headers=auth_headers,
        )
        assert resp.status_code == 403


class TestUpdateRate:
    async def test_fix_amount(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        rate = await _make_rate(db_session, employee_member.id, 18000)
        resp = await client.patch(
            f"{_rates_url(org.id, employee_member.id)}/{rate.id}",
            headers=owner_headers,
            json={"rate_amount_minor": 18500, "note": "опечатка"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["rate_amount_minor"] == 18500
        assert data["note"] == "опечатка"
        assert data["rate_type"] == "hourly"

    async def test_move_effective_from_to_taken_date(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(
            db_session, employee_member.id, 15000,
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        )
        rate2 = await _make_rate(
            db_session, employee_member.id, 18000,
            effective_from=datetime(2026, 3, 1, tzinfo=UTC),
        )
        resp = await client.patch(
            f"{_rates_url(org.id, employee_member.id)}/{rate2.id}",
            headers=owner_headers,
            json={"effective_from": "2026-01-01T00:00:00Z"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "RATE_EFFECTIVE_FROM_TAKEN"

    async def test_move_effective_from_to_free_date(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        rate = await _make_rate(db_session, employee_member.id, 18000)
        resp = await client.patch(
            f"{_rates_url(org.id, employee_member.id)}/{rate.id}",
            headers=owner_headers,
            json={"effective_from": "2026-03-02T00:00:00Z"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["effective_from"].startswith("2026-03-02T00:00:00")

    async def test_rate_not_found(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.patch(
            f"{_rates_url(org.id, employee_member.id)}/{uuid.uuid4()}",
            headers=owner_headers,
            json={"rate_amount_minor": 18500},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "RATE_NOT_FOUND"

    async def test_rate_of_other_member_not_found(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
        admin_member: OrganizationMember,
    ) -> None:
        rate = await _make_rate(db_session, admin_member.id, 18000)
        resp = await client.patch(
            f"{_rates_url(org.id, employee_member.id)}/{rate.id}",
            headers=owner_headers,
            json={"rate_amount_minor": 18500},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "RATE_NOT_FOUND"


class TestDeleteRate:
    async def test_delete_then_404(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        rate = await _make_rate(db_session, employee_member.id, 18000)
        url = f"{_rates_url(org.id, employee_member.id)}/{rate.id}"
        resp = await client.delete(url, headers=owner_headers)
        assert resp.status_code == 200
        assert resp.json()["data"] == {"deleted": True}

        again = await client.delete(url, headers=owner_headers)
        assert again.status_code == 404
        assert again.json()["error"]["code"] == "RATE_NOT_FOUND"


class TestCurrentRateInMembers:
    async def test_owner_sees_latest_effective_rate(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(
            db_session, employee_member.id, 15000,
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await _make_rate(
            db_session, employee_member.id, 18000,
            effective_from=datetime(2026, 3, 1, tzinfo=UTC),
        )
        # будущая ставка не действует
        await _make_rate(
            db_session, employee_member.id, 99000,
            effective_from=datetime(2030, 1, 1, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/members", headers=owner_headers,
        )
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        member_payload = next(
            i for i in items if i["id"] == str(employee_member.id)
        )
        assert member_payload["current_rate"] is not None
        assert member_payload["current_rate"]["rate_amount_minor"] == 18000
        assert member_payload["current_rate"]["rate_type"] == "hourly"
        assert member_payload["current_rate"]["currency"] == "RUB"

    async def test_future_only_rates_give_null(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(
            db_session, employee_member.id, 18000,
            effective_from=datetime(2030, 1, 1, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/members", headers=owner_headers,
        )
        items = resp.json()["data"]["items"]
        assert all(i["current_rate"] is None for i in items)

    async def test_no_rates_give_null(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/members", headers=owner_headers,
        )
        items = resp.json()["data"]["items"]
        assert all(i["current_rate"] is None for i in items)

    async def test_employee_never_sees_rates(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
        admin_member: OrganizationMember,
    ) -> None:
        """Ставки видят только owner/admin: для employee current_rate = null."""
        await _make_rate(db_session, employee_member.id, 18000)
        await _make_rate(db_session, admin_member.id, 25000)
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/members", headers=auth_headers,
        )
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 2
        assert all(i["current_rate"] is None for i in items)

    async def test_admin_sees_rates(
        self,
        client: AsyncClient,
        admin_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
        admin_member: OrganizationMember,
    ) -> None:
        await _make_rate(db_session, employee_member.id, 18000)
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/members", headers=admin_headers,
        )
        items = resp.json()["data"]["items"]
        member_payload = next(
            i for i in items if i["id"] == str(employee_member.id)
        )
        assert member_payload["current_rate"]["rate_amount_minor"] == 18000


class TestPayrollReport:
    async def test_hourly_calculation_and_totals(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """180 ₽/час (18000 коп.), 2ч + 1ч → 54000 коп."""
        await _make_rate(db_session, employee_member.id, 18000)
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 3, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 3, 11, 0, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll",
            headers=owner_headers,
            params={
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-30T23:59:59Z",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["currency"] == "RUB"
        assert data["period"]["date_from"].startswith("2026-06-01T00:00:00")
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["user_id"] == str(verified_user.id)
        assert item["worked_seconds"] == 10800
        assert item["shifts_count"] == 2
        assert item["gross_amount_minor"] == 54000
        assert item["unpaid_seconds"] == 0
        assert item["unpaid_shifts_count"] == 0
        assert item["has_missing_rate"] is False
        assert data["totals"] == {
            "worked_seconds": 10800,
            "shifts_count": 2,
            "gross_amount_minor": 54000,
        }

    async def test_rate_change_applies_by_shift_start(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """Каждая смена считается по ставке, действовавшей на её started_at."""
        await _make_rate(
            db_session, employee_member.id, 10000,
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await _make_rate(
            db_session, employee_member.id, 20000,
            effective_from=datetime(2026, 6, 2, tzinfo=UTC),
        )
        # 2ч по старой ставке + 1ч по новой
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 3, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 3, 11, 0, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=owner_headers,
        )
        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["gross_amount_minor"] == 2 * 10000 + 1 * 20000

    async def test_per_shift_rate_ignores_duration(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(
            db_session, employee_member.id, 50000, rate_type=RateType.per_shift,
        )
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 3, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 3, 10, 30, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=owner_headers,
        )
        item = resp.json()["data"]["items"][0]
        assert item["gross_amount_minor"] == 100000

    async def test_mixed_rate_types_in_history(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """rate_type меняется по истории: каждая смена — по типу своей ставки."""
        await _make_rate(
            db_session, employee_member.id, 10000,
            rate_type=RateType.hourly,
            effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        )
        await _make_rate(
            db_session, employee_member.id, 30000,
            rate_type=RateType.per_shift,
            effective_from=datetime(2026, 6, 2, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 3, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 3, 12, 0, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=owner_headers,
        )
        item = resp.json()["data"]["items"][0]
        assert item["gross_amount_minor"] == 10000 + 30000

    async def test_shift_before_first_rate_is_unpaid(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(
            db_session, employee_member.id, 18000,
            effective_from=datetime(2026, 6, 2, tzinfo=UTC),
        )
        # до первой ставки — неоплачиваемая
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 3, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 3, 11, 0, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=owner_headers,
        )
        item = resp.json()["data"]["items"][0]
        assert item["worked_seconds"] == 10800
        assert item["shifts_count"] == 2
        assert item["gross_amount_minor"] == 18000
        assert item["unpaid_seconds"] == 7200
        assert item["unpaid_shifts_count"] == 1
        assert item["has_missing_rate"] is True

    async def test_rounding_half_up_once_on_employee_total(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """Две смены по 0.5 коп. дробной части: построчное округление дало бы
        10002, единое округление итога — 10001."""
        await _make_rate(db_session, employee_member.id, 10001)
        for day in (1, 3):
            await _make_finished_shift(
                db_session, verified_user.id, org.id,
                datetime(2026, 6, day, 10, 0, tzinfo=UTC),
                datetime(2026, 6, day, 10, 30, tzinfo=UTC),
            )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=owner_headers,
        )
        item = resp.json()["data"]["items"][0]
        assert item["gross_amount_minor"] == 10001

    async def test_pauses_reduce_paid_time(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """worked_seconds — за вычетом пауз (calculate_worked_seconds)."""
        await _make_rate(db_session, employee_member.id, 36000)
        shift = await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        pause = Pause(
            shift_id=shift.id,
            started_at=datetime(2026, 6, 1, 10, 30, tzinfo=UTC),
            finished_at=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
        )
        db_session.add(pause)
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=owner_headers,
        )
        item = resp.json()["data"]["items"][0]
        assert item["worked_seconds"] == 5400
        assert item["gross_amount_minor"] == 54000

    async def test_only_finished_shifts_counted(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(db_session, employee_member.id, 18000)
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            status=ShiftStatus.active,
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=owner_headers,
        )
        assert resp.json()["data"]["items"] == []

    async def test_period_boundaries_inclusive(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_rate(db_session, employee_member.id, 18000)
        boundary = datetime(2026, 6, 5, 10, 0, tzinfo=UTC)
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            boundary,
            datetime(2026, 6, 5, 11, 0, tzinfo=UTC),
        )
        # вне окна
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 5, 1, 11, 0, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll",
            headers=owner_headers,
            params={
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": boundary.isoformat().replace("+00:00", "Z"),
            },
        )
        item = resp.json()["data"]["items"][0]
        assert item["shifts_count"] == 1

    async def test_removed_member_shifts_become_unpaid(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        """Исключение участника каскадом удаляет ставки → его смены unpaid."""
        await _make_rate(db_session, employee_member.id, 18000)
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        await db_session.delete(employee_member)
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=owner_headers,
        )
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["user_name"] == "Test User"
        assert items[0]["gross_amount_minor"] == 0
        assert items[0]["has_missing_rate"] is True

    async def test_employee_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=auth_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_org_not_found(
        self, client: AsyncClient, owner_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{uuid.uuid4()}/payroll", headers=owner_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ORG_NOT_FOUND"

    async def test_invalid_date_range(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll",
            headers=owner_headers,
            params={
                "date_from": "2026-06-05T00:00:00Z",
                "date_to": "2026-06-01T00:00:00Z",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"


class TestMyEarnings:
    async def test_employee_gets_own_earnings(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        admin_user: User,
        employee_member: OrganizationMember,
        admin_member: OrganizationMember,
    ) -> None:
        await _make_rate(db_session, employee_member.id, 18000)
        await _make_rate(db_session, admin_member.id, 99000)
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        # чужая смена не учитывается
        await _make_finished_shift(
            db_session, admin_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 18, 0, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/my-earnings",
            headers=auth_headers,
            params={
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-30T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["currency"] == "RUB"
        assert data["worked_seconds"] == 7200
        assert data["shifts_count"] == 1
        assert data["gross_amount_minor"] == 36000
        assert data["has_missing_rate"] is False
        assert data["current_rate"]["rate_amount_minor"] == 18000
        assert data["period"]["date_from"].startswith("2026-06-01T00:00:00")

    async def test_no_rates_zero_gross_missing_flag(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        org: Organization,
        verified_user: User,
        employee_member: OrganizationMember,
    ) -> None:
        await _make_finished_shift(
            db_session, verified_user.id, org.id,
            datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/my-earnings", headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["gross_amount_minor"] == 0
        assert data["worked_seconds"] == 7200
        assert data["has_missing_rate"] is True
        assert data["current_rate"] is None

    async def test_owner_forbidden(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        """Owner — не member (ADR-001): в my-earnings не участвует."""
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/my-earnings", headers=owner_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_non_member_forbidden(
        self,
        client: AsyncClient,
        admin_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        """Пользователь без членства (admin_member не создан) → 403."""
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/my-earnings", headers=admin_headers,
        )
        assert resp.status_code == 403

    async def test_org_not_found(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{uuid.uuid4()}/my-earnings", headers=auth_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ORG_NOT_FOUND"

    async def test_invalid_date_range(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        org: Organization,
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/my-earnings",
            headers=auth_headers,
            params={
                "date_from": "2026-06-05T00:00:00Z",
                "date_to": "2026-06-01T00:00:00Z",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"
