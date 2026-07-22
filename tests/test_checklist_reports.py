"""Тесты фичи checklist_reports.

Покрывает: фильтр `checklists` в `GET /organizations/{org_id}/shifts`,
`checklists_summary` в `ShiftResponse` (org vs. персональный контекст) и
реестр `GET /organizations/{org_id}/checklist-instances`.

Назначение шаблонов сотрудникам делается напрямую через `ChecklistMemberOverride`
(канал `personal_add` в `_compute_effective`) — это самый короткий путь получить
детерминированный набор экземпляров чек-листов в смене без обвязки
custom-ролей/API-назначений, которые тестируются отдельно в `test_checklist_assignments.py`.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.checklist import (
    ChecklistMemberOverride,
    ChecklistTemplate,
    ChecklistTemplateItem,
    ChecklistType,
    OverrideType,
)
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.user import User
from src.app.models.work_location import WorkLocation


async def _create_user(db_session: AsyncSession, email: str, name: str = "User") -> User:
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


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "owner@example.com", "Owner")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    return await _login(client, "owner@example.com")


@pytest.fixture
async def org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="Cafe", owner_id=owner.id)
    db_session.add(organization)
    await db_session.flush()
    db_session.add(
        OrganizationSettings(
            organization_id=organization.id,
            geo_check_enabled=False,
            require_work_location=False,
        )
    )
    await db_session.commit()
    return organization


async def _add_member(
    db_session: AsyncSession,
    org: Organization,
    email: str,
    name: str,
) -> tuple[User, OrganizationMember]:
    user = await _create_user(db_session, email, name)
    member = OrganizationMember(organization_id=org.id, user_id=user.id, role=MemberRole.employee)
    db_session.add(member)
    await db_session.commit()
    return user, member


async def _add_template(
    db_session: AsyncSession,
    org: Organization,
    member: OrganizationMember,
    *,
    name: str,
    type_: ChecklistType = ChecklistType.shift_start,
    template_required: bool = True,
    item_required: bool = True,
    item_text: str = "Пункт 1",
) -> ChecklistTemplate:
    """Назначает member шаблон через личный override `add` (см. docstring модуля)."""
    template = ChecklistTemplate(
        organization_id=org.id,
        name=name,
        type=type_,
        is_required=template_required,
    )
    db_session.add(template)
    await db_session.flush()
    db_session.add(
        ChecklistTemplateItem(
            template_id=template.id,
            text=item_text,
            is_required=item_required,
            position=0,
        )
    )
    db_session.add(
        ChecklistMemberOverride(
            template_id=template.id,
            member_id=member.id,
            override_type=OverrideType.add,
        )
    )
    await db_session.commit()
    return template


async def _start_shift(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: uuid.UUID,
    *,
    work_location_id: uuid.UUID | None = None,
) -> str:
    body: dict[str, object] = {"organization_id": str(org_id)}
    if work_location_id is not None:
        body["work_location_id"] = str(work_location_id)
    resp = await client.post("/api/v1/shifts/start", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _first_instance_id(client: AsyncClient, headers: dict[str, str], shift_id: str) -> str:
    resp = await client.get(f"/api/v1/shifts/{shift_id}/checklists", headers=headers)
    return resp.json()["data"]["items"][0]["id"]


async def _complete_all_items(
    client: AsyncClient,
    headers: dict[str, str],
    shift_id: str,
    instance_id: str,
) -> None:
    detail = await client.get(
        f"/api/v1/shifts/{shift_id}/checklists/{instance_id}",
        headers=headers,
    )
    for item in detail.json()["data"]["items"]:
        resp = await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{instance_id}/items/{item['id']}",
            headers=headers,
            json={"is_completed": True, "comment": None},
        )
        assert resp.status_code == 200, resp.text


class TestChecklistsShiftFilter:
    """Фильтр `checklists` в `GET /organizations/{org_id}/shifts`."""

    async def _build_four_shifts(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
    ) -> dict[str, tuple[User, str]]:
        """Строит 4 смены — по одной на каждое значение фильтра `checklists`."""
        none_user, _none_member = await _add_member(db_session, org, "none@example.com", "None")
        completed_user, completed_member = await _add_member(
            db_session, org, "completed@example.com", "Completed"
        )
        optional_user, optional_member = await _add_member(
            db_session, org, "optional@example.com", "Optional"
        )
        required_user, required_member = await _add_member(
            db_session, org, "required@example.com", "Required"
        )

        await _add_template(
            db_session, org, completed_member, name="T-completed", template_required=True
        )
        await _add_template(
            db_session, org, optional_member, name="T-optional", template_required=False
        )
        await _add_template(
            db_session, org, required_member, name="T-required", template_required=True
        )

        none_headers = await _login(client, "none@example.com")
        completed_headers = await _login(client, "completed@example.com")
        optional_headers = await _login(client, "optional@example.com")
        required_headers = await _login(client, "required@example.com")

        none_shift = await _start_shift(client, none_headers, org.id)
        completed_shift = await _start_shift(client, completed_headers, org.id)
        optional_shift = await _start_shift(client, optional_headers, org.id)
        required_shift = await _start_shift(client, required_headers, org.id)

        inst_id = await _first_instance_id(client, completed_headers, completed_shift)
        await _complete_all_items(client, completed_headers, completed_shift, inst_id)

        return {
            "none": (none_user, none_shift),
            "completed": (completed_user, completed_shift),
            "optional_incomplete": (optional_user, optional_shift),
            "required_incomplete": (required_user, required_shift),
        }

    async def test_filter_none(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        shifts = await self._build_four_shifts(client, db_session, org)
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={"checklists": "none"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == shifts["none"][1]

    async def test_filter_all_completed(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        shifts = await self._build_four_shifts(client, db_session, org)
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={"checklists": "all_completed"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == shifts["completed"][1]

    async def test_filter_has_incomplete(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        shifts = await self._build_four_shifts(client, db_session, org)
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={"checklists": "has_incomplete"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 2
        ids = {item["id"] for item in data["items"]}
        assert ids == {shifts["optional_incomplete"][1], shifts["required_incomplete"][1]}

    async def test_filter_required_incomplete(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        """Регресс: активная (не завершённая) смена с незаполненным обязательным
        чек-листом ловится фильтром, хотя `has_incomplete_required_checklists`
        проставляется только при завершении смены."""
        shifts = await self._build_four_shifts(client, db_session, org)
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={"checklists": "required_incomplete"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        item = data["items"][0]
        assert item["id"] == shifts["required_incomplete"][1]
        assert item["status"] == "active"
        assert item["has_incomplete_required_checklists"] is False

    async def test_filter_combines_with_user_id(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        shifts = await self._build_four_shifts(client, db_session, org)
        required_user, _ = shifts["required_incomplete"]
        optional_user, _ = shifts["optional_incomplete"]

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={"checklists": "has_incomplete", "user_id": str(optional_user.id)},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == shifts["optional_incomplete"][1]

        resp2 = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={"checklists": "required_incomplete", "user_id": str(required_user.id)},
        )
        assert resp2.json()["data"]["total"] == 1

    async def test_filter_combines_with_date_range(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        await self._build_four_shifts(client, db_session, org)
        now = datetime.now(UTC)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={
                "checklists": "has_incomplete",
                "date_from": (now - timedelta(hours=1)).isoformat(),
                "date_to": (now + timedelta(hours=1)).isoformat(),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 2

        resp_out_of_range = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={
                "checklists": "has_incomplete",
                "date_from": (now + timedelta(days=1)).isoformat(),
            },
        )
        assert resp_out_of_range.json()["data"]["total"] == 0

    async def test_invalid_filter_400(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
            params={"checklists": "bogus"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_CHECKLIST_FILTER"


class TestChecklistsSummaryField:
    """`ShiftResponse.checklists_summary` — заполнен только в орг-эндпоинтах."""

    async def test_summary_in_org_list(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member = await _add_member(db_session, org, "worker@example.com", "Worker")
        await _add_template(db_session, org, member, name="A", template_required=True)
        await _add_template(db_session, org, member, name="B", template_required=False)

        headers = await _login(client, "worker@example.com")
        shift_id = await _start_shift(client, headers, org.id)
        inst_id = await _first_instance_id(client, headers, shift_id)
        await _complete_all_items(client, headers, shift_id, inst_id)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts",
            headers=owner_headers,
        )
        item = resp.json()["data"]["items"][0]
        summary = item["checklists_summary"]
        assert summary["total"] == 2
        assert summary["completed"] == 1
        assert summary["required_total"] == 1
        assert summary["required_incomplete"] == 0

    async def test_summary_in_org_detail(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member = await _add_member(db_session, org, "worker@example.com", "Worker")
        await _add_template(db_session, org, member, name="A", template_required=True)

        headers = await _login(client, "worker@example.com")
        shift_id = await _start_shift(client, headers, org.id)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts/{shift_id}",
            headers=owner_headers,
        )
        summary = resp.json()["data"]["checklists_summary"]
        assert summary == {
            "total": 1,
            "completed": 0,
            "required_total": 1,
            "required_incomplete": 1,
        }

    async def test_summary_null_in_personal_endpoint(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
    ) -> None:
        _, member = await _add_member(db_session, org, "worker@example.com", "Worker")
        await _add_template(db_session, org, member, name="A", template_required=True)

        headers = await _login(client, "worker@example.com")
        await _start_shift(client, headers, org.id)

        resp = await client.get("/api/v1/shifts", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["items"][0]["checklists_summary"] is None


class TestOrgChecklistInstancesRegistry:
    """`GET /organizations/{org_id}/checklist-instances`."""

    async def test_pagination(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member_a = await _add_member(db_session, org, "a@example.com", "A")
        _, member_b = await _add_member(db_session, org, "b@example.com", "B")
        await _add_template(db_session, org, member_a, name="TA", template_required=True)
        await _add_template(db_session, org, member_b, name="TB", template_required=True)

        await _start_shift(client, await _login(client, "a@example.com"), org.id)
        await _start_shift(client, await _login(client, "b@example.com"), org.id)

        seen: set[str] = set()
        for offset in (0, 1):
            resp = await client.get(
                f"/api/v1/organizations/{org.id}/checklist-instances",
                headers=owner_headers,
                params={"limit": 1, "offset": offset},
            )
            assert resp.status_code == 200
            data = resp.json()["data"]
            assert data["total"] == 2
            assert data["limit"] == 1
            assert data["offset"] == offset
            assert len(data["items"]) == 1
            seen.add(data["items"][0]["id"])
        assert len(seen) == 2

    async def test_filter_user_id(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        user_a, member_a = await _add_member(db_session, org, "a@example.com", "A")
        _, member_b = await _add_member(db_session, org, "b@example.com", "B")
        await _add_template(db_session, org, member_a, name="TA", template_required=True)
        await _add_template(db_session, org, member_b, name="TB", template_required=True)

        await _start_shift(client, await _login(client, "a@example.com"), org.id)
        await _start_shift(client, await _login(client, "b@example.com"), org.id)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"user_id": str(user_a.id)},
        )
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["user_id"] == str(user_a.id)
        assert data["items"][0]["name"] == "TA"

    async def test_filter_template_id(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member = await _add_member(db_session, org, "worker@example.com", "Worker")
        tpl_a = await _add_template(db_session, org, member, name="TA", template_required=True)
        await _add_template(db_session, org, member, name="TB", template_required=True)

        headers = await _login(client, "worker@example.com")
        await _start_shift(client, headers, org.id)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"template_id": str(tpl_a.id)},
        )
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["template_id"] == str(tpl_a.id)
        assert data["items"][0]["name"] == "TA"

    async def test_filter_type(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member = await _add_member(db_session, org, "worker@example.com", "Worker")
        await _add_template(
            db_session,
            org,
            member,
            name="Open",
            type_=ChecklistType.shift_start,
            template_required=True,
        )
        await _add_template(
            db_session,
            org,
            member,
            name="Close",
            type_=ChecklistType.shift_end,
            template_required=True,
        )

        headers = await _login(client, "worker@example.com")
        await _start_shift(client, headers, org.id)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"type": "shift_end"},
        )
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Close"
        assert data["items"][0]["type"] == "shift_end"

    async def test_invalid_type_400(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"type": "bogus"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_TYPE"

    async def test_filter_status_and_state(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, completed_member = await _add_member(
            db_session, org, "completed@example.com", "Completed"
        )
        _, pending_member = await _add_member(db_session, org, "pending@example.com", "Pending")
        await _add_template(
            db_session, org, completed_member, name="T-completed", template_required=True
        )
        await _add_template(
            db_session, org, pending_member, name="T-pending", template_required=True
        )

        completed_headers = await _login(client, "completed@example.com")
        pending_headers = await _login(client, "pending@example.com")
        completed_shift = await _start_shift(client, completed_headers, org.id)
        await _start_shift(client, pending_headers, org.id)

        inst_id = await _first_instance_id(client, completed_headers, completed_shift)
        await _complete_all_items(client, completed_headers, completed_shift, inst_id)

        # status
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"status": "completed"},
        )
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["name"] == "T-completed"

        # state=completed эквивалентен status=completed
        resp_state = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"state": "completed"},
        )
        assert resp_state.json()["data"]["total"] == 1

        # state=not_completed = pending + incomplete
        resp_not_completed = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"state": "not_completed"},
        )
        data_nc = resp_not_completed.json()["data"]
        assert data_nc["total"] == 1
        assert data_nc["items"][0]["name"] == "T-pending"

        # status приоритетнее state — оба переданы, побеждает status
        resp_priority = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"status": "completed", "state": "not_completed"},
        )
        data_priority = resp_priority.json()["data"]
        assert data_priority["total"] == 1
        assert data_priority["items"][0]["name"] == "T-completed"

    async def test_invalid_status_400(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"status": "bogus"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_STATUS"

    async def test_invalid_state_400(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"state": "bogus"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_STATE"

    async def test_filter_is_required(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member = await _add_member(db_session, org, "worker@example.com", "Worker")
        await _add_template(db_session, org, member, name="Req", template_required=True)
        await _add_template(db_session, org, member, name="Opt", template_required=False)

        headers = await _login(client, "worker@example.com")
        await _start_shift(client, headers, org.id)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"is_required": "true"},
        )
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["name"] == "Req"

        resp_false = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"is_required": "false"},
        )
        assert resp_false.json()["data"]["items"][0]["name"] == "Opt"

    async def test_filter_work_location_id(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        location = WorkLocation(
            organization_id=org.id,
            name="Точка А",
            latitude=55.75,
            longitude=37.61,
            radius_meters=100,
        )
        db_session.add(location)
        await db_session.commit()

        with_loc_user, with_loc_member = await _add_member(
            db_session, org, "withloc@example.com", "WithLoc"
        )
        _, no_loc_member = await _add_member(db_session, org, "noloc@example.com", "NoLoc")
        await _add_template(db_session, org, with_loc_member, name="T1", template_required=True)
        await _add_template(db_session, org, no_loc_member, name="T2", template_required=True)

        await _start_shift(
            client,
            await _login(client, "withloc@example.com"),
            org.id,
            work_location_id=location.id,
        )
        await _start_shift(client, await _login(client, "noloc@example.com"), org.id)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"work_location_id": str(location.id)},
        )
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["user_id"] == str(with_loc_user.id)
        assert data["items"][0]["work_location"]["id"] == str(location.id)
        assert data["items"][0]["work_location"]["name"] == "Точка А"

    async def test_filter_date_range(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member = await _add_member(db_session, org, "worker@example.com", "Worker")
        await _add_template(db_session, org, member, name="T", template_required=True)

        headers = await _login(client, "worker@example.com")
        await _start_shift(client, headers, org.id)
        now = datetime.now(UTC)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={
                "date_from": (now - timedelta(hours=1)).isoformat(),
                "date_to": (now + timedelta(hours=1)).isoformat(),
            },
        )
        assert resp.json()["data"]["total"] == 1

        resp_out = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"date_from": (now + timedelta(days=1)).isoformat()},
        )
        assert resp_out.json()["data"]["total"] == 0

    async def test_invalid_date_range_400(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        now = datetime.now(UTC)
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={
                "date_from": now.isoformat(),
                "date_to": (now - timedelta(days=1)).isoformat(),
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_DATE_RANGE"

    async def test_sort_and_order(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member_a = await _add_member(db_session, org, "a@example.com", "A")
        _, member_b = await _add_member(db_session, org, "b@example.com", "B")
        await _add_template(db_session, org, member_a, name="TA", template_required=True)
        await _add_template(db_session, org, member_b, name="TB", template_required=True)

        shift_a = await _start_shift(client, await _login(client, "a@example.com"), org.id)
        shift_b = await _start_shift(client, await _login(client, "b@example.com"), org.id)

        resp_desc = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"sort": "shift_started_at", "order": "desc"},
        )
        items_desc = resp_desc.json()["data"]["items"]
        assert [i["shift_id"] for i in items_desc] == [shift_b, shift_a]

        resp_asc = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"sort": "shift_started_at", "order": "asc"},
        )
        items_asc = resp_asc.json()["data"]["items"]
        assert [i["shift_id"] for i in items_asc] == [shift_a, shift_b]

    async def test_invalid_sort_400(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
            params={"sort": "bogus"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_SORT"

    async def test_403_for_employee(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
    ) -> None:
        await _add_member(db_session, org, "worker@example.com", "Worker")
        headers = await _login(client, "worker@example.com")

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=headers,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_org_isolation(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner: User,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        other_org = Organization(name="Other", owner_id=owner.id)
        db_session.add(other_org)
        await db_session.flush()
        db_session.add(
            OrganizationSettings(
                organization_id=other_org.id,
                geo_check_enabled=False,
            )
        )
        await db_session.commit()

        _, member_org = await _add_member(db_session, org, "worker@example.com", "Worker")
        _, member_other = await _add_member(
            db_session, other_org, "other-worker@example.com", "OtherWorker"
        )
        await _add_template(db_session, org, member_org, name="OrgTpl", template_required=True)
        await _add_template(
            db_session, other_org, member_other, name="OtherTpl", template_required=True
        )

        await _start_shift(client, await _login(client, "worker@example.com"), org.id)
        await _start_shift(client, await _login(client, "other-worker@example.com"), other_org.id)

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
        )
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["name"] == "OrgTpl"

    async def test_personal_shifts_excluded(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member = await _add_member(db_session, org, "worker@example.com", "Worker")
        await _add_template(db_session, org, member, name="T", template_required=True)

        headers = await _login(client, "worker@example.com")
        await _start_shift(client, headers, org.id)
        # Персональная смена того же сотрудника — чек-листов не создаёт, но
        # проверяем, что реестр в принципе фильтрует по organization_id.
        personal_resp = await client.post("/api/v1/shifts/start", headers=headers, json={})
        assert personal_resp.status_code == 201

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
        )
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["shift_status"] == "active"

    async def test_deleted_template_keeps_null_template_id(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        _, member = await _add_member(db_session, org, "worker@example.com", "Worker")
        template = await _add_template(
            db_session, org, member, name="Temporary", template_required=True
        )

        headers = await _login(client, "worker@example.com")
        await _start_shift(client, headers, org.id)

        await db_session.execute(
            delete(ChecklistTemplate).where(ChecklistTemplate.id == template.id)
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        item = data["items"][0]
        assert item["template_id"] is None
        assert item["name"] == "Temporary"

    async def test_empty_result_200(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
    ) -> None:
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0
