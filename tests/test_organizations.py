# tests/test_organizations.py
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, OrganizationMember
from src.app.models.user import User, UserRole


@pytest.fixture(autouse=True)
async def _owner_is_super_admin(verified_user: User, db_session: AsyncSession) -> None:
    """Создание организации требует super_admin; в этих тестах владелец-создатель
    повышается до super_admin (creator становится owner). На роль владельца тесты
    не опираются, а forbidden-кейсы используют отдельных обычных пользователей."""
    verified_user.role = UserRole.super_admin
    await db_session.commit()


async def _create_second_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="second@example.com",
        password_hash=hash_password("Test1234"),
        name="Second User",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _create_super_admin(db_session: AsyncSession, email: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("Test1234"),
        name="Other Super Admin",
        is_verified=True,
        role=UserRole.super_admin,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _add_member(
    db_session: AsyncSession,
    org_id: str,
    user_id: uuid.UUID,
    role: MemberRole,
) -> None:
    db_session.add(
        OrganizationMember(
            organization_id=uuid.UUID(org_id),
            user_id=user_id,
            role=role,
        )
    )
    await db_session.commit()


async def _login_as(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestCreateOrganization:
    async def test_create_organization_success(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/organizations",
            headers=auth_headers,
            json={"name": "Test Org"},
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["name"] == "Test Org"
        assert len(data["invite_code"]) == 8
        assert data["is_deleted"] is False

    async def test_create_organization_unauthorized(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/organizations",
            json={"name": "Test Org"},
        )
        assert response.status_code == 401

    async def test_create_multiple_organizations(self, client: AsyncClient, auth_headers):
        await client.post("/api/v1/organizations", headers=auth_headers, json={"name": "Org 1"})
        response = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org 2"}
        )
        assert response.status_code == 201

        list_resp = await client.get("/api/v1/organizations", headers=auth_headers)
        assert len(list_resp.json()["data"]["items"]) == 2


class TestUpdateOrganization:
    async def test_update_organization_success(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations",
            headers=auth_headers,
            json={"name": "Old Name"},
        )
        org_id = create_resp.json()["data"]["id"]

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=auth_headers,
            json={"name": "New Name"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["name"] == "New Name"
        # Роль вызывающего в ответе — фактическая (owner), не захардкожена.
        assert data["my_role"] == "owner"
        assert data["my_custom_role"] is None

    async def test_update_by_admin_member(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        admin = await _create_second_user(db_session)
        await _add_member(db_session, org_id, admin.id, MemberRole.admin)
        admin_headers = await _login_as(client, "second@example.com")

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=admin_headers,
            json={"name": "Renamed by admin"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["name"] == "Renamed by admin"
        assert data["my_role"] == "admin"
        assert data["my_custom_role"] is None

    async def test_update_by_admin_member_with_custom_role(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        # Кастомная роль создаётся владельцем и назначается admin-участнику.
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=auth_headers,
            json={"name": "Управляющий"},
        )
        role_id = role_resp.json()["data"]["id"]

        admin = await _create_second_user(db_session)
        await _add_member(db_session, org_id, admin.id, MemberRole.admin)
        await client.patch(
            f"/api/v1/organizations/{org_id}/members/{admin.id}/custom-role",
            headers=auth_headers,
            json={"role_id": role_id},
        )
        admin_headers = await _login_as(client, "second@example.com")

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=admin_headers,
            json={"name": "Renamed"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["my_role"] == "admin"
        assert data["my_custom_role"]["id"] == role_id
        assert data["my_custom_role"]["name"] == "Управляющий"

    async def test_update_by_employee_forbidden(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        employee = await _create_second_user(db_session)
        await _add_member(db_session, org_id, employee.id, MemberRole.employee)
        employee_headers = await _login_as(client, "second@example.com")

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=employee_headers,
            json={"name": "Hacked"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    async def test_update_organization_not_member(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=other_headers,
            json={"name": "Hacked"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    async def test_update_organization_not_found(self, client: AsyncClient, auth_headers):
        response = await client.patch(
            f"/api/v1/organizations/{uuid.uuid4()}",
            headers=auth_headers,
            json={"name": "New Name"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ORG_NOT_FOUND"

    async def test_update_by_foreign_super_admin(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        # Другой super_admin, не owner и не участник этой org.
        await _create_super_admin(db_session, "other-sa@example.com")
        sa_headers = await _login_as(client, "other-sa@example.com")

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=sa_headers,
            json={"name": "Renamed by platform"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["name"] == "Renamed by platform"

    async def test_update_name_only_spaces_422(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=auth_headers,
            json={"name": "   "},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_update_name_too_long_422(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=auth_headers,
            json={"name": "a" * 256},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_update_name_trimmed(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        response = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=auth_headers,
            json={"name": "  Trimmed Name  "},
        )
        assert response.status_code == 200
        assert response.json()["data"]["name"] == "Trimmed Name"

    async def test_update_by_admin_writes_audit(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        admin = await _create_second_user(db_session)
        await _add_member(db_session, org_id, admin.id, MemberRole.admin)
        admin_headers = await _login_as(client, "second@example.com")

        rename_resp = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=admin_headers,
            json={"name": "Renamed by admin"},
        )
        assert rename_resp.status_code == 200

        # Владелец видит запись аудита org.update с actor = админ.
        audit_resp = await client.get(
            f"/api/v1/organizations/{org_id}/audit-logs",
            headers=auth_headers,
            params={"action": "org.update"},
        )
        assert audit_resp.status_code == 200
        items = audit_resp.json()["data"]["items"]
        entry = next(it for it in items if it["action"] == "org.update")
        assert entry["actor_user_id"] == str(admin.id)
        assert entry["summary"]["name"] == "Renamed by admin"


class TestDeleteOrganization:
    async def test_delete_organization_soft(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "To Delete"}
        )
        org_id = create_resp.json()["data"]["id"]

        response = await client.delete(f"/api/v1/organizations/{org_id}", headers=auth_headers)
        assert response.status_code == 200

        list_resp = await client.get("/api/v1/organizations", headers=auth_headers)
        assert len(list_resp.json()["data"]["items"]) == 0

    async def test_delete_organization_not_owner(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.delete(f"/api/v1/organizations/{org_id}", headers=other_headers)
        assert response.status_code == 403


class TestInviteCode:
    async def test_rotate_invite_code(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        old_code = create_resp.json()["data"]["invite_code"]

        response = await client.post(
            f"/api/v1/organizations/{org_id}/rotate-invite", headers=auth_headers
        )
        assert response.status_code == 200
        new_code = response.json()["data"]["invite_code"]
        assert new_code != old_code
        assert len(new_code) == 8

    async def test_rotate_invite_code_by_admin(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        old_code = create_resp.json()["data"]["invite_code"]

        admin = await _create_second_user(db_session)
        await _add_member(db_session, org_id, admin.id, MemberRole.admin)
        admin_headers = await _login_as(client, "second@example.com")

        response = await client.post(
            f"/api/v1/organizations/{org_id}/rotate-invite", headers=admin_headers
        )
        assert response.status_code == 200
        new_code = response.json()["data"]["invite_code"]
        assert new_code != old_code
        assert len(new_code) == 8

        # Старый код после ротации админом невалиден.
        join_resp = await client.post(
            f"/api/v1/organizations/join/{old_code}", headers=admin_headers
        )
        assert join_resp.status_code == 404

    async def test_rotate_invite_code_by_employee_forbidden(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        employee = await _create_second_user(db_session)
        await _add_member(db_session, org_id, employee.id, MemberRole.employee)
        employee_headers = await _login_as(client, "second@example.com")

        response = await client.post(
            f"/api/v1/organizations/{org_id}/rotate-invite", headers=employee_headers
        )
        assert response.status_code == 403
        body = response.json()
        assert body["data"] is None
        assert body["error"]["code"] == "FORBIDDEN"

    async def test_join_by_invite_code(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        invite_code = create_resp.json()["data"]["invite_code"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.post(
            f"/api/v1/organizations/join/{invite_code}", headers=other_headers
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["organization_name"] == "Org"
        assert data["role"] == "employee"

    async def test_join_invalid_code(self, client: AsyncClient, auth_headers):
        response = await client.post("/api/v1/organizations/join/INVALID1", headers=auth_headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "INVALID_INVITE"

    async def test_join_already_member(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        invite_code = create_resp.json()["data"]["invite_code"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)
        response = await client.post(
            f"/api/v1/organizations/join/{invite_code}", headers=other_headers
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ALREADY_MEMBER"

    async def test_owner_cannot_join_own_org(self, client: AsyncClient, auth_headers):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        invite_code = create_resp.json()["data"]["invite_code"]

        response = await client.post(
            f"/api/v1/organizations/join/{invite_code}", headers=auth_headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "OWNER_CANNOT_JOIN"

    async def test_join_deleted_org(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        await client.delete(f"/api/v1/organizations/{org_id}", headers=auth_headers)

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.post(
            f"/api/v1/organizations/join/{invite_code}", headers=other_headers
        )
        assert response.status_code == 404


class TestMembers:
    async def test_list_members(self, client: AsyncClient, auth_headers, db_session: AsyncSession):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)

        response = await client.get(
            f"/api/v1/organizations/{org_id}/members", headers=auth_headers
        )
        assert response.status_code == 200
        members = response.json()["data"]["items"]
        assert len(members) == 1
        assert members[0]["role"] == "employee"
        assert members[0]["user_email"] == "second@example.com"

    async def test_remove_member_by_owner(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        second_user = await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)

        response = await client.delete(
            f"/api/v1/organizations/{org_id}/members/{second_user.id}",
            headers=auth_headers,
        )
        assert response.status_code == 200

        members_resp = await client.get(
            f"/api/v1/organizations/{org_id}/members", headers=auth_headers
        )
        assert len(members_resp.json()["data"]["items"]) == 0

    async def test_member_self_leave(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        second_user = await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)

        response = await client.delete(
            f"/api/v1/organizations/{org_id}/members/{second_user.id}",
            headers=other_headers,
        )
        assert response.status_code == 200

    async def test_employee_cannot_remove_other(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]
        invite_code = create_resp.json()["data"]["invite_code"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=other_headers)

        third_user = User(
            id=uuid.uuid4(),
            email="third@example.com",
            password_hash=hash_password("Test1234"),
            name="Third User",
            is_verified=True,
        )
        db_session.add(third_user)
        await db_session.commit()
        third_headers = await _login_as(client, "third@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=third_headers)

        response = await client.delete(
            f"/api/v1/organizations/{org_id}/members/{third_user.id}",
            headers=other_headers,
        )
        assert response.status_code == 403

    async def test_list_members_forbidden(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        create_resp = await client.post(
            "/api/v1/organizations", headers=auth_headers, json={"name": "Org"}
        )
        org_id = create_resp.json()["data"]["id"]

        await _create_second_user(db_session)
        other_headers = await _login_as(client, "second@example.com")

        response = await client.get(
            f"/api/v1/organizations/{org_id}/members", headers=other_headers
        )
        assert response.status_code == 403
