# tests/test_org_shifts.py
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import Organization, OrganizationMember, MemberRole
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.user import User
from src.app.models.work_location import WorkLocation


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
async def employee_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="employee@example.com",
        password_hash=hash_password("Test1234"),
        name="Employee",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def employee_headers(employee_user: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "employee@example.com", "password": "Test1234"},
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def org_with_geo(
    db_session: AsyncSession, owner: User, employee_user: User,
) -> Organization:
    """Org with geo_check_enabled and a work location in Moscow."""
    org = Organization(name="Geo Org", owner_id=owner.id)
    db_session.add(org)
    await db_session.flush()

    settings = OrganizationSettings(
        organization_id=org.id,
        geo_check_enabled=True,
    )
    db_session.add(settings)

    location = WorkLocation(
        organization_id=org.id,
        name="Office",
        latitude=55.7558,
        longitude=37.6173,
        radius_meters=200,
    )
    db_session.add(location)

    member = OrganizationMember(
        organization_id=org.id,
        user_id=employee_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return org


@pytest.fixture
async def org_no_geo(
    db_session: AsyncSession, owner: User, employee_user: User,
) -> Organization:
    """Org without geo check."""
    org = Organization(name="No Geo Org", owner_id=owner.id)
    db_session.add(org)
    await db_session.flush()

    settings = OrganizationSettings(organization_id=org.id, geo_check_enabled=False)
    db_session.add(settings)

    member = OrganizationMember(
        organization_id=org.id,
        user_id=employee_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return org


class TestOrgShiftStart:
    async def test_start_org_shift_within_radius(
        self, client: AsyncClient, employee_headers: dict, org_with_geo: Organization,
    ):
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={
                "organization_id": str(org_with_geo.id),
                "latitude": 55.7560,
                "longitude": 37.6175,
            },
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["organization_id"] == str(org_with_geo.id)
        assert data["status"] == "active"

    async def test_start_org_shift_outside_radius(
        self, client: AsyncClient, employee_headers: dict, org_with_geo: Organization,
    ):
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={
                "organization_id": str(org_with_geo.id),
                "latitude": 56.0,
                "longitude": 38.0,
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "GEO_CHECK_FAILED"

    async def test_start_org_shift_no_geo_check(
        self, client: AsyncClient, employee_headers: dict, org_no_geo: Organization,
    ):
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_no_geo.id)},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["organization_id"] == str(org_no_geo.id)

    async def test_start_org_shift_geo_enabled_no_coords(
        self, client: AsyncClient, employee_headers: dict, org_with_geo: Organization,
    ):
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_with_geo.id)},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "COORDS_REQUIRED"

    async def test_non_member_cannot_start_org_shift(
        self, client: AsyncClient, auth_headers: dict, org_with_geo: Organization,
    ):
        """verified_user (from conftest) is not a member of the org."""
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=auth_headers,
            json={
                "organization_id": str(org_with_geo.id),
                "latitude": 55.7560,
                "longitude": 37.6175,
            },
        )
        assert resp.status_code == 403

    async def test_personal_and_org_shift_simultaneously(
        self, client: AsyncClient, employee_headers: dict, org_no_geo: Organization,
    ):
        # Start personal shift
        resp1 = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={},
        )
        assert resp1.status_code == 201
        assert resp1.json()["data"]["organization_id"] is None

        # Start org shift — should succeed
        resp2 = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_no_geo.id)},
        )
        assert resp2.status_code == 201
        assert resp2.json()["data"]["organization_id"] == str(org_no_geo.id)

    async def test_cannot_start_second_org_shift_same_org(
        self, client: AsyncClient, employee_headers: dict, org_no_geo: Organization,
    ):
        await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_no_geo.id)},
        )
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_no_geo.id)},
        )
        assert resp.status_code == 409


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "Test1234"},
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def org_with_limits(
    db_session: AsyncSession, owner: User, employee_user: User,
) -> Organization:
    """Org with pause limits: max 2 pauses, max 5 minutes per pause."""
    org = Organization(name="Limits Org", owner_id=owner.id)
    db_session.add(org)
    await db_session.flush()

    settings = OrganizationSettings(
        organization_id=org.id,
        geo_check_enabled=False,
        max_pause_minutes=5,
        max_pauses_per_shift=2,
    )
    db_session.add(settings)

    member = OrganizationMember(
        organization_id=org.id,
        user_id=employee_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return org


class TestPauseLimits:
    async def test_max_pauses_per_shift(
        self, client: AsyncClient, employee_headers: dict, org_with_limits: Organization,
    ):
        # Start org shift
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_with_limits.id)},
        )
        shift_id = resp.json()["data"]["id"]

        # Pause 1
        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=employee_headers)
        await client.post(f"/api/v1/shifts/{shift_id}/resume", headers=employee_headers)

        # Pause 2
        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=employee_headers)
        await client.post(f"/api/v1/shifts/{shift_id}/resume", headers=employee_headers)

        # Pause 3 — should fail
        resp = await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=employee_headers)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "MAX_PAUSES_REACHED"

    async def test_personal_shift_no_pause_limits(
        self, client: AsyncClient, employee_headers: dict,
    ):
        # Start personal shift
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={},
        )
        shift_id = resp.json()["data"]["id"]

        # Should allow unlimited pauses
        for _ in range(5):
            resp_p = await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=employee_headers)
            assert resp_p.status_code == 200
            resp_r = await client.post(f"/api/v1/shifts/{shift_id}/resume", headers=employee_headers)
            assert resp_r.status_code == 200


