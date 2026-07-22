# tests/test_member_display_name.py
"""Фича member_display_name: display_name участника внутри организации.

Покрывает:
- `PATCH /organizations/{org_id}/members/{member_user_id}` — установка/сброс,
  нормализация, права (owner/admin/super_admin — да; employee, в т.ч. себе —
  403), 404 для не-участника, аудит-запись;
- регресс: поле присутствует (и `null`, когда не задано) и `user_name`
  (настоящее имя) не подменяется в списке участников, орг-сменах (список и
  деталь), реестре чек-листов, штрафах (список и деталь), payroll (обе
  проекции) и статистике организации.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
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
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User

SHIFT_START = datetime(2026, 1, 10, 9, 0, tzinfo=UTC)
SHIFT_END = datetime(2026, 1, 10, 17, 0, tzinfo=UTC)


async def _create_user(db_session: AsyncSession, email: str, name: str) -> User:
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


async def _add_template(
    db_session: AsyncSession,
    org: Organization,
    member: OrganizationMember,
    *,
    name: str = "Открытие смены",
) -> ChecklistTemplate:
    """Назначает member шаблон через личный override `add` (см. checklist_reports)."""
    template = ChecklistTemplate(
        organization_id=org.id,
        name=name,
        type=ChecklistType.shift_start,
        is_required=True,
    )
    db_session.add(template)
    await db_session.flush()
    db_session.add(
        ChecklistTemplateItem(
            template_id=template.id,
            text="Пункт 1",
            is_required=True,
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


async def _make_finished_shift(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
) -> Shift:
    shift = Shift(
        user_id=user_id,
        organization_id=org_id,
        started_at=SHIFT_START,
        finished_at=SHIFT_END,
        status=ShiftStatus.finished,
    )
    db_session.add(shift)
    await db_session.commit()
    return shift


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "owner@example.com", "Владелец Кофейни")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    return await _login(client, "owner@example.com")


@pytest.fixture
async def org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="Кофейня", owner_id=owner.id)
    db_session.add(organization)
    await db_session.flush()
    db_session.add(
        OrganizationSettings(
            organization_id=organization.id,
            geo_check_enabled=False,
            require_work_location=False,
            auto_finish_hours=16,
        )
    )
    await db_session.commit()
    return organization


@pytest.fixture
async def employee_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "employee@example.com", "Белоусов Артём")


@pytest.fixture
async def employee_member(
    db_session: AsyncSession, org: Organization, employee_user: User
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id, user_id=employee_user.id, role=MemberRole.employee
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def employee_headers(employee_user: User, client: AsyncClient) -> dict[str, str]:
    return await _login(client, "employee@example.com")


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "orgadmin@example.com", "Админ Организации")


@pytest.fixture
async def admin_member(
    db_session: AsyncSession, org: Organization, admin_user: User
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id, user_id=admin_user.id, role=MemberRole.admin
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def admin_headers(admin_user: User, client: AsyncClient) -> dict[str, str]:
    return await _login(client, "orgadmin@example.com")


@pytest.fixture
async def outsider_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "outsider@example.com", "Посторонний")


@pytest.fixture
async def outsider_headers(outsider_user: User, client: AsyncClient) -> dict[str, str]:
    return await _login(client, "outsider@example.com")


class TestUpdateDisplayName:
    """`PATCH /organizations/{org_id}/members/{member_user_id}`."""

    async def test_owner_sets_display_name(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Артём (ночная смена)"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["display_name"] == "Артём (ночная смена)"
        assert data["user_name"] == "Белоусов Артём"

    async def test_admin_sets_display_name(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
        admin_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=admin_headers,
            json={"display_name": "Тёма"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["display_name"] == "Тёма"

    async def test_reset_via_null(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": None},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["display_name"] is None
        assert data["user_name"] == "Белоусов Артём"

    async def test_reset_via_empty_string(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "   "},
        )
        assert response.status_code == 200
        assert response.json()["data"]["display_name"] is None

    async def test_trims_and_collapses_whitespace(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "  Артём\tночная  "},
        )
        assert response.status_code == 200
        assert response.json()["data"]["display_name"] == "Артём ночная"

    async def test_101_chars_rejected(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "a" * 101},
        )
        assert response.status_code == 400
        body = response.json()
        assert body["data"] is None
        assert body["error"]["code"] == "INVALID_DISPLAY_NAME"

    async def test_100_chars_accepted(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "a" * 100},
        )
        assert response.status_code == 200
        assert response.json()["data"]["display_name"] == "a" * 100

    async def test_employee_cannot_rename_self(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=employee_headers,
            json={"display_name": "Тёма"},
        )
        assert response.status_code == 403

    async def test_employee_cannot_rename_other(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        org: Organization,
        admin_user: User,
        admin_member: OrganizationMember,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{admin_user.id}",
            headers=employee_headers,
            json={"display_name": "Тёма"},
        )
        assert response.status_code == 403

    async def test_member_not_found(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        outsider_user: User,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{outsider_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "MEMBER_NOT_FOUND"

    async def test_outsider_forbidden(
        self,
        client: AsyncClient,
        outsider_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=outsider_headers,
            json={"display_name": "Тёма"},
        )
        assert response.status_code == 403

    async def test_super_admin_can_set(
        self,
        client: AsyncClient,
        super_admin_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=super_admin_headers,
            json={"display_name": "Тёма (супер-админ)"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["display_name"] == "Тёма (супер-админ)"

    async def test_two_members_same_display_name_allowed(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
        admin_user: User,
        admin_member: OrganizationMember,
    ):
        for uid in (employee_user.id, admin_user.id):
            response = await client.patch(
                f"/api/v1/organizations/{org.id}/members/{uid}",
                headers=owner_headers,
                json={"display_name": "Артём"},
            )
            assert response.status_code == 200
            assert response.json()["data"]["display_name"] == "Артём"

    async def test_audit_entry_written(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Артём (ночная)"},
        )
        assert response.status_code == 200

        audit_resp = await client.get(
            f"/api/v1/organizations/{org.id}/audit-logs",
            headers=owner_headers,
            params={"action": "member.display_name_update"},
        )
        assert audit_resp.status_code == 200
        items = audit_resp.json()["data"]["items"]
        assert len(items) == 2
        latest = items[0]  # created_at DESC
        assert latest["summary"]["old_display_name"] == "Тёма"
        assert latest["summary"]["new_display_name"] == "Артём (ночная)"
        assert latest["summary"]["user_id"] == str(employee_user.id)


class TestDisplayNameInResponses:
    """Регресс: поле присутствует (и `null`), `user_name` не подменяется — все схемы ТЗ."""

    async def test_no_regression_when_not_set(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.get(
            f"/api/v1/organizations/{org.id}/members", headers=owner_headers
        )
        item = response.json()["data"]["items"][0]
        assert item["display_name"] is None
        assert item["user_name"] == "Белоусов Артём"

    async def test_member_list(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        response = await client.get(
            f"/api/v1/organizations/{org.id}/members", headers=owner_headers
        )
        item = response.json()["data"]["items"][0]
        assert item["display_name"] == "Тёма"
        assert item["user_name"] == "Белоусов Артём"

    async def test_org_shifts_list_and_detail(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        employee_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        start_resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id)},
        )
        assert start_resp.status_code == 201, start_resp.text
        shift_id = start_resp.json()["data"]["id"]

        list_resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts", headers=owner_headers
        )
        item = list_resp.json()["data"]["items"][0]
        assert item["display_name"] == "Тёма"
        assert item["user_name"] == "Белоусов Артём"

        detail_resp = await client.get(
            f"/api/v1/organizations/{org.id}/shifts/{shift_id}", headers=owner_headers
        )
        detail = detail_resp.json()["data"]
        assert detail["display_name"] == "Тёма"
        assert detail["user_name"] == "Белоусов Артём"

    async def test_checklist_instances_registry(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        employee_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        await _add_template(db_session, org, employee_member)
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        start_resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id)},
        )
        assert start_resp.status_code == 201, start_resp.text

        response = await client.get(
            f"/api/v1/organizations/{org.id}/checklist-instances", headers=owner_headers
        )
        items = response.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["display_name"] == "Тёма"
        assert items[0]["user_name"] == "Белоусов Артём"

    async def test_penalties_list_and_detail(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        create_resp = await client.post(
            f"/api/v1/organizations/{org.id}/penalties",
            headers=owner_headers,
            json={
                "member_id": str(employee_member.id),
                "reason": "Опоздание",
                "amount_minor": 50000,
                "occurred_at": SHIFT_START.isoformat(),
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        penalty_id = create_resp.json()["data"]["id"]
        assert create_resp.json()["data"]["display_name"] == "Тёма"
        assert create_resp.json()["data"]["user_name"] == "Белоусов Артём"

        list_resp = await client.get(
            f"/api/v1/organizations/{org.id}/penalties", headers=owner_headers
        )
        item = list_resp.json()["data"]["items"][0]
        assert item["display_name"] == "Тёма"
        assert item["user_name"] == "Белоусов Артём"

        detail_resp = await client.get(
            f"/api/v1/organizations/{org.id}/penalties/{penalty_id}", headers=owner_headers
        )
        detail = detail_resp.json()["data"]
        assert detail["display_name"] == "Тёма"
        assert detail["user_name"] == "Белоусов Артём"

    async def test_payroll_list_and_detailed(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        await _make_finished_shift(db_session, employee_user.id, org.id)
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        params = {
            "date_from": SHIFT_START.isoformat(),
            "date_to": SHIFT_END.isoformat(),
        }
        list_resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll", headers=owner_headers, params=params
        )
        item = list_resp.json()["data"]["items"][0]
        assert item["display_name"] == "Тёма"
        assert item["user_name"] == "Белоусов Артём"

        detailed_resp = await client.get(
            f"/api/v1/organizations/{org.id}/payroll",
            headers=owner_headers,
            params={**params, "granularity": "day"},
        )
        detailed_item = detailed_resp.json()["data"]["items"][0]
        assert detailed_item["display_name"] == "Тёма"
        assert detailed_item["user_name"] == "Белоусов Артём"

    async def test_org_stats(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        await _make_finished_shift(db_session, employee_user.id, org.id)
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Тёма"},
        )
        response = await client.get(
            f"/api/v1/organizations/{org.id}/stats",
            headers=owner_headers,
            params={
                "date_from": SHIFT_START.isoformat(),
                "date_to": SHIFT_END.isoformat(),
            },
        )
        per_employee = response.json()["data"]["per_employee"]
        assert per_employee[0]["display_name"] == "Тёма"
        assert per_employee[0]["user_name"] == "Белоусов Артём"
