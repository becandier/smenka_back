# tests/test_users.py
from httpx import AsyncClient


class TestGetMe:
    async def test_get_me_success(
        self,
        client: AsyncClient,
        verified_user,
        auth_headers: dict,
    ):
        response = await client.get("/api/v1/users/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["email"] == "test@example.com"
        assert data["name"] == "Test User"
        assert data["is_verified"] is True

    async def test_get_me_unauthorized(self, client: AsyncClient):
        response = await client.get("/api/v1/users/me")
        assert response.status_code in (401, 403)


class TestUpdateMe:
    async def test_update_name(
        self,
        client: AsyncClient,
        verified_user,
        auth_headers: dict,
    ):
        response = await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"name": "Updated Name"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["name"] == "Updated Name"

    async def test_update_phone(
        self,
        client: AsyncClient,
        verified_user,
        auth_headers: dict,
    ):
        response = await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"phone": "+79991234567"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["phone"] == "+79991234567"

    async def test_update_me_unauthorized(self, client: AsyncClient):
        response = await client.patch(
            "/api/v1/users/me",
            json={"name": "Hacker"},
        )
        assert response.status_code in (401, 403)
