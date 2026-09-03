import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.user import User
from src.app.models.work_location import WorkLocation


@pytest.fixture
async def second_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="employee@example.com",
        password_hash=hash_password("Test1234"),
        name="Employee User",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def second_auth_headers(second_user: User, client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "employee@example.com", "password": "Test1234"},
    )
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def organization(db_session: AsyncSession, verified_user: User) -> Organization:
    org = Organization(name="Test Org", owner_id=verified_user.id)
    db_session.add(org)
    await db_session.flush()
    settings = OrganizationSettings(organization_id=org.id)
    db_session.add(settings)
    await db_session.commit()
    return org


class TestGetSettings:
    async def test_owner_can_get_settings(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.get(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["geo_check_enabled"] is False
        assert data["max_pause_minutes"] is None
        assert data["max_pauses_per_shift"] is None
        assert data["auto_finish_by_schedule"] is True
        assert data["require_schedule"] is False
        assert data["late_tolerance_minutes"] == 0
        assert data["overtime_request_days"] == 7
        assert data["early_start_minutes"] == 0
        assert data["checklist_grace_minutes"] == 30
        assert "auto_finish_hours" not in data

    async def test_admin_can_get_settings(
        self,
        client: AsyncClient,
        second_auth_headers: dict,
        organization: Organization,
        second_user: User,
        db_session: AsyncSession,
    ):
        member = OrganizationMember(
            organization_id=organization.id,
            user_id=second_user.id,
            role=MemberRole.admin,
        )
        db_session.add(member)
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=second_auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["geo_check_enabled"] is False

    async def test_employee_cannot_get_settings(
        self,
        client: AsyncClient,
        second_auth_headers: dict,
        organization: Organization,
        second_user: User,
        db_session: AsyncSession,
    ):
        member = OrganizationMember(
            organization_id=organization.id,
            user_id=second_user.id,
            role=MemberRole.employee,
        )
        db_session.add(member)
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=second_auth_headers,
        )
        assert resp.status_code == 403


class TestUpdateSettings:
    async def test_owner_can_update(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={
                "max_pause_minutes": 30,
                "max_pauses_per_shift": 3,
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["max_pause_minutes"] == 30
        assert data["max_pauses_per_shift"] == 3
        assert data["auto_finish_by_schedule"] is True  # unchanged

    async def test_admin_can_update(
        self,
        client: AsyncClient,
        second_auth_headers: dict,
        organization: Organization,
        second_user: User,
        db_session: AsyncSession,
    ):
        member = OrganizationMember(
            organization_id=organization.id,
            user_id=second_user.id,
            role=MemberRole.admin,
        )
        db_session.add(member)
        await db_session.commit()

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=second_auth_headers,
            json={"max_pause_minutes": 15},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["max_pause_minutes"] == 15

    async def test_disable_auto_finish_by_schedule(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"auto_finish_by_schedule": False},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["auto_finish_by_schedule"] is False

    async def test_set_late_tolerance_and_overtime_request_days(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"late_tolerance_minutes": 15, "overtime_request_days": 14},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["late_tolerance_minutes"] == 15
        assert data["overtime_request_days"] == 14

    async def test_late_tolerance_out_of_range_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"late_tolerance_minutes": 121},
        )
        assert resp.status_code == 422

    async def test_set_early_start_minutes_boundaries(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        """schedule_window_enforcement: диапазон 0–240, обе границы допустимы."""
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"early_start_minutes": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["early_start_minutes"] == 0

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"early_start_minutes": 240},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["early_start_minutes"] == 240

    async def test_early_start_minutes_out_of_range_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"early_start_minutes": -1},
        )
        assert resp.status_code == 422

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"early_start_minutes": 241},
        )
        assert resp.status_code == 422

    async def test_set_checklist_grace_minutes_boundaries(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        """checklist_grace_period: диапазон 0–240, обе границы допустимы; 0 —
        дозаполнение запрещено (прежнее поведение)."""
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"checklist_grace_minutes": 0},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["checklist_grace_minutes"] == 0

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"checklist_grace_minutes": 240},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["checklist_grace_minutes"] == 240

    async def test_checklist_grace_minutes_out_of_range_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"checklist_grace_minutes": -1},
        )
        assert resp.status_code == 422

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"checklist_grace_minutes": 241},
        )
        assert resp.status_code == 422

    async def test_checklist_grace_minutes_not_integer_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"checklist_grace_minutes": 10.5},
        )
        assert resp.status_code == 422

    async def test_cannot_require_schedule_without_schedules(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"require_schedule": True},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "SCHEDULE_REQUIRED_NO_SCHEDULES"

    async def test_can_require_schedule_with_active_schedule(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        create_resp = await client.post(
            f"/api/v1/organizations/{organization.id}/work-schedules",
            headers=auth_headers,
            json={"name": "Дневная", "start_time": "09:00", "end_time": "18:00"},
        )
        assert create_resp.status_code == 201

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"require_schedule": True},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["require_schedule"] is True

    async def test_cannot_require_schedule_with_only_paused_schedule(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        create_resp = await client.post(
            f"/api/v1/organizations/{organization.id}/work-schedules",
            headers=auth_headers,
            json={"name": "Дневная", "start_time": "09:00", "end_time": "18:00"},
        )
        schedule_id = create_resp.json()["data"]["id"]
        await client.patch(
            f"/api/v1/organizations/{organization.id}/work-schedules/{schedule_id}",
            headers=auth_headers,
            json={"is_paused": True},
        )

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"require_schedule": True},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "SCHEDULE_REQUIRED_NO_SCHEDULES"

    async def test_employee_cannot_update(
        self,
        client: AsyncClient,
        second_auth_headers: dict,
        organization: Organization,
        second_user: User,
        db_session: AsyncSession,
    ):
        member = OrganizationMember(
            organization_id=organization.id,
            user_id=second_user.id,
            role=MemberRole.employee,
        )
        db_session.add(member)
        await db_session.commit()

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=second_auth_headers,
            json={"max_pause_minutes": 10},
        )
        assert resp.status_code == 403


