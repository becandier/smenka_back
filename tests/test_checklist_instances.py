import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.main import app
from src.app.models.organization import Organization
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User
from src.app.services import checklist_instance as instance_service
from src.app.services.checklist_template import ChecklistError


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

    member_user = await _create_user(db_session, "worker@example.com")
    member_headers = await _login_as(client, "worker@example.com")
    await client.post(
        f"/api/v1/organizations/join/{invite_code}",
        headers=member_headers,
    )
    await client.patch(
        f"/api/v1/organizations/{org_id}/members/{member_user.id}/custom-role",
        headers=owner_headers,
        json={"role_id": role_id},
    )

    return {
        "org_id": org_id,
        "role_id": role_id,
        "member_user": member_user,
        "member_headers": member_headers,
    }


async def _make_template_with_items(
    client: AsyncClient,
    owner_headers: dict[str, str],
    org_id: str,
    role_id: str,
    *,
    name: str = "Открытие",
    type_: str = "shift_start",
    is_required: bool = True,
    items: list[tuple[str, bool]] | None = None,
) -> str:
    tpl_resp = await client.post(
        f"/api/v1/organizations/{org_id}/checklist-templates",
        headers=owner_headers,
        json={"name": name, "type": type_, "is_required": is_required},
    )
    tpl_id = tpl_resp.json()["data"]["id"]
    for text, required in items or []:
        await client.post(
            f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
            headers=owner_headers,
            json={"text": text, "is_required": required},
        )
    await client.put(
        f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/roles",
        headers=owner_headers,
        json={"role_ids": [role_id]},
    )
    return tpl_id


async def _start_org_shift(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
) -> str:
    resp = await client.post(
        "/api/v1/shifts/start",
        headers=headers,
        json={"organization_id": org_id},
    )
    return resp.json()["data"]["id"]


