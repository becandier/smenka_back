"""Тесты `GET /organizations/{org_id}/work-locations/nearby` (shift_start_location_choice).

Покрывают контракт списка подходящих точек: попадание в радиус, `is_nearest`,
детерминированный тай-брейк при равном расстоянии, `nearest_outside`, доступ.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient, Response
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
async def outsider_user(db_session: AsyncSession) -> User:
    """Пользователь, не состоящий в организации — для проверки доступа."""
    user = User(
        id=uuid.uuid4(),
        email="outsider@example.com",
        password_hash=hash_password("Test1234"),
        name="Outsider",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _headers(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
async def employee_headers(employee_user: User, client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "employee@example.com")


@pytest.fixture
async def outsider_headers(outsider_user: User, client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "outsider@example.com")


async def _make_org(
    db_session: AsyncSession,
    owner: User,
    employee_user: User,
    *,
    locations: list[WorkLocation] | None = None,
) -> Organization:
    org = Organization(name="Org", owner_id=owner.id)
    db_session.add(org)
    await db_session.flush()

    db_session.add(OrganizationSettings(organization_id=org.id, geo_check_enabled=True))

    for loc in locations or []:
        loc.organization_id = org.id
        db_session.add(loc)

    db_session.add(
        OrganizationMember(
            organization_id=org.id,
            user_id=employee_user.id,
            role=MemberRole.employee,
        )
    )
    await db_session.commit()
    return org


async def _get_nearby(
    client: AsyncClient,
    org: Organization,
    headers: dict[str, str],
    latitude: float | None = 55.7558,
    longitude: float | None = 37.6173,
) -> Response:
    """GET .../work-locations/nearby. `latitude`/`longitude=None` опускает параметр
    (для проверки обязательности query)."""
    params: dict[str, float] = {}
    if latitude is not None:
        params["latitude"] = latitude
    if longitude is not None:
        params["longitude"] = longitude
    return await client.get(
        f"/api/v1/organizations/{org.id}/work-locations/nearby",
        headers=headers,
        params=params,
    )


class TestNearbyMatchedAndSorting:
    async def test_two_overlapping_zones_sorted_with_nearest_flag(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        loc_a = WorkLocation(name="A", latitude=55.7558, longitude=37.6173, radius_meters=500)
        loc_b = WorkLocation(name="B", latitude=55.7600, longitude=37.6173, radius_meters=500)
        org = await _make_org(db_session, owner, employee_user, locations=[loc_a, loc_b])

        resp = await _get_nearby(
            client, org, employee_headers, latitude=55.7565, longitude=37.6173
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        items = data["items"]
        assert len(items) == 2
        assert items[0]["id"] == str(loc_a.id)
        assert items[0]["is_nearest"] is True
        assert items[1]["id"] == str(loc_b.id)
        assert items[1]["is_nearest"] is False
        assert items[0]["distance_meters"] < items[1]["distance_meters"]
        assert isinstance(items[0]["distance_meters"], int)
        assert data["nearest_outside"] is None

    async def test_single_matching_zone(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        loc = WorkLocation(name="A", latitude=55.7558, longitude=37.6173, radius_meters=200)
        org = await _make_org(db_session, owner, employee_user, locations=[loc])

        resp = await _get_nearby(client, org, employee_headers)
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["is_nearest"] is True
        assert items[0]["distance_meters"] == 0


class TestNearbyTieBreak:
    async def test_equal_distance_ties_break_by_created_at(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Две точки с идентичными координатами (равное расстояние) — побеждает
        созданная раньше, независимо от порядка выдачи БД."""
        now = datetime.now(UTC)
        loc_later = WorkLocation(
            name="Later",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=300,
            created_at=now,
        )
        loc_earlier = WorkLocation(
            name="Earlier",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=300,
            created_at=now - timedelta(seconds=5),
        )
        # Порядок добавления в сессию намеренно "неправильный" (later раньше earlier),
        # чтобы исключить случайное совпадение с порядком выдачи БД.
        org = await _make_org(db_session, owner, employee_user, locations=[loc_later, loc_earlier])

        resp = await _get_nearby(client, org, employee_headers)
        items = resp.json()["data"]["items"]
        assert [it["id"] for it in items] == [str(loc_earlier.id), str(loc_later.id)]
        assert items[0]["is_nearest"] is True

    async def test_equal_distance_and_created_at_ties_break_by_id(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Равное расстояние И равный `created_at` — последний тай-брейк: меньший id."""
        now = datetime.now(UTC)
        id_small = uuid.UUID(int=1)
        id_large = uuid.UUID(int=2)
        loc_large = WorkLocation(
            id=id_large,
            name="Large",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=300,
            created_at=now,
        )
        loc_small = WorkLocation(
            id=id_small,
            name="Small",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=300,
            created_at=now,
        )
        org = await _make_org(db_session, owner, employee_user, locations=[loc_large, loc_small])

        resp = await _get_nearby(client, org, employee_headers)
        items = resp.json()["data"]["items"]
        assert [it["id"] for it in items] == [str(id_small), str(id_large)]

    async def test_repeated_calls_return_stable_order(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)
        loc_1 = WorkLocation(
            name="One",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=300,
            created_at=now,
        )
        loc_2 = WorkLocation(
            name="Two",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=300,
            created_at=now + timedelta(seconds=1),
        )
        org = await _make_org(db_session, owner, employee_user, locations=[loc_1, loc_2])

        orders = []
        for _ in range(5):
            resp = await _get_nearby(client, org, employee_headers)
            orders.append([it["id"] for it in resp.json()["data"]["items"]])

        assert all(order == [str(loc_1.id), str(loc_2.id)] for order in orders)


class TestNearbyOutsideAllZones:
    async def test_empty_items_with_nearest_outside_hint(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        loc_office = WorkLocation(
            name="Офис", latitude=55.7558, longitude=37.6173, radius_meters=100
        )
        org = await _make_org(db_session, owner, employee_user, locations=[loc_office])

        # достаточно далеко, чтобы гарантированно оказаться вне 100 м радиуса
        resp = await _get_nearby(client, org, employee_headers, latitude=55.76)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["nearest_outside"]["id"] == str(loc_office.id)
        assert data["nearest_outside"]["distance_meters"] > 100
        assert data["nearest_outside"]["radius_meters"] == 100
        assert data["nearest_outside"]["name"] == "Офис"

    async def test_no_locations_at_all_nearest_outside_is_null(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org = await _make_org(db_session, owner, employee_user, locations=[])

        resp = await _get_nearby(client, org, employee_headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["nearest_outside"] is None


class TestNearbyAccess:
    async def test_non_member_forbidden(
        self,
        client: AsyncClient,
        outsider_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        loc = WorkLocation(name="A", latitude=55.7558, longitude=37.6173, radius_meters=200)
        org = await _make_org(db_session, owner, employee_user, locations=[loc])

        resp = await _get_nearby(client, org, outsider_headers)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_owner_has_access(
        self,
        client: AsyncClient,
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        loc = WorkLocation(name="A", latitude=55.7558, longitude=37.6173, radius_meters=200)
        org = await _make_org(db_session, owner, employee_user, locations=[loc])
        owner_headers = await _headers(client, "owner@example.com")

        resp = await _get_nearby(client, org, owner_headers)
        assert resp.status_code == 200
        assert len(resp.json()["data"]["items"]) == 1

    async def test_org_not_found(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        employee_user: User,
    ) -> None:
        fake_org = Organization(id=uuid.uuid4(), name="Ghost", owner_id=employee_user.id)
        resp = await _get_nearby(client, fake_org, employee_headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ORG_NOT_FOUND"

    async def test_missing_coordinates_validation_error(
        self,
        client: AsyncClient,
        employee_headers: dict[str, Any],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org = await _make_org(db_session, owner, employee_user, locations=[])

        resp = await _get_nearby(client, org, employee_headers, latitude=None, longitude=None)
        assert resp.status_code == 422