class TestGeoCheckEnabled:
    async def test_org_response_contains_geo_check_enabled(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.get(
            f"/api/v1/organizations/{organization.id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "geo_check_enabled" in data
        assert data["geo_check_enabled"] is False

    async def test_org_list_contains_geo_check_enabled(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.get(
            "/api/v1/organizations",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) > 0
        assert "geo_check_enabled" in items[0]

    async def test_cannot_enable_geo_check_without_locations(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"geo_check_enabled": True},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "NO_WORK_LOCATIONS"

    async def test_can_enable_geo_check_with_locations(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
        db_session: AsyncSession,
    ):
        location = WorkLocation(
            organization_id=organization.id,
            name="Office",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=200,
        )
        db_session.add(location)
        await db_session.commit()

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"geo_check_enabled": True},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["geo_check_enabled"] is True

    async def test_delete_last_location_disables_geo_check(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
        db_session: AsyncSession,
    ):
        # Create location and enable geo_check
        location = WorkLocation(
            organization_id=organization.id,
            name="Office",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=200,
        )
        db_session.add(location)
        await db_session.commit()

        await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"geo_check_enabled": True},
        )

        # Delete the only location
        resp = await client.delete(
            f"/api/v1/organizations/{organization.id}/locations/{location.id}",
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # Verify geo_check was auto-disabled
        settings_resp = await client.get(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
        )
        assert settings_resp.json()["data"]["geo_check_enabled"] is False

    async def test_delete_non_last_location_keeps_geo_check(
        self,
        client: AsyncClient,
        auth_headers: dict,
        organization: Organization,
        db_session: AsyncSession,
    ):
        # Create two locations
        loc1 = WorkLocation(
            organization_id=organization.id,
            name="Office 1",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=200,
        )
        loc2 = WorkLocation(
            organization_id=organization.id,
            name="Office 2",
            latitude=55.7600,
            longitude=37.6200,
            radius_meters=100,
        )
        db_session.add_all([loc1, loc2])
        await db_session.commit()

        await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={"geo_check_enabled": True},
        )

        # Delete one location — geo_check should stay enabled
        await client.delete(
            f"/api/v1/organizations/{organization.id}/locations/{loc1.id}",
            headers=auth_headers,
        )

        settings_resp = await client.get(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
        )
        assert settings_resp.json()["data"]["geo_check_enabled"] is True
