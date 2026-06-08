import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.user import User, UserRole


async def _create_user(
    db_session: AsyncSession,
    email: str,
    name: str = "User",
    role: UserRole = UserRole.user,
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("Test1234"),
        name=name,
        is_verified=True,
        role=role,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _login_as(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


class TestListMyOrganizations:
    async def test_owner(
        self, client: AsyncClient, super_admin_headers
    ):
        create_resp = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Cafe"},
        )
        assert create_resp.json()["data"]["my_role"] == "owner"
        assert create_resp.json()["data"]["my_custom_role"] is None

        list_resp = await client.get(
            "/api/v1/organizations", headers=super_admin_headers,
        )
        items = list_resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["my_role"] == "owner"
        assert items[0]["my_custom_role"] is None

    async def test_employee_without_custom_role(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Cafe"},
        )
        invite = create_resp.json()["data"]["invite_code"]
        await _create_user(db_session, "emp@example.com")
        emp_headers = await _login_as(client, "emp@example.com")
        await client.post(
            f"/api/v1/organizations/join/{invite}", headers=emp_headers,
        )

        list_resp = await client.get(
            "/api/v1/organizations", headers=emp_headers,
        )
        items = list_resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["my_role"] == "employee"
        assert items[0]["my_custom_role"] is None

    async def test_admin_role(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Cafe"},
        )
        org_id = create_resp.json()["data"]["id"]
        invite = create_resp.json()["data"]["invite_code"]
        member = await _create_user(db_session, "mod@example.com")
        member_headers = await _login_as(client, "mod@example.com")
        await client.post(
            f"/api/v1/organizations/join/{invite}", headers=member_headers,
        )
        await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/role",
            headers=super_admin_headers,
            json={"role": "admin"},
        )
        list_resp = await client.get(
            "/api/v1/organizations", headers=member_headers,
        )
        assert list_resp.json()["data"]["items"][0]["my_role"] == "admin"

    async def test_employee_with_custom_role(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Cafe"},
        )
        org_id = create_resp.json()["data"]["id"]
        invite = create_resp.json()["data"]["invite_code"]
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        role_id = role_resp.json()["data"]["id"]

        member = await _create_user(db_session, "barista@example.com")
        member_headers = await _login_as(client, "barista@example.com")
        await client.post(
            f"/api/v1/organizations/join/{invite}", headers=member_headers,
        )
        await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": role_id},
        )
        list_resp = await client.get(
            "/api/v1/organizations", headers=member_headers,
        )
        item = list_resp.json()["data"]["items"][0]
        assert item["my_role"] == "employee"
        assert item["my_custom_role"]["id"] == role_id
        assert item["my_custom_role"]["name"] == "Бариста"
        assert "created_at" in item["my_custom_role"]

    async def test_multiple_orgs_different_roles(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        # Org1: user is owner
        await _create_user(
            db_session, "other@example.com", role=UserRole.super_admin,
        )
        other_admin_headers = await _login_as(client, "other@example.com")

        my_org = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Mine"},
        )

        # Org2: user joins as employee
        other_org = await client.post(
            "/api/v1/organizations",
            headers=other_admin_headers,
            json={"name": "Other"},
        )
        other_invite = other_org.json()["data"]["invite_code"]
        await client.post(
            f"/api/v1/organizations/join/{other_invite}",
            headers=super_admin_headers,
        )

        list_resp = await client.get(
            "/api/v1/organizations", headers=super_admin_headers,
        )
        items = list_resp.json()["data"]["items"]
        by_id = {i["id"]: i for i in items}
        assert by_id[my_org.json()["data"]["id"]]["my_role"] == "owner"
        assert by_id[other_org.json()["data"]["id"]]["my_role"] == "employee"


