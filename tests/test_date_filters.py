# tests/test_date_filters.py
"""Фича date_filters: валидация диапазона дат в списках и кастомное окно stats."""
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _make_finished_shift(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    started_at: datetime,
    finished_at: datetime,
    organization_id: uuid.UUID | None = None,
) -> Shift:
    shift = Shift(
        user_id=user_id,
        organization_id=organization_id,
        started_at=started_at,
        finished_at=finished_at,
        status=ShiftStatus.finished,
    )
    db_session.add(shift)
    await db_session.commit()
    return shift


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
async def org(db_session: AsyncSession, owner: User, verified_user: User) -> Organization:
    """Организация: owner + verified_user (employee)."""
    organization = Organization(name="Date Filters Org", owner_id=owner.id)
    db_session.add(organization)
    await db_session.flush()
    member = OrganizationMember(
        organization_id=organization.id,
        user_id=verified_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return organization


class TestShiftListDateRange:
    async def test_invalid_date_range(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={
                "date_from": "2026-06-05T00:00:00Z",
                "date_to": "2026-06-01T00:00:00Z",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"

    async def test_open_range_only_date_from(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"date_from": "2026-06-01T00:00:00Z"},
        )
        assert resp.status_code == 200

    async def test_open_range_only_date_to(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"date_to": "2026-06-01T00:00:00Z"},
        )
        assert resp.status_code == 200

    async def test_equal_boundaries_inclusive(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        """date_from == date_to: точечное окно по started_at включает смену."""
        point = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        await _make_finished_shift(
            db_session, verified_user.id, point, datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"date_from": _iso(point), "date_to": _iso(point)},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["started_at"].startswith("2026-06-01T10:00:00")

    async def test_range_combines_with_status_filter(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={
                "status": "active",
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-02T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 0


class TestOrgShiftListDateRange:
    async def test_invalid_date_range(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={
                "date_from": "2026-06-05T00:00:00Z",
                "date_to": "2026-06-01T00:00:00Z",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"

    async def test_valid_range_filters_org_shifts(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            organization_id=org.id,
        )
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            organization_id=org.id,
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-02T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 1

    async def test_open_range_only_date_from(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            organization_id=org.id,
        )
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            organization_id=org.id,
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={"date_from": "2026-06-01T00:00:00Z"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 1

    async def test_range_combines_with_user_id_filter(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Диапазон и user_id комбинируются как AND."""
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            organization_id=org.id,
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={
                "user_id": str(uuid.uuid4()),
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-02T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 0

    async def test_org_not_found(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{uuid.uuid4()}/shifts",
            headers=owner_headers,
            params={"date_from": "2026-06-01T00:00:00Z"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ORG_NOT_FOUND"


class TestUtcNormalization:
    async def test_naive_datetime_treated_as_utc_in_list(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Naive-границы (без зоны) трактуются как UTC, а не как локаль сервера.

        Смена начата в 22:00 UTC: при UTC-семантике окно
        [2026-06-01T00:00:00, 2026-06-02T00:00:00] её включает; при трактовке
        границ как локального времени сервера (восточнее UTC) — нет.
        """
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 22, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 23, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={
                "date_from": "2026-06-01T00:00:00",
                "date_to": "2026-06-02T00:00:00",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 1

    async def test_naive_datetime_treated_as_utc_in_stats(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 22, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 23, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={
                "date_from": "2026-06-01T00:00:00",
                "date_to": "2026-06-02T00:00:00",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["shift_count"] == 1
        assert _parse(data["range_from"]) == datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)

    async def test_non_utc_offset_normalized_in_stats_echo(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Границы с не-UTC оффсетом приводятся к UTC в фильтре и эхо-полях."""
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={
                # тот же момент, что 2026-06-01T00:00:00Z / 2026-06-02T00:00:00Z
                "date_from": "2026-06-01T03:00:00+03:00",
                "date_to": "2026-06-02T03:00:00+03:00",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["shift_count"] == 1
        range_from = _parse(data["range_from"])
        assert range_from == datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        assert range_from.utcoffset().total_seconds() == 0


class TestPersonalStatsWindow:
    async def test_preset_still_works_with_ranges_filled(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            "/api/v1/shifts/stats", headers=auth_headers, params={"period": "day"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["period"] == "day"
        assert data["total_worked_seconds"] == 0
        assert data["shift_count"] == 0
        assert data["average_shift_seconds"] == 0
        range_from = _parse(data["range_from"])
        range_to = _parse(data["range_to"])
        assert range_from == range_from.replace(hour=0, minute=0, second=0, microsecond=0)
        assert range_to >= range_from

    async def test_custom_range_aggregates(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        # 2 часа + 1 час в окне, по одной смене до и после окна
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 3, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 3, 11, 0, 0, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 5, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-04T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["period"] is None
        assert data["total_worked_seconds"] == 10800
        assert data["shift_count"] == 2
        assert data["average_shift_seconds"] == 5400
        assert _parse(data["range_from"]) == datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        assert _parse(data["range_to"]) == datetime(2026, 6, 4, 0, 0, 0, tzinfo=UTC)

    async def test_custom_range_inclusive_boundaries(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Смены, начатые ровно в date_from и ровно в date_to, входят в окно."""
        date_from = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        date_to = datetime(2026, 6, 2, 10, 0, 0, tzinfo=UTC)
        await _make_finished_shift(
            db_session, verified_user.id, date_from, datetime(2026, 6, 1, 11, 0, 0, tzinfo=UTC),
        )
        await _make_finished_shift(
            db_session, verified_user.id, date_to, datetime(2026, 6, 2, 11, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"date_from": _iso(date_from), "date_to": _iso(date_to)},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["shift_count"] == 2

    async def test_shift_started_before_window_excluded(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Граница по началу смены: начата до окна, завершена в окне → не входит."""
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 5, 31, 22, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 6, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-02T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["shift_count"] == 0

    async def test_open_range_only_date_from(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"date_from": "2026-06-01T00:00:00Z"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["period"] is None
        assert data["shift_count"] == 1
        assert data["range_from"] is not None
        assert data["range_to"] is None

    async def test_open_range_only_date_to(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        )
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"date_to": "2026-06-02T00:00:00Z"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["shift_count"] == 1
        assert data["range_from"] is None
        assert data["range_to"] is not None

    async def test_missing_stats_range(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get("/api/v1/shifts/stats", headers=auth_headers)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "MISSING_STATS_RANGE"

    async def test_ambiguous_stats_range(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day", "date_from": "2026-06-01T00:00:00Z"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "AMBIGUOUS_STATS_RANGE"

    async def test_ambiguous_wins_over_invalid_period(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        """Порядок валидации: AMBIGUOUS_STATS_RANGE раньше INVALID_PERIOD."""
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "year", "date_from": "2026-06-01T00:00:00Z"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "AMBIGUOUS_STATS_RANGE"

    async def test_invalid_period(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            "/api/v1/shifts/stats", headers=auth_headers, params={"period": "year"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PERIOD"

    async def test_invalid_date_range(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={
                "date_from": "2026-06-05T00:00:00Z",
                "date_to": "2026-06-01T00:00:00Z",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"

    async def test_empty_window_returns_zeros(
        self, client: AsyncClient, auth_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={
                "date_from": "2030-01-01T00:00:00Z",
                "date_to": "2030-01-31T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total_worked_seconds"] == 0
        assert data["shift_count"] == 0
        assert data["average_shift_seconds"] == 0


class TestOrgStatsWindow:
    async def test_preset_still_works(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/stats",
            headers=owner_headers,
            params={"period": "month"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["period"] == "month"
        assert data["per_employee"] == []
        assert data["range_from"] is not None
        assert data["range_to"] is not None

    async def test_custom_range_aggregates_per_employee(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            organization_id=org.id,
        )
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 5, 20, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC),
            organization_id=org.id,
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/stats",
            headers=owner_headers,
            params={
                "date_from": "2026-06-01T00:00:00Z",
                "date_to": "2026-06-02T00:00:00Z",
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["period"] is None
        assert data["shift_count"] == 1
        assert data["total_worked_seconds"] == 7200
        assert len(data["per_employee"]) == 1
        employee = data["per_employee"][0]
        assert employee["user_id"] == str(verified_user.id)
        assert employee["total_worked_seconds"] == 7200

    async def test_open_range_only_date_to(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
        verified_user: User,
        db_session: AsyncSession,
    ) -> None:
        await _make_finished_shift(
            db_session,
            verified_user.id,
            datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            organization_id=org.id,
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/stats",
            headers=owner_headers,
            params={"date_to": "2026-06-02T00:00:00Z"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["period"] is None
        assert data["shift_count"] == 1
        assert data["range_from"] is None
        assert _parse(data["range_to"]) == datetime(2026, 6, 2, 0, 0, 0, tzinfo=UTC)

    async def test_invalid_period(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/stats",
            headers=owner_headers,
            params={"period": "year"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_PERIOD"

    async def test_missing_stats_range(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/stats", headers=owner_headers,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "MISSING_STATS_RANGE"

    async def test_ambiguous_stats_range(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/stats",
            headers=owner_headers,
            params={"period": "week", "date_to": "2026-06-02T00:00:00Z"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "AMBIGUOUS_STATS_RANGE"

    async def test_invalid_date_range(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/stats",
            headers=owner_headers,
            params={
                "date_from": "2026-06-05T00:00:00Z",
                "date_to": "2026-06-01T00:00:00Z",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"

    async def test_employee_forbidden(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/stats",
            headers=auth_headers,
            params={"date_from": "2026-06-01T00:00:00Z"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_org_not_found(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{uuid.uuid4()}/stats",
            headers=owner_headers,
            params={"period": "day"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ORG_NOT_FOUND"
