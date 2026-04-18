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


async def _make_org(
    client: AsyncClient,
    owner_headers: dict[str, str],
) -> str:
    resp = await client.post(
        "/api/v1/organizations", headers=owner_headers, json={"name": "Cafe"},
    )
    return resp.json()["data"]["id"]


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


class TestCreateTemplate:
    async def test_owner_creates(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        response = await client.post(
            f"/api/v1/organizations/{org_id}/checklist-templates",
            headers=super_admin_headers,
            json={"name": "Открытие смены", "type": "shift_start", "is_required": True},
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["name"] == "Открытие смены"
        assert data["type"] == "shift_start"
        assert data["is_required"] is True
        assert data["items_count"] == 0
        assert data["is_archived"] is False

    async def test_invalid_type(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        response = await client.post(
            f"/api/v1/organizations/{org_id}/checklist-templates",
            headers=super_admin_headers,
            json={"name": "X", "type": "invalid", "is_required": False},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_TYPE"

    async def test_employee_forbidden(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id = await _make_org(client, super_admin_headers)
        invite_resp = await client.get(
            f"/api/v1/organizations/{org_id}", headers=super_admin_headers,
        )
        invite_code = invite_resp.json()["data"]["invite_code"]
        await _create_user(db_session, "emp@example.com")
        emp_headers = await _login_as(client, "emp@example.com")
        await client.post(
            f"/api/v1/organizations/join/{invite_code}", headers=emp_headers,
        )
        response = await client.post(
            f"/api/v1/organizations/{org_id}/checklist-templates",
            headers=emp_headers,
            json={"name": "X", "type": "shift_start", "is_required": False},
        )
        assert response.status_code == 403


class TestListTemplates:
    async def test_list_empty(self, client: AsyncClient, super_admin_headers):
        org_id = await _make_org(client, super_admin_headers)
        response = await client.get(
            f"/api/v1/organizations/{org_id}/checklist-templates",
            headers=super_admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["items"] == []

    async def test_archived_hidden_by_default(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        await client.delete(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}",
            headers=super_admin_headers,
        )
        response = await client.get(
            f"/api/v1/organizations/{org_id}/checklist-templates",
            headers=super_admin_headers,
        )
        assert response.json()["data"]["items"] == []

    async def test_include_archived(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        await client.delete(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}",
            headers=super_admin_headers,
        )
        response = await client.get(
            f"/api/v1/organizations/{org_id}/checklist-templates?include_archived=true",
            headers=super_admin_headers,
        )
        items = response.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["is_archived"] is True

    async def test_items_count(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        for i in range(3):
            await client.post(
                f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
                headers=super_admin_headers,
                json={"text": f"Пункт {i}", "is_required": True},
            )
        response = await client.get(
            f"/api/v1/organizations/{org_id}/checklist-templates",
            headers=super_admin_headers,
        )
        assert response.json()["data"]["items"][0]["items_count"] == 3


class TestUpdateTemplate:
    async def test_partial_update(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        response = await client.patch(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}",
            headers=super_admin_headers,
            json={"name": "Новое имя", "is_required": False},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["name"] == "Новое имя"
        assert data["is_required"] is False
        assert data["type"] == "shift_start"

    async def test_update_type(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        response = await client.patch(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}",
            headers=super_admin_headers,
            json={"type": "shift_end"},
        )
        assert response.json()["data"]["type"] == "shift_end"

    async def test_not_found(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        response = await client.patch(
            f"/api/v1/organizations/{org_id}/checklist-templates/{uuid.uuid4()}",
            headers=super_admin_headers,
            json={"name": "X"},
        )
        assert response.status_code == 404


class TestDeleteTemplate:
    async def test_archive(self, client: AsyncClient, super_admin_headers):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        response = await client.delete(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}",
            headers=super_admin_headers,
        )
        assert response.status_code == 200


class TestItems:
    async def test_add_item(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        response = await client.post(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
            headers=super_admin_headers,
            json={"text": "Помыть стойку", "is_required": True},
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["text"] == "Помыть стойку"
        assert data["position"] == 0
        assert data["is_required"] is True

    async def test_position_auto_increment(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        positions = []
        for i in range(3):
            resp = await client.post(
                f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
                headers=super_admin_headers,
                json={"text": f"#{i}", "is_required": False},
            )
            positions.append(resp.json()["data"]["position"])
        assert positions == [0, 1, 2]

    async def test_update_item(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        item_resp = await client.post(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
            headers=super_admin_headers,
            json={"text": "Старый", "is_required": False},
        )
        item_id = item_resp.json()["data"]["id"]
        response = await client.patch(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items/{item_id}",
            headers=super_admin_headers,
            json={"text": "Новый", "is_required": True},
        )
        assert response.status_code == 200
        assert response.json()["data"]["text"] == "Новый"
        assert response.json()["data"]["is_required"] is True

    async def test_delete_item(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        item_resp = await client.post(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
            headers=super_admin_headers,
            json={"text": "X", "is_required": False},
        )
        item_id = item_resp.json()["data"]["id"]
        response = await client.delete(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items/{item_id}",
            headers=super_admin_headers,
        )
        assert response.status_code == 200

        detail = await client.get(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}",
            headers=super_admin_headers,
        )
        assert detail.json()["data"]["items"] == []

    async def test_detail_returns_items_ordered(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        for name in ["A", "B", "C"]:
            await client.post(
                f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
                headers=super_admin_headers,
                json={"text": name, "is_required": False},
            )
        detail = await client.get(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}",
            headers=super_admin_headers,
        )
        items = detail.json()["data"]["items"]
        assert [i["text"] for i in items] == ["A", "B", "C"]
        assert [i["position"] for i in items] == [0, 1, 2]


class TestReorder:
    async def test_reorder_success(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        ids = []
        for name in ["A", "B", "C"]:
            resp = await client.post(
                f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
                headers=super_admin_headers,
                json={"text": name, "is_required": False},
            )
            ids.append(resp.json()["data"]["id"])
        reverse_ids = list(reversed(ids))
        response = await client.put(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items/reorder",
            headers=super_admin_headers,
            json={"item_ids": reverse_ids},
        )
        assert response.status_code == 200
        detail = await client.get(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}",
            headers=super_admin_headers,
        )
        items = detail.json()["data"]["items"]
        assert [i["text"] for i in items] == ["C", "B", "A"]

    async def test_reorder_mismatch(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _make_org(client, super_admin_headers)
        tpl_id = await _make_template(client, super_admin_headers, org_id)
        ids = []
        for _ in range(3):
            resp = await client.post(
                f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
                headers=super_admin_headers,
                json={"text": "X", "is_required": False},
            )
            ids.append(resp.json()["data"]["id"])
        response = await client.put(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items/reorder",
            headers=super_admin_headers,
            json={"item_ids": ids[:2]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ITEMS_MISMATCH"