class TestInstanceCreation:
    async def test_instances_created_on_org_shift_start(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            name="A",
            items=[("P1", True), ("P2", False)],
        )
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            name="B",
            type_="shift_end",
            items=[("Q1", True)],
        )

        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        assert resp.json()["data"]["organization_timezone"] == "Europe/Moscow"
        items = resp.json()["data"]["items"]
        assert len(items) == 2
        names = {i["name"] for i in items}
        assert names == {"A", "B"}

    async def test_personal_shift_no_instances(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("X", True)],
        )

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={},
        )
        shift_id = resp.json()["data"]["id"]
        r = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        assert r.json()["data"]["organization_timezone"] is None
        assert r.json()["data"]["items"] == []

    async def test_snapshot_includes_items(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("P1", True), ("P2", False), ("P3", True)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        inst_id = list_resp.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        assert detail.json()["data"]["organization_timezone"] == "Europe/Moscow"
        items = detail.json()["data"]["items"]
        assert [it["text"] for it in items] == ["P1", "P2", "P3"]
        assert all(it["is_completed"] is False for it in items)


def test_timezone_contract_is_represented_in_openapi() -> None:
    runtime_schema = app.openapi()
    snapshot_schema = json.loads(
        (Path(__file__).resolve().parents[1] / "docs" / "openapi.json").read_text()
    )
    shift_schema = runtime_schema["components"]["schemas"]["ShiftResponse"]
    timezone_schema = shift_schema["properties"]["organization_timezone"]["anyOf"]
    assert {item.get("type") for item in timezone_schema} == {"string", "null"}
    snapshot_timezone_schema = snapshot_schema["components"]["schemas"]["ShiftResponse"][
        "properties"
    ]["organization_timezone"]["anyOf"]
    assert {item.get("type") for item in snapshot_timezone_schema} == {"string", "null"}
    assert (
        snapshot_schema["components"]["schemas"]["ShiftResponse"]
        == runtime_schema["components"]["schemas"]["ShiftResponse"]
    )

    timestamp_fields = {
        "ShiftResponse": [
            "started_at",
            "finished_at",
            "scheduled_start_at",
            "scheduled_end_at",
            "edited_at",
        ],
        "PauseResponse": ["started_at", "finished_at"],
        "ChecklistInstanceResponse": ["completed_at", "created_at"],
        "ChecklistInstanceDetailResponse": ["completed_at", "created_at"],
        "InstanceItemResponse": ["completed_at"],
        "PhotoResponse": ["captured_at", "url_expires_at"],
    }
    for component, fields in timestamp_fields.items():
        for field in fields:
            runtime_property = runtime_schema["components"]["schemas"][component]["properties"][
                field
            ]
            snapshot_property = snapshot_schema["components"]["schemas"][component]["properties"][
                field
            ]
            runtime_formats = {
                item.get("format")
                for item in runtime_property.get("anyOf", [runtime_property])
                if item.get("format") is not None
            }
            snapshot_formats = {
                item.get("format")
                for item in snapshot_property.get("anyOf", [snapshot_property])
                if item.get("format") is not None
            }
            assert runtime_formats == {"date-time"}
            assert snapshot_formats == {"date-time"}

    expected_shift_responses = {
        ("/api/v1/shifts", "get"): "ApiResponse_ShiftListResponse_",
        ("/api/v1/shifts/start", "post"): "ApiResponse_ShiftResponse_",
        ("/api/v1/shifts/{shift_id}", "get"): "ApiResponse_ShiftResponse_",
        ("/api/v1/shifts/{shift_id}/pause", "post"): "ApiResponse_ShiftResponse_",
        ("/api/v1/shifts/{shift_id}/resume", "post"): "ApiResponse_ShiftResponse_",
        ("/api/v1/shifts/{shift_id}/finish", "post"): "ApiResponse_ShiftResponse_",
        ("/api/v1/organizations/{org_id}/shifts", "get"): "ApiResponse_ShiftListResponse_",
        ("/api/v1/organizations/{org_id}/shifts", "post"): "ApiResponse_ShiftResponse_",
        ("/api/v1/organizations/{org_id}/shifts/{shift_id}", "get"): "ApiResponse_ShiftResponse_",
        (
            "/api/v1/organizations/{org_id}/shifts/{shift_id}",
            "patch",
        ): "ApiResponse_ShiftResponse_",
        (
            "/api/v1/organizations/{org_id}/shifts/{shift_id}/restore",
            "post",
        ): "ApiResponse_ShiftResponse_",
        (
            "/api/v1/organizations/{org_id}/shifts/{shift_id}/schedule",
            "patch",
        ): "ApiResponse_ShiftResponse_",
    }
    for (path, method), component in expected_shift_responses.items():
        status_code = (
            "201"
            if (method, path)
            in {
                ("post", "/api/v1/shifts/start"),
                ("post", "/api/v1/organizations/{org_id}/shifts"),
            }
            else "200"
        )
        response_schema = runtime_schema["paths"][path][method]["responses"][status_code][
            "content"
        ]["application/json"]["schema"]
        assert response_schema["$ref"] == f"#/components/schemas/{component}"

    affected_components = {
        "ApiResponse_ChecklistInstanceDetailResponse_",
        "ApiResponse_ChecklistInstanceListResponse_",
        "ApiResponse_ShiftListResponse_",
        "ApiResponse_ShiftResponse_",
        "ChecklistInstanceDetailResponse",
        "ChecklistInstanceListResponse",
        "ChecklistInstanceResponse",
        "InstanceItemResponse",
        "ItemsSummary",
        "OvertimeInfo",
        "PauseResponse",
        "PhotoResponse",
        "ShiftChecklistsSummary",
        "ShiftEarnings",
        "ShiftListResponse",
        "ShiftResponse",
        "ShiftWorkLocation",
    }
    for component in affected_components:
        assert (
            snapshot_schema["components"]["schemas"][component]
            == runtime_schema["components"]["schemas"][component]
        )

    snapshot_shift_responses = {
        (path, method): component
        for (path, method), component in expected_shift_responses.items()
        if (path, method)
        in {
            ("/api/v1/shifts", "get"),
            ("/api/v1/shifts/start", "post"),
            ("/api/v1/shifts/{shift_id}/pause", "post"),
            ("/api/v1/shifts/{shift_id}/resume", "post"),
            ("/api/v1/shifts/{shift_id}/finish", "post"),
            ("/api/v1/organizations/{org_id}/shifts", "get"),
        }
    }
    for (path, method), component in snapshot_shift_responses.items():
        status_code = (
            "201"
            if (method, path)
            in {
                ("post", "/api/v1/shifts/start"),
            }
            else "200"
        )
        snapshot_ref = snapshot_schema["paths"][path][method]["responses"][status_code]["content"][
            "application/json"
        ]["schema"]
        runtime_ref = runtime_schema["paths"][path][method]["responses"][status_code]["content"][
            "application/json"
        ]["schema"]
        assert snapshot_ref == runtime_ref == {"$ref": f"#/components/schemas/{component}"}

    assert snapshot_schema["paths"]["/api/v1/shifts/{shift_id}/checklists"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/ApiResponse_ChecklistInstanceListResponse_"
    )
    assert (
        runtime_schema["paths"]["/api/v1/shifts/{shift_id}/checklists"]["get"]["responses"]["200"][
            "content"
        ]["application/json"]["schema"]
        == snapshot_schema["paths"]["/api/v1/shifts/{shift_id}/checklists"]["get"]["responses"][
            "200"
        ]["content"]["application/json"]["schema"]
    )
    assert snapshot_schema["paths"]["/api/v1/shifts/{shift_id}/checklists/{instance_id}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/ApiResponse_ChecklistInstanceDetailResponse_"
    )
    assert (
        runtime_schema["paths"]["/api/v1/shifts/{shift_id}/checklists/{instance_id}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
        == snapshot_schema["paths"]["/api/v1/shifts/{shift_id}/checklists/{instance_id}"]["get"][
            "responses"
        ]["200"]["content"]["application/json"]["schema"]
    )


class TestItemUpdates:
    async def test_toggle_and_comment(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("P1", True)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        inst_id = list_resp.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        item_id = detail.json()["data"]["items"][0]["id"]

        resp = await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
            headers=ctx["member_headers"],
            json={"is_completed": True, "comment": "ok"},
        )
        data = resp.json()["data"]
        assert data["is_completed"] is True
        assert data["comment"] == "ok"
        assert data["change_count"] == 1
        assert data["completed_at"] is not None

    async def test_change_count_increments(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("P1", False)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        inst_id = list_resp.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        item_id = detail.json()["data"]["items"][0]["id"]
        for _ in range(3):
            await client.patch(
                f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
                headers=ctx["member_headers"],
                json={"is_completed": True, "comment": None},
            )
        detail2 = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        assert detail2.json()["data"]["items"][0]["change_count"] == 3

    async def test_status_transitions_to_completed(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("P1", True), ("P2", True), ("P3", False)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        inst_id = list_resp.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        items = detail.json()["data"]["items"]
        required_ids = [it["id"] for it in items if it["is_required"]]

        for item_id in required_ids[:-1]:
            await client.patch(
                f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
                headers=ctx["member_headers"],
                json={"is_completed": True, "comment": None},
            )
        status_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        assert status_resp.json()["data"]["status"] == "pending"

        await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{required_ids[-1]}",
            headers=ctx["member_headers"],
            json={"is_completed": True, "comment": None},
        )
        final_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        assert final_resp.json()["data"]["status"] == "completed"
        assert final_resp.json()["data"]["completed_at"] is not None

    async def test_cannot_edit_finished_shift_with_grace_disabled(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """checklist_grace_period: `checklist_grace_minutes=0` — прежнее поведение,
        окно не открывается вовсе."""
        ctx = await _setup(client, db_session, super_admin_headers)
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/settings",
            headers=super_admin_headers,
            json={"checklist_grace_minutes": 0},
        )
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("P1", False)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        inst_id = list_resp.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        item_id = detail.json()["data"]["items"][0]["id"]

        await client.post(
            f"/api/v1/shifts/{shift_id}/finish",
            headers=ctx["member_headers"],
        )
        resp = await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
            headers=ctx["member_headers"],
            json={"is_completed": True, "comment": None},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "SHIFT_FINISHED"

    async def test_can_edit_finished_shift_within_grace_window(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """checklist_grace_period: организация с дефолтным окном (30 минут) — PATCH
        пункта завершённой смены проходит, пока окно открыто."""
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("P1", False)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        inst_id = list_resp.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        item_id = detail.json()["data"]["items"][0]["id"]

        await client.post(
            f"/api/v1/shifts/{shift_id}/finish",
            headers=ctx["member_headers"],
        )
        resp = await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
            headers=ctx["member_headers"],
            json={"is_completed": True, "comment": "дозаполнено в окне"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["is_completed"] is True

    async def test_cannot_edit_after_grace_window_elapsed(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """checklist_grace_period: окно посчитано от фактического `finished_at`;
        по истечении — прежний отказ SHIFT_FINISHED."""
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("P1", False)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        inst_id = list_resp.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        item_id = detail.json()["data"]["items"][0]["id"]

        await client.post(
            f"/api/v1/shifts/{shift_id}/finish",
            headers=ctx["member_headers"],
        )
        shift_row = (
            await db_session.execute(select(Shift).where(Shift.id == uuid.UUID(shift_id)))
        ).scalar_one()
        # Дефолтное окно — 30 минут; отодвигаем finished_at так, чтобы оно уже истекло.
        shift_row.finished_at = datetime.now(UTC) - timedelta(minutes=31)
        await db_session.commit()

        resp = await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
            headers=ctx["member_headers"],
            json={"is_completed": True, "comment": None},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "SHIFT_FINISHED"

        # Третье состояние контракта: окно закрылось -> fill_allowed=false,
        # fill_deadline_at=null (не отдаём прошедший дедлайн).
        list_after = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        assert list_after.json()["data"]["fill_allowed"] is False
        assert list_after.json()["data"]["fill_deadline_at"] is None

    async def test_non_owner_cannot_edit(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("P1", False)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        inst_id = list_resp.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        item_id = detail.json()["data"]["items"][0]["id"]

        resp = await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
            headers=super_admin_headers,
            json={"is_completed": True, "comment": None},
        )
        assert resp.status_code == 403


class TestFinalize:
    async def test_finish_marks_incomplete_required_when_grace_disabled(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """checklist_grace_minutes=0 — прежнее поведение: терминальный incomplete
        сразу на финише."""
        ctx = await _setup(client, db_session, super_admin_headers)
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/settings",
            headers=super_admin_headers,
            json={"checklist_grace_minutes": 0},
        )
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            name="Required",
            is_required=True,
            items=[("P1", True)],
        )
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            name="Optional",
            is_required=False,
            items=[("P1", True)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        finish_resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish",
            headers=ctx["member_headers"],
        )
        assert finish_resp.status_code == 200
        assert finish_resp.json()["data"]["has_incomplete_required_checklists"] is True

        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        by_name = {i["name"]: i for i in list_resp.json()["data"]["items"]}
        assert by_name["Required"]["status"] == "incomplete"
        assert by_name["Optional"]["status"] == "pending"
        assert list_resp.json()["data"]["fill_allowed"] is False
        assert list_resp.json()["data"]["fill_deadline_at"] is None

    async def test_finish_with_grace_window_keeps_pending_until_filled(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """checklist_grace_period, окно по умолчанию (30 минут): сразу после финиша
        обязательный экземпляр остаётся `pending` (судьба ещё не решена),
        `has_incomplete_required_checklists` — живой снимок (True, пункт не закрыт),
        `fill_allowed=true`/`fill_deadline_at` заполнен. После дозаполнения
        последнего обязательного пункта флаг становится False, статус — completed."""
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            name="Required",
            is_required=True,
            items=[("P1", True)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        finish_resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish",
            headers=ctx["member_headers"],
        )
        assert finish_resp.status_code == 200
        assert finish_resp.json()["data"]["has_incomplete_required_checklists"] is True

        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        list_data = list_resp.json()["data"]
        by_name = {i["name"]: i for i in list_data["items"]}
        assert by_name["Required"]["status"] == "pending"
        assert list_data["fill_allowed"] is True
        assert list_data["fill_deadline_at"] is not None

        inst_id = list_data["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        assert detail.json()["data"]["fill_allowed"] is True
        assert detail.json()["data"]["fill_deadline_at"] is not None
        item_id = detail.json()["data"]["items"][0]["id"]

        patch_resp = await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
            headers=ctx["member_headers"],
            json={"is_completed": True, "comment": None},
        )
        assert patch_resp.status_code == 200

        list_after = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        by_name_after = {i["name"]: i for i in list_after.json()["data"]["items"]}
        assert by_name_after["Required"]["status"] == "completed"

        shift_resp = await client.get(
            f"/api/v1/shifts/{shift_id}",
            headers=ctx["member_headers"],
        )
        assert shift_resp.json()["data"]["has_incomplete_required_checklists"] is False

    async def test_finish_no_required_no_flag(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            is_required=False,
            items=[("P1", False)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        finish_resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish",
            headers=ctx["member_headers"],
        )
        assert finish_resp.json()["data"]["has_incomplete_required_checklists"] is False

    async def test_finish_after_completing(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            is_required=True,
            items=[("P1", True)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        inst_id = list_resp.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        item_id = detail.json()["data"]["items"][0]["id"]
        await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
            headers=ctx["member_headers"],
            json={"is_completed": True, "comment": None},
        )
        finish_resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish",
            headers=ctx["member_headers"],
        )
        assert finish_resp.json()["data"]["has_incomplete_required_checklists"] is False


class TestAutoFinishIntegration:
    async def test_inline_auto_finish_finalizes_checklists(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            is_required=True,
            items=[("P1", True)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        shift_uuid = uuid.UUID(shift_id)

        shift_row = (
            await db_session.execute(select(Shift).where(Shift.id == shift_uuid))
        ).scalar_one()
        # work_schedules (R4): авто-финиш теперь идёт по scheduled_end_at, а не по
        # возрасту started_at — задаём просроченное плановое окно напрямую.
        shift_row.started_at = datetime.now(UTC) - timedelta(hours=48)
        shift_row.scheduled_start_at = datetime.now(UTC) - timedelta(hours=49)
        shift_row.scheduled_end_at = datetime.now(UTC) - timedelta(hours=1)
        await db_session.commit()

        # Starting another shift triggers inline auto-finish
        await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={},
        )

        await db_session.refresh(shift_row)
        assert shift_row.status == ShiftStatus.finished
        assert shift_row.has_incomplete_required_checklists is True

    async def test_inline_auto_finish_opens_grace_window(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """checklist_grace_period: авто-завершённая смена тоже получает окно
        (дефолт 30 минут) — экземпляр остаётся pending, а не терминально
        incomplete, и дозаполнение через API проходит. Плановое окончание —
        всего пару минут в прошлом, чтобы окно (30 минут от фактического
        finished_at) было ещё открыто на момент проверки."""
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            is_required=True,
            items=[("P1", True)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        shift_uuid = uuid.UUID(shift_id)

        shift_row = (
            await db_session.execute(select(Shift).where(Shift.id == shift_uuid))
        ).scalar_one()
        shift_row.started_at = datetime.now(UTC) - timedelta(hours=2)
        shift_row.scheduled_start_at = datetime.now(UTC) - timedelta(hours=2)
        shift_row.scheduled_end_at = datetime.now(UTC) - timedelta(minutes=2)
        await db_session.commit()

        # Starting another shift triggers inline auto-finish
        await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={},
        )

        await db_session.refresh(shift_row)
        assert shift_row.status == ShiftStatus.finished
        assert shift_row.has_incomplete_required_checklists is True

        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        list_data = list_resp.json()["data"]
        assert list_data["items"][0]["status"] == "pending"
        assert list_data["fill_allowed"] is True
        assert list_data["fill_deadline_at"] is not None

        inst_id = list_data["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        item_id = detail.json()["data"]["items"][0]["id"]
        patch_resp = await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
            headers=ctx["member_headers"],
            json={"is_completed": True, "comment": None},
        )
        assert patch_resp.status_code == 200


class TestFillWindowContract:
    async def test_fill_window_fields_for_active_shift(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Для активной смены fill_allowed=true, fill_deadline_at=null — окно не
        применяется, пока смена не завершена."""
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template_with_items(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            items=[("P1", True)],
        )
        shift_id = await _start_org_shift(
            client,
            ctx["member_headers"],
            ctx["org_id"],
        )
        list_resp = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists",
            headers=ctx["member_headers"],
        )
        data = list_resp.json()["data"]
        assert data["fill_allowed"] is True
        assert data["fill_deadline_at"] is None

        inst_id = data["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        assert detail.json()["data"]["fill_allowed"] is True
        assert detail.json()["data"]["fill_deadline_at"] is None


class TestFillWindowRaceGuard:
    async def test_reassert_catches_window_closed_between_checks(
        self, db_session: AsyncSession, super_admin_headers, client: AsyncClient
    ):
        """checklist_grace_period, граница окна: решение принимается по времени
        начала обработки запроса, но `_reassert_fill_window_open` перечитывает
        `status`/`finished_at` заново перед мутацией — этим он ловит смену,
        которую окно закрыло уже ПОСЛЕ первичной проверки (например, конкурентный
        auto-finish/другая вкладка). Без него мутация бы проскочила на устаревших
        Python-атрибутах уже загруженного объекта."""
        user = User(
            id=uuid.uuid4(),
            email="race-guard@example.com",
            password_hash=hash_password("Test1234"),
            name="Race Guard",
            is_verified=True,
        )
        db_session.add(user)
        await db_session.flush()

        org = Organization(name="Race Org", owner_id=user.id)
        db_session.add(org)
        await db_session.flush()
        # Намеренно без строки OrganizationSettings — берём server_default (30 минут).

        shift = Shift(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org.id,
            status=ShiftStatus.finished,
            finished_at=datetime.now(UTC),
        )
        db_session.add(shift)
        await db_session.commit()

        # Первичная проверка проходит: смена только что завершена, окно (30 минут) открыто.
        await instance_service._assert_fill_window_open(db_session, shift)

        # Симулируем конкурентное завершение окна: другая транзакция отодвинула
        # finished_at в прошлое настолько, что окно уже истекло.
        # synchronize_session=False — не обновляем Python-атрибуты уже загруженного
        # `shift` в памяти, как это и происходило бы при труъ-конкурентном коммите
        # из другого процесса/сессии.
        await db_session.execute(
            sa_update(Shift)
            .where(Shift.id == shift.id)
            .values(finished_at=datetime.now(UTC) - timedelta(minutes=31))
            .execution_options(synchronize_session=False)
        )
        await db_session.commit()

        # Устаревший объект в памяти всё ещё "думает", что окно открыто.
        assert instance_service.compute_fill_window(shift, 30).fill_allowed is True

        # Реассерт перечитывает status/finished_at из БД и ловит закрытое окно.
        with pytest.raises(ChecklistError) as exc_info:
            await instance_service._reassert_fill_window_open(db_session, shift)
        assert exc_info.value.code == "SHIFT_FINISHED"


class TestOrganizationResponseExposesChecklistGraceMinutes:
    """Дополнение к ТЗ (раздел «Длительность окна — рядовому сотруднику»):
    `checklist_grace_minutes` денормализуется в `OrganizationResponse` по тому
    же прецеденту, что и `overtime_request_days` (см. `test_overtime.py`,
    `TestOrganizationResponseExposesOvertimeRequestDays`) — employee не имеет
    доступа к `/settings`, но должен знать длительность окна, чтобы диалог
    завершения смены показал, сколько времени останется на дозаполнение."""

    async def test_employee_sees_default_value_in_org_detail(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}", headers=ctx["member_headers"]
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["checklist_grace_minutes"] == 30

    async def test_employee_sees_value_in_org_list(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        resp = await client.get("/api/v1/organizations", headers=ctx["member_headers"])
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        target = next(i for i in items if i["id"] == ctx["org_id"])
        assert target["checklist_grace_minutes"] == 30

    async def test_value_reflects_settings_update_and_is_read_only(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        patch_resp = await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/settings",
            headers=super_admin_headers,
            json={"checklist_grace_minutes": 0},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["data"]["checklist_grace_minutes"] == 0

        detail_resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}", headers=ctx["member_headers"]
        )
        assert detail_resp.json()["data"]["checklist_grace_minutes"] == 0

        # Поле read-only в OrganizationResponse — попытка передать его через
        # PATCH /organizations/{id} (не /settings) молча игнорируется схемой.
        rename_resp = await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}",
            headers=super_admin_headers,
            json={"name": "Renamed Org", "checklist_grace_minutes": 120},
        )
        assert rename_resp.status_code == 200
        assert rename_resp.json()["data"]["checklist_grace_minutes"] == 0

    async def test_default_thirty_when_settings_record_missing(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        super_admin_headers,
        super_admin_user,
    ):
        organization = Organization(name="No Settings Org", owner_id=super_admin_user.id)
        db_session.add(organization)
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/organizations/{organization.id}", headers=super_admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["checklist_grace_minutes"] == 30