class TestAutoFinishStalePauses:
    async def test_stale_pause_auto_finished(
        self,
        client: AsyncClient,
        employee_headers: dict,
        org_with_limits: Organization,
        db_session: AsyncSession,
    ):
        import uuid as uuid_mod
        from datetime import UTC, datetime, timedelta
        from sqlalchemy import update
        from src.app.models.shift import Pause

        # Start org shift and pause it
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_with_limits.id)},
        )
        shift_id = resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=employee_headers)

        # Manually backdate the pause started_at to exceed max_pause_minutes (5 min)
        await db_session.execute(
            update(Pause)
            .where(Pause.shift_id == uuid_mod.UUID(shift_id))
            .values(started_at=datetime.now(UTC) - timedelta(minutes=10))
        )
        await db_session.commit()

        # Any shift operation triggers auto-finish of stale pauses
        # List shifts to trigger auto-finish
        resp = await client.get("/api/v1/shifts", headers=employee_headers)
        assert resp.status_code == 200

        # The shift should now be active (pause was auto-finished)
        shifts = resp.json()["data"]["items"]
        org_shift = next(s for s in shifts if s["id"] == shift_id)
        assert org_shift["status"] == "active"


class TestAdminShifts:
    async def test_owner_can_see_employee_shifts(
        self,
        client: AsyncClient,
        owner_headers: dict,
        employee_headers: dict,
        org_no_geo: Organization,
    ):
        # Employee starts a shift
        await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_no_geo.id)},
        )

        # Owner views org shifts
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["organization_id"] == str(org_no_geo.id)

    async def test_employee_cannot_see_org_shifts(
        self,
        client: AsyncClient,
        employee_headers: dict,
        org_no_geo: Organization,
    ):
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=employee_headers,
        )
        assert resp.status_code == 403

    async def test_org_shifts_filtered_by_user(
        self,
        client: AsyncClient,
        owner_headers: dict,
        employee_headers: dict,
        employee_user: User,
        org_no_geo: Organization,
    ):
        # Employee starts a shift
        await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_no_geo.id)},
        )

        # Filter by user_id
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=owner_headers,
            params={"user_id": str(employee_user.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 1

    async def test_org_shifts_pagination(
        self,
        client: AsyncClient,
        owner_headers: dict,
        employee_headers: dict,
        org_no_geo: Organization,
    ):
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=owner_headers,
            params={"limit": 5, "offset": 0},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "total" in data
        assert "items" in data


class TestOrgStats:
    async def test_owner_can_view_stats(
        self,
        client: AsyncClient,
        owner_headers: dict,
        employee_headers: dict,
        org_no_geo: Organization,
    ):
        # Employee starts and finishes a shift
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_no_geo.id)},
        )
        shift_id = resp.json()["data"]["id"]
        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=employee_headers)

        # Owner views stats
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/stats",
            headers=owner_headers,
            params={"period": "month"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["period"] == "month"
        assert data["total_worked_seconds"] >= 0
        assert data["shift_count"] == 1
        assert data["average_shift_seconds"] >= 0
        assert len(data["per_employee"]) == 1
        assert data["per_employee"][0]["shift_count"] == 1

    async def test_employee_cannot_view_stats(
        self,
        client: AsyncClient,
        employee_headers: dict,
        org_no_geo: Organization,
    ):
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/stats",
            headers=employee_headers,
            params={"period": "week"},
        )
        assert resp.status_code == 403

    async def test_empty_stats(
        self,
        client: AsyncClient,
        owner_headers: dict,
        org_no_geo: Organization,
    ):
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/stats",
            headers=owner_headers,
            params={"period": "day"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["shift_count"] == 0
        assert data["total_worked_seconds"] == 0
        assert data["per_employee"] == []
