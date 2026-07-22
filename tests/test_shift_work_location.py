"""Тесты привязки точки к смене (shift_work_location).

Покрывают матрицу гео×require_work_location при старте смены, валидацию настройки,
сериализацию точки в ShiftResponse и обратную совместимость.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
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
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


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
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


async def _make_org(
    db_session: AsyncSession,
    owner: User,
    employee_user: User,
    *,
    geo: bool,
    require: bool,
    locations: list[dict[str, Any]] | None = None,
) -> tuple[Organization, list[WorkLocation]]:
    org = Organization(name="Org", owner_id=owner.id)
    db_session.add(org)
    await db_session.flush()

    db_session.add(
        OrganizationSettings(
            organization_id=org.id,
            geo_check_enabled=geo,
            require_work_location=require,
        )
    )

    created: list[WorkLocation] = []
    for spec in locations or []:
        loc = WorkLocation(organization_id=org.id, **spec)
        db_session.add(loc)
        created.append(loc)

    db_session.add(
        OrganizationMember(
            organization_id=org.id,
            user_id=employee_user.id,
            role=MemberRole.employee,
        )
    )
    await db_session.commit()
    return org, created


class TestStartGeoEnabled:
    async def test_point_determined_automatically_nearest(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Гео вкл: при попадании в несколько зон выбирается ближайшая."""
        org, locs = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=True,
            require=False,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 500},
                {"name": "B", "latitude": 55.7600, "longitude": 37.6173, "radius_meters": 500},
            ],
        )
        loc_a, loc_b = locs

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={
                "organization_id": str(org.id),
                "latitude": 55.7565,  # ближе к A
                "longitude": 37.6173,
                "work_location_id": str(loc_b.id),  # должен игнорироваться
            },
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["work_location_id"] == str(loc_a.id)
        assert data["work_location"]["id"] == str(loc_a.id)
        assert data["work_location"]["name"] == "A"

    async def test_outside_zones_still_rejected(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=True,
            require=False,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
            ],
        )
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id), "latitude": 56.0, "longitude": 38.0},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "GEO_CHECK_FAILED"


class TestStartGeoDisabledRequire:
    async def test_missing_work_location_rejected(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=False,
            require=True,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
            ],
        )
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id)},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORK_LOCATION_REQUIRED"

    async def test_valid_work_location_saved(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, locs = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=False,
            require=True,
            locations=[
                {
                    "name": "A",
                    "latitude": 55.7558,
                    "longitude": 37.6173,
                    "radius_meters": 200,
                    "address": "ул. Тест, 1",
                },
            ],
        )
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id), "work_location_id": str(locs[0].id)},
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["work_location_id"] == str(locs[0].id)
        assert data["work_location"]["address"] == "ул. Тест, 1"

    async def test_foreign_work_location_404(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=False,
            require=True,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
            ],
        )
        # точка из другой организации
        other_org = Organization(name="Other", owner_id=owner.id)
        db_session.add(other_org)
        await db_session.flush()
        foreign = WorkLocation(
            organization_id=other_org.id,
            name="Foreign",
            latitude=10.0,
            longitude=10.0,
            radius_meters=100,
        )
        db_session.add(foreign)
        await db_session.commit()

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id), "work_location_id": str(foreign.id)},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "WORK_LOCATION_NOT_FOUND"

    async def test_malformed_work_location_404(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=False,
            require=True,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
            ],
        )
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id), "work_location_id": "not-a-uuid"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "WORK_LOCATION_NOT_FOUND"


class TestStartGeoDisabledOptional:
    async def test_work_location_optional_saved_when_sent(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, locs = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=False,
            require=False,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
            ],
        )
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id), "work_location_id": str(locs[0].id)},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["work_location_id"] == str(locs[0].id)

    async def test_work_location_null_when_not_sent(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session, owner, employee_user, geo=False, require=False, locations=[]
        )
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id)},
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["work_location_id"] is None
        assert data["work_location"] is None


class TestPersonalShiftBackwardCompat:
    async def test_personal_shift_has_null_work_location(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
    ) -> None:
        """Персональная смена: точка всегда null, старый контракт цел."""
        resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["organization_id"] is None
        assert data["work_location_id"] is None
        assert data["work_location"] is None
        # старые поля на месте
        assert "worked_seconds" in data
        assert data["status"] == "active"


