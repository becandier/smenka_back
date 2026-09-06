"""Приёмочные проверки назначения чек-листов на графики."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_checklist_locations import (
    _assign_role_to_member,
    _assign_template_to_locations,
    _assign_template_to_roles,
    _make_location,
    _make_role,
    _make_template,
    _setup_org_with_member,
    _shift_checklist_names,
)


async def _make_schedule(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    name: str = "День",
) -> str:
    response = await client.post(
        f"/api/v1/organizations/{org_id}/work-schedules",
        headers=headers,
        json={"name": name, "start_time": "00:00", "end_time": "23:59"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


async def _assign_template_to_schedules(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    template_id: str,
    schedule_ids: list[str],
) -> None:
    response = await client.put(
        f"/api/v1/organizations/{org_id}/checklist-templates/{template_id}/schedules",
        headers=headers,
        json={"schedule_ids": schedule_ids},
    )
    assert response.status_code == 200, response.text


async def _start_shift_with_schedule(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    schedule_id: str | None = None,
    location_id: str | None = None,
) -> str:
    body: dict[str, str] = {"organization_id": org_id}
    if schedule_id is not None:
        body["work_schedule_id"] = schedule_id
    if location_id is not None:
        body["work_location_id"] = location_id
    response = await client.post("/api/v1/shifts/start", headers=headers, json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


class TestChecklistScheduleResolution:
    async def test_schedule_only_template_is_created_for_matching_schedule(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        schedule_id = await _make_schedule(client, super_admin_headers, ctx["org_id"])
        await _make_schedule(client, super_admin_headers, ctx["org_id"], "Вечер")
        template_id = await _make_template(client, super_admin_headers, ctx["org_id"], "Schedule")
        await _assign_template_to_schedules(
            client, super_admin_headers, ctx["org_id"], template_id, [schedule_id]
        )

        shift_id = await _start_shift_with_schedule(
            client, ctx["member_headers"], ctx["org_id"], schedule_id
        )
        assert await _shift_checklist_names(client, ctx["member_headers"], shift_id) == {
            "Schedule"
        }

    async def test_role_and_schedule_are_both_required(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        role_id = await _make_role(client, super_admin_headers, ctx["org_id"])
        schedule_id = await _make_schedule(client, super_admin_headers, ctx["org_id"])
        other_schedule_id = await _make_schedule(
            client, super_admin_headers, ctx["org_id"], "Вечер"
        )
        template_id = await _make_template(
            client, super_admin_headers, ctx["org_id"], "RoleSchedule"
        )
        await _assign_template_to_roles(
            client, super_admin_headers, ctx["org_id"], template_id, [role_id]
        )
        await _assign_template_to_schedules(
            client, super_admin_headers, ctx["org_id"], template_id, [schedule_id]
        )
        await _assign_role_to_member(
            client, super_admin_headers, ctx["org_id"], str(ctx["member_user"].id), role_id
        )

        matched = await _start_shift_with_schedule(
            client, ctx["member_headers"], ctx["org_id"], schedule_id
        )
        assert await _shift_checklist_names(client, ctx["member_headers"], matched) == {
            "RoleSchedule"
        }

        # The same role on another schedule does not receive the snapshot.
        await client.post(f"/api/v1/shifts/{matched}/finish", headers=ctx["member_headers"])
        other = await _start_shift_with_schedule(
            client, ctx["member_headers"], ctx["org_id"], other_schedule_id
        )
        assert await _shift_checklist_names(client, ctx["member_headers"], other) == set()

    async def test_location_and_schedule_are_both_required(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        location_id = await _make_location(client, super_admin_headers, ctx["org_id"])
        other_location_id = await _make_location(client, super_admin_headers, ctx["org_id"], "B")
        schedule_id = await _make_schedule(client, super_admin_headers, ctx["org_id"])
        template_id = await _make_template(
            client, super_admin_headers, ctx["org_id"], "LocationSchedule"
        )
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], template_id, [location_id]
        )
        await _assign_template_to_schedules(
            client, super_admin_headers, ctx["org_id"], template_id, [schedule_id]
        )

        matched = await _start_shift_with_schedule(
            client, ctx["member_headers"], ctx["org_id"], schedule_id, location_id
        )
        assert await _shift_checklist_names(client, ctx["member_headers"], matched) == {
            "LocationSchedule"
        }
        await client.post(f"/api/v1/shifts/{matched}/finish", headers=ctx["member_headers"])
        other = await _start_shift_with_schedule(
            client, ctx["member_headers"], ctx["org_id"], schedule_id, other_location_id
        )
        assert await _shift_checklist_names(client, ctx["member_headers"], other) == set()

    async def test_schedule_template_is_not_created_without_schedule(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        schedule_id = await _make_schedule(client, super_admin_headers, ctx["org_id"])
        await _make_schedule(client, super_admin_headers, ctx["org_id"], "Вечер")
        template_id = await _make_template(
            client, super_admin_headers, ctx["org_id"], "NeedsSchedule"
        )
        await _assign_template_to_schedules(
            client, super_admin_headers, ctx["org_id"], template_id, [schedule_id]
        )

        shift_id = await _start_shift_with_schedule(client, ctx["member_headers"], ctx["org_id"])
        assert await _shift_checklist_names(client, ctx["member_headers"], shift_id) == set()

    async def test_personal_add_and_remove_respect_schedule_assignment(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        schedule_id = await _make_schedule(client, super_admin_headers, ctx["org_id"])
        template_id = await _make_template(
            client, super_admin_headers, ctx["org_id"], "PersonalSchedule"
        )
        await _assign_template_to_schedules(
            client, super_admin_headers, ctx["org_id"], template_id, [schedule_id]
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={"overrides": [{"template_id": template_id, "type": "add"}]},
        )
        matched = await _start_shift_with_schedule(
            client, ctx["member_headers"], ctx["org_id"], schedule_id
        )
        assert await _shift_checklist_names(client, ctx["member_headers"], matched) == {
            "PersonalSchedule"
        }
        await client.post(f"/api/v1/shifts/{matched}/finish", headers=ctx["member_headers"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklist-overrides",
            headers=super_admin_headers,
            json={"overrides": [{"template_id": template_id, "type": "remove"}]},
        )
        removed = await _start_shift_with_schedule(
            client, ctx["member_headers"], ctx["org_id"], schedule_id
        )
        assert await _shift_checklist_names(client, ctx["member_headers"], removed) == set()

    async def test_archived_schedule_template_is_not_created(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        schedule_id = await _make_schedule(client, super_admin_headers, ctx["org_id"])
        template_id = await _make_template(client, super_admin_headers, ctx["org_id"], "Archived")
        await _assign_template_to_schedules(
            client, super_admin_headers, ctx["org_id"], template_id, [schedule_id]
        )
        pause_response = await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/work-schedules/{schedule_id}",
            headers=super_admin_headers,
            json={"is_paused": True},
        )
        assert pause_response.status_code == 200, pause_response.text
        assert pause_response.json()["data"]["is_paused"] is True

        explicit_response = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={
                "organization_id": ctx["org_id"],
                "work_schedule_id": schedule_id,
            },
        )
        assert explicit_response.status_code == 403
        assert explicit_response.json()["error"]["code"] == "SCHEDULE_NOT_AVAILABLE"

        shift_id = await _start_shift_with_schedule(client, ctx["member_headers"], ctx["org_id"])
        assert await _shift_checklist_names(client, ctx["member_headers"], shift_id) == set()
