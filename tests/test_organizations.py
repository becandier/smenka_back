# tests/test_organizations.py
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


class TestCreateOrganization:
    async def test_create_organization_success(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/organizations",
            headers=auth_headers,
            json={"name": "Test Org"},
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["name"] == "Test Org"
        assert len(data["invite_code"]) == 8
        assert data["is_deleted"] is False

    async def test_create_organization_unauthorized(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/organizations",
            json={"name": "Test Org"},
        )
        assert response.status_code == 401

    async def test_create_multiple_organizations(self, client: AsyncClient, auth_headers):
        await client.post("/api/v1/organizations", headers=auth_headers, json={"name": "Org 1"})
        response = await client.post("/api/v1/organizations", headers=auth_headers, json={"name": "Org 2"})
        assert response.status_code == 201

        list_resp = await client.get("/api/v1/organizations", headers=auth_headers)
        assert len(list_resp.json()["data"]["items"]) == 2


class TestUpdateOrganization:
    async def test_update_organization_success(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Old Name"}
        )
        org_id = create_resp.json()["data"]["id"]

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=auth_headers,
            json={"name": "New Name"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["name"] == "New Name"

    async def test_update_organization_not_owner(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=other_headers,
            json={"name": "Hacked"},
        )
        assert response.status_code == 403


class TestDeleteOrganization:
    async def test_delete_organization_soft(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "To Delete"}
        )
        org_id = create_resp.json()["data"]["id"]

        response = await client.delete(
            f"/api/v1/organizations/{org_id}", headers=auth_headers
        )
        assert response.status_code == 200

        list_resp = await client.get("/api/v1/organizations", headers=auth_headers)
        assert len(list_resp.json()["data"]["items"]) == 0

    async def test_delete_organization_not_owner(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.delete(
            f"/api/v1/organizations/{org_id}", headers=other_headers
        )
        assert response.status_code == 403


class TestInviteCode:
    async def test_rotate_invite_code(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        old_code = create_resp.json()["data"]["invite_code"]

        response = await client.post(
            f"/api/v1/organizations/{org_id}/rotate-invite", headers=auth_headers
        )
        assert response.status_code == 200
        new_code = response.json()["data"]["invite_code"]
        assert new_code != old_code
        assert len(new_code) == 8

    async def test_join_by_invite_code(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        invite_code = create_resp.json()["data"]["invite_code"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.post(
            f"/api/v1/organizations/join/{invite_code}", headers=other_headers
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["organization_name"] == "Org"
        assert data["role"] == "employee"

    async def test_join_invalid_code(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/organizations/join/INVALID1", headers=auth_headers
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "INVALID_INVITE"

    async def test_join_already_member(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        invite_code = create_resp.json()["data"]["invite_code"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)
        response = await client.post(
            f"/api/v1/organizations/join/{invite_code}", headers=other_headers
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ALREADY_MEMBER"

    async def test_owner_cannot_join_own_org(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        invite_code = create_resp.json()["data"]["invite_code"]

        response = await client.post(
            f"/api/v1/organizations/join/{invite_code}", headers=auth_headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "OWNER_CANNOT_JOIN"

    async def test_join_deleted_org(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        await client.delete(f"/api/v1/organizations/{org_id}", headers=auth_headers)

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.post(
            f"/api/v1/organizations/join/{invite_code}", headers=other_headers
        )
        assert response.status_code == 404


class TestMembers:
    async def test_list_members(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)

        response = await client.get(
            f"/api/v1/organizations/{org_id}/members", headers=auth_headers
        )
        assert response.status_code == 200
        members = response.json()["data"]["items"]
        assert len(members) == 1
        assert members[0]["role"] == "employee"
        assert members[0]["user_email"] == "second@example.com"

    async def test_remove_member_by_owner(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        second_user = await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)

        response = await client.delete(
            f"/api/v1/organizations/{org_id}/members/{second_user.id}",
            headers=auth_headers,
        )
        assert response.status_code == 200

        members_resp = await client.get(
            f"/api/v1/organizations/{org_id}/members", headers=auth_headers
        )
        assert len(members_resp.json()["data"]["items"]) == 0

    async def test_member_self_leave(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        second_user = await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)

        response = await client.delete(
            f"/api/v1/organizations/{org_id}/members/{second_user.id}",
            headers=other_headers,
        )
        assert response.status_code == 200

    async def test_employee_cannot_remove_other(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        second_user = await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)

        third_user = User(
            id=uuid.uuid4(),
            email="third@example.com",
            password_hash=hash_password("Test1234"),
            name="Third User",
            is_verified=True,
        )
        db_session.add(third_user)
        await db_session.commit()
        third_headers = await _login_as(client, "third@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=third_headers)

        response = await client.delete(
            f"/api/v1/organizations/{org_id}/members/{third_user.id}",
            headers=other_headers,
        )
        assert response.status_code == 403

    async def test_list_members_forbidden(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.get(
            f"/api/v1/organizations/{org_id}/members", headers=other_headers
        )
        assert response.status_code == 403
