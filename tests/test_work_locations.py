# tests/test_work_locations.py
import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.user import User


async def _create_second_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="second@example.com",
        password_hash=hash_password("Test1234"),
        name="Second User",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _login_as(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _create_org(client: AsyncClient, auth_headers) -> dict:
    response = await client.post(
        "/api/v1/organizations", headers=auth_headers, json={"name": "Test Org"}
    )
    return response.json()["data"]


class TestCreateWorkLocation:
    async def test_create_location_by_owner(self, client: AsyncClient, auth_headers):
        org = await _create_org(client, auth_headers)

        response = await client.post(
            f"/api/v1/organizations/{org['id']}/locations",
            headers=auth_headers,
            json={
                "name": "Офис",
                "latitude": 55.7558,
                "longitude": 37.6173,
                "radius_meters": 200,
            },
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["name"] == "Офис"
        assert data["latitude"] == 55.7558
        assert data["longitude"] == 37.6173
        assert data["radius_meters"] == 200

    async def test_create_location_default_radius(self, client: AsyncClient, auth_headers):
        org = await _create_org(client, auth_headers)

        response = await client.post(
            f"/api/v1/organizations/{org['id']}/locations",
            headers=auth_headers,
            json={"name": "Склад", "latitude": 55.0, "longitude": 37.0},
        )
        assert response.status_code == 201
        assert response.json()["data"]["radius_meters"] == 100

    async def test_create_location_by_employee_forbidden(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        org = await _create_org(client, auth_headers)

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(
            f"/api/v1/organizations/join/{org['invite_code']}", headers=other_headers
        )

        response = await client.post(
            f"/api/v1/organizations/{org['id']}/locations",
            headers=other_headers,
            json={"name": "Офис", "latitude": 55.0, "longitude": 37.0},
        )
        assert response.status_code == 403

    async def test_create_location_invalid_coordinates(
        self, client: AsyncClient, auth_headers
    ):
        org = await _create_org(client, auth_headers)

        response = await client.post(
            f"/api/v1/organizations/{org['id']}/locations",
            headers=auth_headers,
            json={"name": "Bad", "latitude": 999, "longitude": 37.0},
        )
        assert response.status_code == 422


class TestListWorkLocations:
    async def test_list_locations_by_owner(self, client: AsyncClient, auth_headers):
        org = await _create_org(client, auth_headers)

        await client.post(
            f"/api/v1/organizations/{org['id']}/locations",
            headers=auth_headers,
            json={"name": "Офис", "latitude": 55.0, "longitude": 37.0},
        )
        await client.post(
            f"/api/v1/organizations/{org['id']}/locations",
            headers=auth_headers,
            json={"name": "Склад", "latitude": 56.0, "longitude": 38.0},
        )

        response = await client.get(
            f"/api/v1/organizations/{org['id']}/locations", headers=auth_headers
        )
        assert response.status_code == 200
        assert len(response.json()["data"]["items"]) == 2

    async def test_list_locations_by_member(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        org = await _create_org(client, auth_headers)

        await client.post(
            f"/api/v1/organizations/{org['id']}/locations",
            headers=auth_headers,
            json={"name": "Офис", "latitude": 55.0, "longitude": 37.0},
        )

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(
            f"/api/v1/organizations/join/{org['invite_code']}", headers=other_headers
        )

        response = await client.get(
            f"/api/v1/organizations/{org['id']}/locations", headers=other_headers
        )
        assert response.status_code == 200
        assert len(response.json()["data"]["items"]) == 1


class TestUpdateWorkLocation:
    async def test_update_location(self, client: AsyncClient, auth_headers):
        org = await _create_org(client, auth_headers)

        create_resp = await client.post(
            f"/api/v1/organizations/{org['id']}/locations",
            headers=auth_headers,
            json={"name": "Офис", "latitude": 55.0, "longitude": 37.0},
        )
        loc_id = create_resp.json()["data"]["id"]

        response = await client.patch(
            f"/api/v1/organizations/{org['id']}/locations/{loc_id}",
            headers=auth_headers,
            json={"name": "Главный офис", "radius_meters": 300},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["name"] == "Главный офис"
        assert data["radius_meters"] == 300
        assert data["latitude"] == 55.0  # unchanged


class TestDeleteWorkLocation:
    async def test_delete_location(self, client: AsyncClient, auth_headers):
        org = await _create_org(client, auth_headers)

        create_resp = await client.post(
            f"/api/v1/organizations/{org['id']}/locations",
            headers=auth_headers,
            json={"name": "Офис", "latitude": 55.0, "longitude": 37.0},
        )
        loc_id = create_resp.json()["data"]["id"]

        response = await client.delete(
            f"/api/v1/organizations/{org['id']}/locations/{loc_id}",
            headers=auth_headers,
        )
        assert response.status_code == 200

        list_resp = await client.get(
            f"/api/v1/organizations/{org['id']}/locations", headers=auth_headers
        )
        assert len(list_resp.json()["data"]["items"]) == 0

    async def test_delete_location_not_found(self, client: AsyncClient, auth_headers):
        org = await _create_org(client, auth_headers)

        fake_id = str(uuid.uuid4())
        response = await client.delete(
            f"/api/v1/organizations/{org['id']}/locations/{fake_id}",
            headers=auth_headers,
        )
        assert response.status_code == 404
