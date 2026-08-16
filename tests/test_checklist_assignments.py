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
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


async def _setup_org_with_roles_and_member(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
) -> dict:
    org_resp = await client.post(
        "/api/v1/organizations",
        headers=owner_headers,
        json={"name": "Cafe"},
    )
    org_id = org_resp.json()["data"]["id"]
    invite_code = org_resp.json()["data"]["invite_code"]

    role_resp = await client.post(
        f"/api/v1/organizations/{org_id}/roles",
        headers=owner_headers,
        json={"name": "Бариста"},
    )
    role_id = role_resp.json()["data"]["id"]

    member_user = await _create_user(db_session, "member@example.com")
    member_headers = await _login_as(client, "member@example.com")
    await client.post(
        f"/api/v1/organizations/join/{invite_code}",
        headers=member_headers,
    )
    return {
        "org_id": org_id,
        "role_id": role_id,
        "member_user": member_user,
        "member_headers": member_headers,
    }


async def _make_template(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    name: str = "Открытие",
    type_: str = "shift_start",
    is_required: bool = True,
) -> str:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/checklist-templates",
        headers=headers,
        json={"name": name, "type": type_, "is_required": is_required},
    )
    return resp.json()["data"]["id"]


class TestRoleAssignment:
    async def test_assign_and_replace(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        role1 = ctx["role_id"]
        role2_resp = await client.post(
            f"/api/v1/organizations/{ctx['org_id']}/roles",
            headers=super_admin_headers,
            json={"name": "Кассир"},
        )
        role2 = role2_resp.json()["data"]["id"]

        tpl_id = await _make_template(client, super_admin_headers, ctx["org_id"])

        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/roles",
            headers=super_admin_headers,
            json={"role_ids": [role1, role2]},
        )
        assert resp.status_code == 200
        assert set(resp.json()["data"]["role_ids"]) == {role1, role2}

        # Replace with only role1
        resp2 = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/roles",
            headers=super_admin_headers,
            json={"role_ids": [role1]},
        )
        assert set(resp2.json()["data"]["role_ids"]) == {role1}

        # Clear all
        resp3 = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/roles",
            headers=super_admin_headers,
            json={"role_ids": []},
        )
        assert resp3.json()["data"]["role_ids"] == []

    async def test_role_from_other_org_rejected(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        other_org_resp = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Other"},
        )
        other_org_id = other_org_resp.json()["data"]["id"]
        other_role_resp = await client.post(
            f"/api/v1/organizations/{other_org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Foreign"},
        )
        other_role_id = other_role_resp.json()["data"]["id"]

        tpl_id = await _make_template(client, super_admin_headers, ctx["org_id"])

        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/roles",
            headers=super_admin_headers,
            json={"role_ids": [other_role_id]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_ROLE"


class TestAssignmentView:
    async def test_view_includes_roles_and_overrides(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        tpl_id = await _make_template(client, super_admin_headers, ctx["org_id"])

        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/roles",
            headers=super_admin_headers,
            json={"role_ids": [ctx["role_id"]]},
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={"overrides": [{"template_id": tpl_id, "type": "add"}]},
        )

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/assignments",
            headers=super_admin_headers,
        )
        data = resp.json()["data"]
        assert data["role_ids"] == [ctx["role_id"]]
        assert len(data["personal_add"]) == 1
        assert data["personal_add"][0]["user_email"] == "member@example.com"
        assert data["personal_remove"] == []


class TestMemberOverrides:
    async def test_set_and_replace(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        tpl_a = await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            name="A",
        )
        tpl_b = await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            name="B",
        )

        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={
                "overrides": [
                    {"template_id": tpl_a, "type": "add"},
                    {"template_id": tpl_b, "type": "remove"},
                ],
            },
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]["overrides"]) == 2

        # Replace with only one
        resp2 = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={
                "overrides": [{"template_id": tpl_a, "type": "remove"}],
            },
        )
        ovs = resp2.json()["data"]["overrides"]
        assert len(ovs) == 1
        assert ovs[0]["type"] == "remove"

    async def test_duplicate_template_rejected(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={
                "overrides": [
                    {"template_id": tpl, "type": "add"},
                    {"template_id": tpl, "type": "remove"},
                ],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "DUPLICATE_TEMPLATE"

    async def test_invalid_override_type(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={
                "overrides": [{"template_id": tpl, "type": "toggle"}],
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_OVERRIDE_TYPE"


class TestEffectiveTemplates:
    async def test_no_role_no_overrides_empty(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=super_admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["items"] == []

    async def test_role_template_assigned(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        tpl = await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            name="T1",
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl}/roles",
            headers=super_admin_headers,
            json={"role_ids": [ctx["role_id"]]},
        )
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": ctx["role_id"]},
        )

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=super_admin_headers,
        )
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["id"] == tpl
        assert items[0]["source"] == "role"

    async def test_personal_add(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={"overrides": [{"template_id": tpl, "type": "add"}]},
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=super_admin_headers,
        )
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["source"] == "personal_add"

    async def test_personal_remove_subtracts_role(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        tpl_a = await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            name="A",
        )
        tpl_b = await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            name="B",
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_a}/roles",
            headers=super_admin_headers,
            json={"role_ids": [ctx["role_id"]]},
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_b}/roles",
            headers=super_admin_headers,
            json={"role_ids": [ctx["role_id"]]},
        )
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": ctx["role_id"]},
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={"overrides": [{"template_id": tpl_b, "type": "remove"}]},
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=super_admin_headers,
        )
        ids = {item["id"] for item in resp.json()["data"]["items"]}
        assert ids == {tpl_a}

    async def test_deleted_filtered(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl}/roles",
            headers=super_admin_headers,
            json={"role_ids": [ctx["role_id"]]},
        )
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": ctx["role_id"]},
        )
        await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl}",
            headers=super_admin_headers,
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=super_admin_headers,
        )
        assert resp.json()["data"]["items"] == []

    async def test_member_sees_own(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl}/roles",
            headers=super_admin_headers,
            json={"role_ids": [ctx["role_id"]]},
        )
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": ctx["role_id"]},
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=ctx["member_headers"],
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]["items"]) == 1

    async def test_member_cannot_view_others(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_roles_and_member(
            client,
            db_session,
            super_admin_headers,
        )
        await _create_user(db_session, "other@example.com")
        other_headers = await _login_as(client, "other@example.com")
        invite_resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}",
            headers=super_admin_headers,
        )
        invite = invite_resp.json()["data"]["invite_code"]
        await client.post(
            f"/api/v1/organizations/join/{invite}",
            headers=other_headers,
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=other_headers,
        )
        assert resp.status_code == 403
