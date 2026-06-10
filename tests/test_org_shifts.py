# tests/test_org_shifts.py
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_role import OrganizationRole
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
    db_session: AsyncSession,
    owner: User,
    employee_user: User,
) -> Organization:
    """Org with geo_check_enabled and a work location in Moscow."""
    org = Organization(name="Geo Org", owner_id=owner.id)
    db_session.add(org)
    await db_session.flush()

    settings = OrganizationSettings(
        organization_id=org.id,
        geo_check_enabled=True,
        auto_finish_hours=16,
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
    db_session: AsyncSession,
    owner: User,
    employee_user: User,
) -> Organization:
    """Org without geo check."""
    org = Organization(name="No Geo Org", owner_id=owner.id)
    db_session.add(org)
    await db_session.flush()

    settings = OrganizationSettings(
        organization_id=org.id,
        geo_check_enabled=False,
        auto_finish_hours=16,
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


class TestOrgShiftStart:
    async def test_start_org_shift_within_radius(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        org_with_geo: Organization,
    ) -> None:
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
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        org_with_geo: Organization,
    ) -> None:
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
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_no_geo.id)},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["organization_id"] == str(org_no_geo.id)

    async def test_start_org_shift_geo_enabled_no_coords(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        org_with_geo: Organization,
    ) -> None:
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org_with_geo.id)},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "COORDS_REQUIRED"

    async def test_non_member_cannot_start_org_shift(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        org_with_geo: Organization,
    ) -> None:
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
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
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
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
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
    db_session: AsyncSession,
    owner: User,
    employee_user: User,
) -> Organization:
    """Org with pause limits: max 2 pauses, max 5 minutes per pause."""
    org = Organization(name="Limits Org", owner_id=owner.id)
    db_session.add(org)
    await db_session.flush()

    settings = OrganizationSettings(
        organization_id=org.id,
        geo_check_enabled=False,
        auto_finish_hours=16,
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
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        org_with_limits: Organization,
    ) -> None:
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
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
    ) -> None:
        # Start personal shift
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={},
        )
        shift_id = resp.json()["data"]["id"]

        # Should allow unlimited pauses
        for _ in range(5):
            resp_p = await client.post(
                f"/api/v1/shifts/{shift_id}/pause",
                headers=employee_headers,
            )
            assert resp_p.status_code == 200
            resp_r = await client.post(
                f"/api/v1/shifts/{shift_id}/resume",
                headers=employee_headers,
            )
            assert resp_r.status_code == 200


