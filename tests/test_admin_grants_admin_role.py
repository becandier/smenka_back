# tests/test_admin_grants_admin_role.py
"""Фича admin_grants_admin_role: админ организации получает право назначать роль admin.

Покрывает `PATCH /organizations/{org_id}/members/{user_id}/role`:
- владелец, admin-участник организации и super_admin могут менять роль (в обе
  стороны, employee<->admin);
- участнику запрещено менять роль самому себе (кроме super_admin) — 403 FORBIDDEN;
- сотрудник (employee) не может менять роли вообще — 403 FORBIDDEN;
- чужая организация/несуществующий участник — 403/404 как раньше;
- аудит-событие `member.role_update` фиксирует, кто и на какую роль изменил.

Право admin-участника заводить учётку с role=admin (второе изменение фичи)
покрыто в `tests/test_admin_created_accounts.py::TestCreateMember`.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.user import User


async def _create_user(
    db_session: AsyncSession,
    email: str,
    name: str,
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


async def _login_headers(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "gar-owner@example.com", "Владелец Кофейни")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    return await _login_headers(client, "gar-owner@example.com")


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
        )
    )
    await db_session.commit()
    return organization


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "gar-admin@example.com", "Админ Организации")


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
    return await _login_headers(client, "gar-admin@example.com")


@pytest.fixture
async def employee_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "gar-employee@example.com", "Белоусов Артём")


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
    return await _login_headers(client, "gar-employee@example.com")


class TestAdminCanChangeRole:
    """Основное изменение фичи: admin-участник организации меняет роль другого участника."""

    async def test_admin_promotes_employee_to_admin(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        org: Organization,
        admin_member: OrganizationMember,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}/role",
            headers=admin_headers,
            json={"role": "admin"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["role"] == "admin"

    async def test_admin_demotes_admin_to_employee(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        org: Organization,
        admin_member: OrganizationMember,
        db_session: AsyncSession,
    ):
        """Понижение admin -> employee админом разрешено (обоснование в backend.md:
        админ и так может удалить другого админа через remove_member)."""
        other_admin = await _create_user(db_session, "gar-other-admin@example.com", "Второй Админ")
        db_session.add(
            OrganizationMember(
                organization_id=org.id, user_id=other_admin.id, role=MemberRole.admin
            )
        )
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{other_admin.id}/role",
            headers=admin_headers,
            json={"role": "employee"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["role"] == "employee"


class TestOwnerAndSuperAdminRegression:
    async def test_owner_updates_role(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}/role",
            headers=owner_headers,
            json={"role": "admin"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["role"] == "admin"

    async def test_super_admin_updates_role_in_foreign_org(
        self,
        client: AsyncClient,
        super_admin_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}/role",
            headers=super_admin_headers,
            json={"role": "admin"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["role"] == "admin"


class TestEmployeeForbidden:
    async def test_employee_cannot_change_others_role(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        org: Organization,
        employee_member: OrganizationMember,
        admin_user: User,
        admin_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{admin_user.id}/role",
            headers=employee_headers,
            json={"role": "employee"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    async def test_employee_cannot_change_own_role(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}/role",
            headers=employee_headers,
            json={"role": "admin"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"


class TestSelfRoleChangeForbidden:
    """Новый инвариант: участник не может изменить собственную роль (не касается super_admin)."""

    async def test_admin_cannot_change_own_role(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        org: Organization,
        admin_user: User,
        admin_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{admin_user.id}/role",
            headers=admin_headers,
            json={"role": "employee"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    async def test_super_admin_self_role_change_exception(
        self,
        client: AsyncClient,
        super_admin_headers: dict[str, str],
        super_admin_user: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        """super_admin не участник организации, но если его user_id всё же совпал
        с member_user_id (сквозной доступ), инвариант self-change его не блокирует."""
        member = OrganizationMember(
            organization_id=org.id, user_id=super_admin_user.id, role=MemberRole.employee
        )
        db_session.add(member)
        await db_session.commit()

        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{super_admin_user.id}/role",
            headers=super_admin_headers,
            json={"role": "admin"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["role"] == "admin"


class TestForeignOrgAndNotFound:
    async def test_member_of_another_org_forbidden(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        outsider_owner = await _create_user(db_session, "gar-outsider@example.com", "Чужой")
        outsider_headers = await _login_headers(client, "gar-outsider@example.com")

        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}/role",
            headers=outsider_headers,
            json={"role": "admin"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
        del outsider_owner

    async def test_nonexistent_org_404(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        employee_user: User,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{uuid.uuid4()}/members/{employee_user.id}/role",
            headers=owner_headers,
            json={"role": "admin"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ORG_NOT_FOUND"

    async def test_member_not_found_404(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{uuid.uuid4()}/role",
            headers=owner_headers,
            json={"role": "admin"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "MEMBER_NOT_FOUND"

    async def test_invalid_role_400(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}/role",
            headers=owner_headers,
            json={"role": "owner"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ROLE"


class TestAudit:
    async def test_role_update_writes_audit_log(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        owner_headers: dict[str, str],
        org: Organization,
        admin_member: OrganizationMember,
        admin_user: User,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}/role",
            headers=admin_headers,
            json={"role": "admin"},
        )
        assert response.status_code == 200, response.text

        audit_resp = await client.get(
            f"/api/v1/organizations/{org.id}/audit-logs",
            headers=owner_headers,
            params={"action": "member.role_update"},
        )
        assert audit_resp.status_code == 200
        items = audit_resp.json()["data"]["items"]
        entry = next(it for it in items if it["action"] == "member.role_update")
        assert entry["actor_user_id"] == str(admin_user.id)
        assert entry["summary"]["new_role"] == "admin"
        assert entry["summary"]["user_id"] == str(employee_user.id)
