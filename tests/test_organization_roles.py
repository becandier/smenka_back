import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.user import User


async def _create_user(
    db_session: AsyncSession,
    email: str,
    name: str = "User",
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("Test1234"),
        name=name,
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


async def _create_org_with_member(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    member_email: str = "member@example.com",
) -> tuple[str, User, dict[str, str]]:
    create_resp = await client.post(
        "/api/v1/organizations",
        headers=owner_headers,
        json={"name": "Cafe"},
    )
    org_id = create_resp.json()["data"]["id"]
    invite_code = create_resp.json()["data"]["invite_code"]

    member = await _create_user(db_session, member_email, name="Member")
    member_headers = await _login_as(client, member_email)
    await client.post(
        f"/api/v1/organizations/join/{invite_code}",
        headers=member_headers,
    )
    return org_id, member, member_headers


class TestCreateRole:
    async def test_owner_creates_role(
        self, client: AsyncClient, super_admin_headers
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org"},
        )
        org_id = create_resp.json()["data"]["id"]

        response = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["name"] == "Бариста"
        assert "id" in data
        assert "created_at" in data

    async def test_duplicate_name_rejected(
        self, client: AsyncClient, super_admin_headers
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org"},
        )
        org_id = create_resp.json()["data"]["id"]
        await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        response = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ROLE_NAME_TAKEN"

    async def test_same_name_different_orgs_allowed(
        self, client: AsyncClient, super_admin_headers
    ):
        resp1 = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org1"},
        )
        resp2 = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org2"},
        )
        org1, org2 = resp1.json()["data"]["id"], resp2.json()["data"]["id"]
        r1 = await client.post(
            f"/api/v1/organizations/{org1}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        r2 = await client.post(
            f"/api/v1/organizations/{org2}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201

    async def test_admin_can_create_role(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id, member, _ = await _create_org_with_member(
            client, db_session, super_admin_headers,
        )
        await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/role",
            headers=super_admin_headers,
            json={"role": "admin"},
        )
        admin_headers = await _login_as(client, "member@example.com")

        response = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=admin_headers,
            json={"name": "Кассир"},
        )
        assert response.status_code == 201

    async def test_employee_cannot_create_role(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id, _, member_headers = await _create_org_with_member(
            client, db_session, super_admin_headers,
        )
        response = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=member_headers,
            json={"name": "Кассир"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"


class TestListRoles:
    async def test_list_empty(self, client: AsyncClient, super_admin_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org"},
        )
        org_id = create_resp.json()["data"]["id"]
        response = await client.get(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["items"] == []

    async def test_list_ordered_by_creation(
        self, client: AsyncClient, super_admin_headers
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org"},
        )
        org_id = create_resp.json()["data"]["id"]
        for name in ["Бариста", "Кассир", "Стажёр"]:
            await client.post(
                f"/api/v1/organizations/{org_id}/roles",
                headers=super_admin_headers,
                json={"name": name},
            )
        response = await client.get(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
        )
        items = response.json()["data"]["items"]
        assert [i["name"] for i in items] == ["Бариста", "Кассир", "Стажёр"]

    async def test_employee_can_list_roles(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id, _, member_headers = await _create_org_with_member(
            client, db_session, super_admin_headers,
        )
        await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        response = await client.get(
            f"/api/v1/organizations/{org_id}/roles",
            headers=member_headers,
        )
        assert response.status_code == 200
        assert len(response.json()["data"]["items"]) == 1

    async def test_outsider_cannot_list_roles(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org"},
        )
        org_id = create_resp.json()["data"]["id"]
        await _create_user(db_session, "outsider@example.com")
        outsider_headers = await _login_as(client, "outsider@example.com")
        response = await client.get(
            f"/api/v1/organizations/{org_id}/roles",
            headers=outsider_headers,
        )
        assert response.status_code == 403


class TestUpdateRole:
    async def test_rename_role(self, client: AsyncClient, super_admin_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org"},
        )
        org_id = create_resp.json()["data"]["id"]
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        role_id = role_resp.json()["data"]["id"]
        response = await client.patch(
            f"/api/v1/organizations/{org_id}/roles/{role_id}",
            headers=super_admin_headers,
            json={"name": "Старший бариста"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["name"] == "Старший бариста"

    async def test_rename_to_existing_rejected(
        self, client: AsyncClient, super_admin_headers
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org"},
        )
        org_id = create_resp.json()["data"]["id"]
        r1 = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        r2 = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Кассир"},
        )
        response = await client.patch(
            f"/api/v1/organizations/{org_id}/roles/{r2.json()['data']['id']}",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        assert response.status_code == 409

    async def test_update_nonexistent(self, client: AsyncClient, super_admin_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org"},
        )
        org_id = create_resp.json()["data"]["id"]
        response = await client.patch(
            f"/api/v1/organizations/{org_id}/roles/{uuid.uuid4()}",
            headers=super_admin_headers,
            json={"name": "X"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ROLE_NOT_FOUND"


class TestDeleteRole:
    async def test_delete_role(self, client: AsyncClient, super_admin_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Org"},
        )
        org_id = create_resp.json()["data"]["id"]
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        role_id = role_resp.json()["data"]["id"]
        response = await client.delete(
            f"/api/v1/organizations/{org_id}/roles/{role_id}",
            headers=super_admin_headers,
        )
        assert response.status_code == 200
        list_resp = await client.get(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
        )
        assert list_resp.json()["data"]["items"] == []

    async def test_delete_role_unassigns_members(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id, member, _ = await _create_org_with_member(
            client, db_session, super_admin_headers,
        )
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        role_id = role_resp.json()["data"]["id"]
        assign_resp = await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": role_id},
        )
        assert assign_resp.json()["data"]["custom_role"]["id"] == role_id

        await client.delete(
            f"/api/v1/organizations/{org_id}/roles/{role_id}",
            headers=super_admin_headers,
        )

        members_resp = await client.get(
            f"/api/v1/organizations/{org_id}/members",
            headers=super_admin_headers,
        )
        member_data = members_resp.json()["data"]["items"][0]
        assert member_data["custom_role"] is None


class TestAssignRoleToMember:
    async def test_assign_and_unassign(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id, member, _ = await _create_org_with_member(
            client, db_session, super_admin_headers,
        )
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        role_id = role_resp.json()["data"]["id"]

        assign = await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": role_id},
        )
        assert assign.status_code == 200
        assert assign.json()["data"]["custom_role"]["name"] == "Бариста"

        unassign = await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": None},
        )
        assert unassign.status_code == 200
        assert unassign.json()["data"]["custom_role"] is None

    async def test_assign_nonexistent_role(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id, member, _ = await _create_org_with_member(
            client, db_session, super_admin_headers,
        )
        response = await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": str(uuid.uuid4())},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ROLE_NOT_FOUND"

    async def test_assign_role_from_other_org(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org1_id, member, _ = await _create_org_with_member(
            client, db_session, super_admin_headers,
        )
        org2_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Other"},
        )
        org2_id = org2_resp.json()["data"]["id"]
        role_resp = await client.post(
            f"/api/v1/organizations/{org2_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        role_id = role_resp.json()["data"]["id"]
        response = await client.patch(
            f"/api/v1/organizations/{org1_id}/members/{member.id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": role_id},
        )
        assert response.status_code == 404

    async def test_employee_cannot_assign(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id, member, member_headers = await _create_org_with_member(
            client, db_session, super_admin_headers,
        )
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        role_id = role_resp.json()["data"]["id"]
        response = await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/custom-role",
            headers=member_headers,
            json={"role_id": role_id},
        )
        assert response.status_code == 403

    async def test_member_response_includes_custom_role(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id, member, _ = await _create_org_with_member(
            client, db_session, super_admin_headers,
        )
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        role_id = role_resp.json()["data"]["id"]
        await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": role_id},
        )
        members_resp = await client.get(
            f"/api/v1/organizations/{org_id}/members",
            headers=super_admin_headers,
        )
        item = members_resp.json()["data"]["items"][0]
        assert item["custom_role"]["id"] == role_id
        assert item["custom_role"]["name"] == "Бариста"