class TestAdminShifts:
    async def test_owner_can_see_employee_shifts(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
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
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=employee_headers,
        )
        assert resp.status_code == 403

    async def test_org_shifts_filtered_by_user(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        employee_user: User,
        org_no_geo: Organization,
    ) -> None:
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
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
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
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
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
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/stats",
            headers=employee_headers,
            params={"period": "week"},
        )
        assert resp.status_code == 403

    async def test_empty_stats(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
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


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="admin-member@example.com",
        password_hash=hash_password("Test1234"),
        name="Admin Member",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def admin_member(
    db_session: AsyncSession,
    admin_user: User,
    org_no_geo: Organization,
) -> OrganizationMember:
    """admin_user added to org_no_geo with the system `admin` role."""
    member = OrganizationMember(
        organization_id=org_no_geo.id,
        user_id=admin_user.id,
        role=MemberRole.admin,
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def admin_member_headers(admin_user: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin-member@example.com", "password": "Test1234"},
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def employee2_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="employee2@example.com",
        password_hash=hash_password("Test1234"),
        name="Employee Two",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def employee2_member(
    db_session: AsyncSession,
    employee2_user: User,
    org_no_geo: Organization,
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org_no_geo.id,
        user_id=employee2_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def employee2_headers(employee2_user: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "employee2@example.com", "password": "Test1234"},
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _start_org_shift(
    client: AsyncClient,
    headers: dict[str, str],
    org: Organization,
) -> str:
    resp = await client.post(
        "/api/v1/shifts/start",
        headers=headers,
        json={"organization_id": str(org.id)},
    )
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


class TestShiftOwnerVisibility:
    """Видимость владельца/сотрудника смены в орг-режиме (shift_owner_visibility)."""

    async def test_list_enriches_employee_identity(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        employee_user: User,
        org_no_geo: Organization,
    ) -> None:
        await _start_org_shift(client, employee_headers, org_no_geo)

        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["user_id"] == str(employee_user.id)
        assert item["user_name"] == "Employee"
        assert item["user_email"] == "employee@example.com"
        assert item["role"] == "employee"
        assert item["custom_role_name"] is None

    async def test_list_enriches_custom_role_name(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        employee_user: User,
        org_no_geo: Organization,
    ) -> None:
        # Assign a custom role to the employee member
        role = OrganizationRole(organization_id=org_no_geo.id, name="Бариста")
        db_session.add(role)
        await db_session.flush()
        await db_session.execute(
            OrganizationMember.__table__.update()
            .where(
                OrganizationMember.organization_id == org_no_geo.id,
                OrganizationMember.user_id == employee_user.id,
            )
            .values(role_id=role.id)
        )
        await db_session.commit()

        await _start_org_shift(client, employee_headers, org_no_geo)

        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["role"] == "employee"
        assert item["custom_role_name"] == "Бариста"

    async def test_personal_shifts_identity_always_null(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
    ) -> None:
        # Personal shift (no organization_id)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={},
        )
        assert resp.status_code == 201

        resp = await client.get("/api/v1/shifts", headers=employee_headers)
        assert resp.status_code == 200
        item = resp.json()["data"]["items"][0]
        assert item["organization_id"] is None
        assert item["user_name"] is None
        assert item["user_email"] is None
        assert item["role"] is None
        assert item["custom_role_name"] is None

    async def test_owner_reads_shift_detail(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        employee_user: User,
        org_no_geo: Organization,
    ) -> None:
        shift_id = await _start_org_shift(client, employee_headers, org_no_geo)

        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts/{shift_id}",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == shift_id
        assert data["user_id"] == str(employee_user.id)
        assert data["user_name"] == "Employee"
        assert data["user_email"] == "employee@example.com"
        assert data["role"] == "employee"
        assert data["custom_role_name"] is None
        assert "pauses" in data
        assert "worked_seconds" in data

    async def test_admin_member_reads_shift_detail(
        self,
        client: AsyncClient,
        admin_member: OrganizationMember,
        admin_member_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        shift_id = await _start_org_shift(client, employee_headers, org_no_geo)

        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts/{shift_id}",
            headers=admin_member_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["id"] == shift_id

    async def test_employee_cannot_read_shift_detail(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        shift_id = await _start_org_shift(client, employee_headers, org_no_geo)

        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts/{shift_id}",
            headers=employee_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_detail_unknown_shift_404(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts/{uuid.uuid4()}",
            headers=owner_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SHIFT_NOT_FOUND"

    async def test_detail_personal_shift_404(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        # Employee's personal shift must not be visible via org detail endpoint
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={},
        )
        personal_shift_id = resp.json()["data"]["id"]

        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts/{personal_shift_id}",
            headers=owner_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SHIFT_NOT_FOUND"

    async def test_detail_other_org_shift_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner: User,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        employee_user: User,
        org_no_geo: Organization,
    ) -> None:
        # A second org owned by the same owner; shift belongs to org_no_geo
        other_org = Organization(name="Other Org", owner_id=owner.id)
        db_session.add(other_org)
        await db_session.flush()
        db_session.add(
            OrganizationMember(
                organization_id=other_org.id,
                user_id=employee_user.id,
                role=MemberRole.employee,
            )
        )
        await db_session.commit()

        shift_id = await _start_org_shift(client, employee_headers, org_no_geo)

        # Reading org_no_geo's shift under other_org must 404
        resp = await client.get(
            f"/api/v1/organizations/{other_org.id}/shifts/{shift_id}",
            headers=owner_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SHIFT_NOT_FOUND"

    async def test_detail_unknown_org_404(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{uuid.uuid4()}/shifts/{uuid.uuid4()}",
            headers=owner_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ORG_NOT_FOUND"

    async def test_excluded_member_keeps_name_drops_role(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        employee_user: User,
        org_no_geo: Organization,
    ) -> None:
        shift_id = await _start_org_shift(client, employee_headers, org_no_geo)

        # Employee excluded from org after the shift exists
        await db_session.execute(
            delete(OrganizationMember).where(
                OrganizationMember.organization_id == org_no_geo.id,
                OrganizationMember.user_id == employee_user.id,
            )
        )
        await db_session.commit()

        # Detail: name/email survive, role/custom_role_name become null
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts/{shift_id}",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["user_name"] == "Employee"
        assert data["user_email"] == "employee@example.com"
        assert data["role"] is None
        assert data["custom_role_name"] is None

        # List: shift still present and enriched the same way
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["user_name"] == "Employee"
        assert items[0]["role"] is None

    async def test_super_admin_reads_shift_detail(
        self,
        client: AsyncClient,
        super_admin_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        employee_user: User,
        org_no_geo: Organization,
    ) -> None:
        """super_admin (не owner и не член org) читает деталь как owner-эквивалент."""
        shift_id = await _start_org_shift(client, employee_headers, org_no_geo)

        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts/{shift_id}",
            headers=super_admin_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == shift_id
        assert data["user_id"] == str(employee_user.id)
        assert data["user_name"] == "Employee"
        assert data["role"] == "employee"

    async def test_list_status_filter_keeps_enrichment(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        """Фильтр ?status=finished не ломает обогащение identity."""
        shift_id = await _start_org_shift(client, employee_headers, org_no_geo)
        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=employee_headers)

        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=owner_headers,
            params={"status": "finished"},
        )
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["status"] == "finished"
        assert items[0]["user_name"] == "Employee"
        assert items[0]["role"] == "employee"

        # active-фильтр после завершения — пусто
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=owner_headers,
            params={"status": "active"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 0

    async def test_list_invalid_status_400(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts",
            headers=owner_headers,
            params={"status": "bogus"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_STATUS"

    async def test_list_pagination_preserves_enrichment(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        employee2_member: OrganizationMember,
        employee2_headers: dict[str, Any],
        employee_user: User,
        employee2_user: User,
        org_no_geo: Organization,
    ) -> None:
        """Batch-обогащение работает при пагинации с несколькими сотрудниками."""
        await _start_org_shift(client, employee_headers, org_no_geo)
        await _start_org_shift(client, employee2_headers, org_no_geo)

        names_by_user = {
            str(employee_user.id): "Employee",
            str(employee2_user.id): "Employee Two",
        }

        seen: set[str] = set()
        for offset in (0, 1):
            resp = await client.get(
                f"/api/v1/organizations/{org_no_geo.id}/shifts",
                headers=owner_headers,
                params={"limit": 1, "offset": offset},
            )
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["total"] == 2
            assert len(data["items"]) == 1
            item = data["items"][0]
            assert item["user_name"] == names_by_user[item["user_id"]]
            assert item["role"] == "employee"
            seen.add(item["user_id"])

        # Обе смены разных сотрудников обогащены корректно на разных страницах
        assert seen == {str(employee_user.id), str(employee2_user.id)}

    async def test_other_employee_cannot_read_detail(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        employee2_member: OrganizationMember,
        employee2_headers: dict[str, Any],
        org_no_geo: Organization,
    ) -> None:
        """ТЗ: employee чужие смены не видит — даже член той же org."""
        shift_id = await _start_org_shift(client, employee_headers, org_no_geo)

        resp = await client.get(
            f"/api/v1/organizations/{org_no_geo.id}/shifts/{shift_id}",
            headers=employee2_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
