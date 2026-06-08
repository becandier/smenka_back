import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.checklist import ChecklistMemberOverride
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


async def _setup(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
    member_email: str = "member@example.com",
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

    member_user = await _create_user(db_session, member_email)
    member_headers = await _login_as(client, member_email)
    await client.post(
        f"/api/v1/organizations/join/{invite_code}",
        headers=member_headers,
    )
    return {
        "org_id": org_id,
        "role_id": role_id,
        "member_user": member_user,
        "member_headers": member_headers,
        "invite_code": invite_code,
    }


async def _make_template(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    name: str = "T",
    type_: str = "shift_start",
    is_required: bool = False,
) -> str:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/checklist-templates",
        headers=headers,
        json={"name": name, "type": type_, "is_required": is_required},
    )
    return resp.json()["data"]["id"]


class TestListOverrides:
    async def test_empty_for_new_member(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["items"] == []

    async def test_returns_add_and_remove_with_template_fields(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t_add = await _make_template(
            client, super_admin_headers, ctx["org_id"], name="Alpha",
        )
        t_rem = await _make_template(
            client, super_admin_headers, ctx["org_id"],
            name="Omega", type_="shift_end",
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t_add}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t_rem}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "remove"},
        )

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
        )
        items = resp.json()["data"]["items"]
        assert len(items) == 2
        by_tpl = {it["template_id"]: it for it in items}
        assert by_tpl[t_add]["type"] == "add"
        assert by_tpl[t_add]["template_name"] == "Alpha"
        assert by_tpl[t_add]["template_type"] == "shift_start"
        assert by_tpl[t_rem]["type"] == "remove"
        assert by_tpl[t_rem]["template_type"] == "shift_end"

    async def test_includes_overrides_of_archived_templates(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}",
            headers=super_admin_headers,
        )

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
        )
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["template_id"] == t

    async def test_self_can_view(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=ctx["member_headers"],
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]["items"]) == 1

    async def test_other_employee_forbidden(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _create_user(db_session, "other@example.com")
        other_headers = await _login_as(client, "other@example.com")
        await client.post(
            f"/api/v1/organizations/join/{ctx['invite_code']}",
            headers=other_headers,
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=other_headers,
        )
        assert resp.status_code == 403


class TestUpsertOverride:
    async def test_create_when_absent(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["type"] == "add"
        assert data["template_id"] == t
        assert data["user_id"] == str(ctx["member_user"].id)

    async def test_update_existing(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "remove"},
        )
        assert resp.json()["data"]["type"] == "remove"

        # Verify only one row in DB (no duplicates)
        rows = await db_session.execute(
            select(ChecklistMemberOverride).where(
                ChecklistMemberOverride.template_id == uuid.UUID(t),
            )
        )
        assert len(list(rows.scalars().all())) == 1

    async def test_template_from_other_org_404(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        other_org_resp = await client.post(
            "/api/v1/organizations",
            headers=super_admin_headers,
            json={"name": "Other"},
        )
        other_org_id = other_org_resp.json()["data"]["id"]
        foreign_t = await _make_template(
            client, super_admin_headers, other_org_id,
        )
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{foreign_t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "TEMPLATE_NOT_FOUND"

    async def test_user_not_member_404(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        stranger = await _create_user(db_session, "stranger@example.com")
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{stranger.id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "MEMBER_NOT_FOUND"

    async def test_archived_template_rejected(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}",
            headers=super_admin_headers,
        )
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "TEMPLATE_ARCHIVED"

    async def test_employee_forbidden(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=ctx["member_headers"],
            json={"type": "add"},
        )
        assert resp.status_code == 403

    async def test_invalid_type(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "toggle"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_OVERRIDE_TYPE"


class TestDeleteOverride:
    async def test_delete_existing(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        resp = await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
        )
        assert resp.status_code == 200

        list_resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
        )
        assert list_resp.json()["data"]["items"] == []

    async def test_idempotent_when_absent(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        resp = await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
        )
        assert resp.status_code == 200

    async def test_delete_on_archived_allowed(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}",
            headers=super_admin_headers,
        )
        resp = await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
        )
        assert resp.status_code == 200

    async def test_foreign_template_404(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        other = await client.post(
            "/api/v1/organizations", headers=super_admin_headers,
            json={"name": "Other"},
        )
        foreign_t = await _make_template(
            client, super_admin_headers, other.json()["data"]["id"],
        )
        resp = await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{foreign_t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
        )
        assert resp.status_code == 404


class TestEffectiveAfterGranular:
    async def test_put_add_appears_in_effective(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=super_admin_headers,
        )
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["source"] == "personal_add"

    async def test_put_remove_subtracts_role_template(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/roles",
            headers=super_admin_headers,
            json={"role_ids": [ctx["role_id"]]},
        )
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": ctx["role_id"]},
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "remove"},
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=super_admin_headers,
        )
        assert resp.json()["data"]["items"] == []

    async def test_delete_returns_to_role_default(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/roles",
            headers=super_admin_headers,
            json={"role_ids": [ctx["role_id"]]},
        )
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/custom-role",
            headers=super_admin_headers,
            json={"role_id": ctx["role_id"]},
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "remove"},
        )
        await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
        )
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=super_admin_headers,
        )
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["source"] == "role"


class TestBulkPutBackwardCompat:
    async def test_bulk_put_still_works(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t_a = await _make_template(client, super_admin_headers, ctx["org_id"], name="A")
        t_b = await _make_template(client, super_admin_headers, ctx["org_id"], name="B")

        # seed via granular PUT
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t_a}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        # bulk PUT replaces entirely
        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={"overrides": [{"template_id": t_b, "type": "remove"}]},
        )
        assert resp.status_code == 200

        list_resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
        )
        items = list_resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["template_id"] == t_b
        assert items[0]["type"] == "remove"


class TestMemberRemovalCascade:
    async def test_overrides_cascade_on_member_removal(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        t = await _make_template(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{t}/personal/{ctx['member_user'].id}",
            headers=super_admin_headers,
            json={"type": "add"},
        )
        remove_resp = await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}",
            headers=super_admin_headers,
        )
        assert remove_resp.status_code == 200

        rows = await db_session.execute(
            select(ChecklistMemberOverride).where(
                ChecklistMemberOverride.template_id == uuid.UUID(t),
            )
        )
        assert list(rows.scalars().all()) == []