class TestSettingsRequireWorkLocation:
    async def test_response_contains_field_default_false(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session, owner, employee_user, geo=False, require=False, locations=[]
        )
        resp = await client.get(f"/api/v1/organizations/{org.id}/settings", headers=owner_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["require_work_location"] is False

    async def test_cannot_enable_without_locations(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session, owner, employee_user, geo=False, require=False, locations=[]
        )
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/settings",
            headers=owner_headers,
            json={"require_work_location": True},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "WORK_LOCATION_REQUIRED_NO_LOCATIONS"

    async def test_can_enable_with_locations(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=False,
            require=False,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
            ],
        )
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/settings",
            headers=owner_headers,
            json={"require_work_location": True},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["require_work_location"] is True

    async def test_delete_last_location_auto_disables_require(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, locs = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=False,
            require=True,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
            ],
        )
        del_resp = await client.delete(
            f"/api/v1/organizations/{org.id}/locations/{locs[0].id}",
            headers=owner_headers,
        )
        assert del_resp.status_code == 200

        settings_resp = await client.get(
            f"/api/v1/organizations/{org.id}/settings", headers=owner_headers
        )
        assert settings_resp.json()["data"]["require_work_location"] is False


class TestOrganizationResponseExposesRequireWorkLocation:
    """Контракт-гап (addendum): employee должен видеть require_work_location в
    контексте org, т.к. к /settings (owner/admin) у него доступа нет."""

    async def test_employee_sees_require_in_org_list(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=False,
            require=True,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
            ],
        )
        resp = await client.get("/api/v1/organizations", headers=employee_headers)
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        target = next(i for i in items if i["id"] == str(org.id))
        assert target["require_work_location"] is True

    async def test_default_false_in_org_response(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(
            db_session, owner, employee_user, geo=False, require=False, locations=[]
        )
        resp = await client.get("/api/v1/organizations", headers=employee_headers)
        items = resp.json()["data"]["items"]
        target = next(i for i in items if i["id"] == str(org.id))
        assert target["require_work_location"] is False

    async def test_join_response_contains_require(
        self,
        client: AsyncClient,
        owner: User,
        db_session: AsyncSession,
    ) -> None:
        org = Organization(name="Joinable", owner_id=owner.id)
        db_session.add(org)
        await db_session.flush()
        db_session.add(
            OrganizationSettings(
                organization_id=org.id,
                geo_check_enabled=False,
                require_work_location=True,
            )
        )
        db_session.add(
            WorkLocation(
                organization_id=org.id,
                name="A",
                latitude=55.7558,
                longitude=37.6173,
                radius_meters=200,
            )
        )
        joiner = User(
            id=uuid.uuid4(),
            email="joiner@example.com",
            password_hash=hash_password("Test1234"),
            name="Joiner",
            is_verified=True,
        )
        db_session.add(joiner)
        await db_session.commit()

        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "joiner@example.com", "password": "Test1234"},
        )
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

        resp = await client.post(f"/api/v1/organizations/join/{org.invite_code}", headers=headers)
        assert resp.status_code in (200, 201)
        assert resp.json()["data"]["require_work_location"] is True


class TestOrgShiftsExposeWorkLocation:
    async def test_org_shift_list_and_detail_contain_work_location(
        self,
        client: AsyncClient,
        owner_headers: dict[str, Any],
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, locs = await _make_org(
            db_session,
            owner,
            employee_user,
            geo=False,
            require=True,
            locations=[
                {"name": "A", "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
            ],
        )
        start = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id), "work_location_id": str(locs[0].id)},
        )
        shift_id = start.json()["data"]["id"]

        list_resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts", headers=owner_headers
        )
        assert list_resp.status_code == 200
        item = list_resp.json()["data"]["items"][0]
        assert item["work_location"]["id"] == str(locs[0].id)

        detail_resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts/{shift_id}", headers=owner_headers
        )
        assert detail_resp.status_code == 200
        assert detail_resp.json()["data"]["work_location_id"] == str(locs[0].id)