class TestGetOrganizationById:
    async def test_owner_by_id(
        self, client: AsyncClient, super_admin_headers
    ):
        create = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Cafe"},
        )
        org_id = create.json()["data"]["id"]
        resp = await client.get(
            f"/api/v1/organizations/{org_id}", headers=super_admin_headers,
        )
        assert resp.json()["data"]["my_role"] == "owner"
        assert resp.json()["data"]["my_custom_role"] is None

    async def test_member_with_custom_role_by_id(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        create = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Cafe"},
        )
        org_id = create.json()["data"]["id"]
        invite = create.json()["data"]["invite_code"]
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Кассир"},
        )
        role_id = role_resp.json()["data"]["id"]
        member = await _create_user(db_session, "cash@example.com")
        member_headers = await _login_as(client, "cash@example.com")
        await client.post(
            f"/api/v1/organizations/join/{invite}", headers=member_headers,
        )
        await client.patch(
            f"/api/v1/organizations/{org_id}/members/{member.id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": role_id},
        )
        resp = await client.get(
            f"/api/v1/organizations/{org_id}", headers=member_headers,
        )
        data = resp.json()["data"]
        assert data["my_role"] == "employee"
        assert data["my_custom_role"]["name"] == "Кассир"


class TestSuperAdminAll:
    async def test_my_role_null_for_foreign_org(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        await _create_user(
            db_session, "other-sa@example.com", role=UserRole.super_admin,
        )
        other_headers = await _login_as(client, "other-sa@example.com")
        foreign = await client.post(
            "/api/v1/organizations",
            headers=other_headers,
            json={"name": "Foreign"},
        )
        foreign_id = foreign.json()["data"]["id"]

        resp = await client.get(
            "/api/v1/organizations/all", headers=super_admin_headers,
        )
        items = resp.json()["data"]["items"]
        assert len(items) >= 1
        found = [i for i in items if i["id"] == foreign_id][0]
        assert found["my_role"] is None
        assert found["my_custom_role"] is None

    async def test_my_role_owner_for_own_org_in_all(
        self, client: AsyncClient, super_admin_headers
    ):
        own = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Own"},
        )
        own_id = own.json()["data"]["id"]
        resp = await client.get(
            "/api/v1/organizations/all", headers=super_admin_headers,
        )
        found = [i for i in resp.json()["data"]["items"] if i["id"] == own_id][0]
        assert found["my_role"] == "owner"


class TestNoNPlusOne:
    async def test_list_does_not_issue_n_plus_one_membership_queries(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        from sqlalchemy import event

        # Seed 3 orgs where super_admin is owner, 3 where member with custom role.
        for i in range(3):
            await client.post(
                "/api/v1/organizations",
                headers=super_admin_headers,
                json={"name": f"Own-{i}"},
            )
        invites = []
        for i in range(3):
            await _create_user(
                db_session, f"sa-{i}@example.com", role=UserRole.super_admin,
            )
            oh = await _login_as(client, f"sa-{i}@example.com")
            r = await client.post(
                "/api/v1/organizations",
                headers=oh,
                json={"name": f"Other-{i}"},
            )
            invites.append(r.json()["data"]["invite_code"])
        for invite in invites:
            await client.post(
                f"/api/v1/organizations/join/{invite}",
                headers=super_admin_headers,
            )

        sync_engine = db_session.bind.sync_engine
        counter = {"n": 0}

        def on_execute(conn, cursor, statement, *args):
            if "organization_members" in statement.lower():
                counter["n"] += 1

        event.listen(sync_engine, "before_cursor_execute", on_execute)
        try:
            resp = await client.get(
                "/api/v1/organizations", headers=super_admin_headers,
            )
        finally:
            event.remove(sync_engine, "before_cursor_execute", on_execute)

        assert resp.status_code == 200
        assert len(resp.json()["data"]["items"]) == 6
        # Expected: 1 query in get_user_organizations (member join)
        # + 1 query in batch_get_my_roles (non-owned memberships).
        # Absolute cap 3 to allow for potential minor refactors.
        assert counter["n"] <= 3, (
            f"Expected ≤3 queries hitting organization_members, got {counter['n']}"
        )
