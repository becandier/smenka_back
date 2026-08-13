# tests/test_admin_created_accounts.py
"""Фича admin_created_accounts: учётки, заводимые админом организации, вход по логину.

Покрывает:
- `POST /organizations/{org_id}/members` — создание сотрудника (login/email,
  генерация/явный пароль, права на роль admin, конфликты login/email);
- `POST /organizations/{org_id}/members/{user_id}/reset-password` — сброс
  пароля (только для учёток, заведённых ЭТОЙ организацией; отзыв refresh-токенов);
- `PATCH /organizations/{org_id}/members/{user_id}` — смена `login` (то же
  правило владения, что и у сброса пароля; занят → 409 LOGIN_TAKEN);
- `POST /auth/login` — вход по `login` или `email` (обратная совместимость
  старого тела, регистронезависимость email, неоднозначное совпадение).
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
    email: str | None,
    name: str,
    *,
    login: str | None = None,
    created_by_org_id: uuid.UUID | None = None,
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        login=login,
        password_hash=hash_password("Test1234"),
        name=name,
        is_verified=True,
        created_by_org_id=created_by_org_id,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _login(
    client: AsyncClient,
    *,
    email: str | None = None,
    login: str | None = None,
    password: str,
) -> dict:
    body: dict[str, str] = {"password": password}
    if email is not None:
        body["email"] = email
    if login is not None:
        body["login"] = login
    resp = await client.post("/api/v1/auth/login", json=body)
    return resp.json()


async def _login_headers(client: AsyncClient, email: str) -> dict[str, str]:
    payload = await _login(client, email=email, password="Test1234")
    return {"Authorization": f"Bearer {payload['data']['access_token']}"}


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "owner@example.com", "Владелец Кофейни")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    return await _login_headers(client, "owner@example.com")


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
    return await _login_headers(client, "orgadmin@example.com")


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
    return await _login_headers(client, "employee@example.com")


@pytest.fixture
async def self_registered_user(db_session: AsyncSession) -> User:
    """Сотрудник, зарегистрировавшийся сам и пришедший по инвайту: `created_by_org_id`
    не заполнен — под правило «эта организация завела учётку» не попадает."""
    return await _create_user(db_session, "selfjoined@example.com", "Самостоятельный")


@pytest.fixture
async def self_registered_member(
    db_session: AsyncSession, org: Organization, self_registered_user: User
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id,
        user_id=self_registered_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return member


class TestCreateMember:
    """`POST /organizations/{org_id}/members`."""

    async def test_owner_creates_member_login_only(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Иван Иванов", "login": "ivanov"},
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["login"] == "ivanov"
        assert len(data["password"]) == 10
        member = data["member"]
        assert member["user_login"] == "ivanov"
        assert member["user_email"] == ""
        assert member["password_managed"] is True
        assert member["role"] == "employee"

    async def test_generated_password_logs_in(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        create_resp = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Иван Иванов", "login": "ivanov2"},
        )
        password = create_resp.json()["data"]["password"]

        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"login": "ivanov2", "password": password},
        )
        assert login_resp.status_code == 200, login_resp.text
        assert login_resp.json()["data"]["access_token"] is not None

    async def test_owner_creates_member_with_explicit_password(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Пётр Петров", "login": "petrov", "password": "MyPass123"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["data"]["password"] == "MyPass123"

    async def test_email_only_no_login(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Сидор Сидоров", "email": "sidorov@example.com"},
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["login"] is None
        member = data["member"]
        assert member["user_email"] == "sidorov@example.com"
        assert member["user_login"] is None

    async def test_missing_login_and_email_rejected(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Без идентификатора"},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert any("login" in item["field"] for item in body["error"]["validation"])

    async def test_login_taken_case_insensitive(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Первый", "login": "sklad1"},
        )
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Второй", "login": "SKLAD1"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LOGIN_TAKEN"

    async def test_email_taken(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Дубликат", "email": employee_user.email},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EMAIL_TAKEN"

    async def test_admin_cannot_create_admin_role(
        self,
        client: AsyncClient,
        admin_headers: dict[str, str],
        org: Organization,
        admin_member: OrganizationMember,
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=admin_headers,
            json={"name": "Новый Админ", "login": "newadmin", "role": "admin"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    async def test_owner_can_create_admin_role(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Новый Админ", "login": "newadmin2", "role": "admin"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["data"]["member"]["role"] == "admin"

    async def test_employee_forbidden(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        org: Organization,
        employee_member: OrganizationMember,
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=employee_headers,
            json={"name": "Кто-то", "login": "someone"},
        )
        assert response.status_code == 403

    async def test_weak_password_rejected(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Слабый", "login": "weakpass", "password": "short"},
        )
        assert response.status_code == 422

    async def test_invalid_login_format_rejected(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Плохой логин", "login": "a"},
        )
        assert response.status_code == 422

    async def test_role_not_found(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "С ролью", "login": "withrole", "role_id": str(uuid.uuid4())},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ROLE_NOT_FOUND"

    async def test_audit_entry_no_password(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Аудит Тестов", "login": "audituser"},
        )
        password = response.json()["data"]["password"]

        audit_resp = await client.get(
            f"/api/v1/organizations/{org.id}/audit-logs",
            headers=owner_headers,
            params={"action": "member.create"},
        )
        items = audit_resp.json()["data"]["items"]
        assert len(items) == 1
        summary = items[0]["summary"]
        assert summary["role"] == "employee"
        assert summary["login"] == "audituser"
        assert summary["has_email"] is False
        assert password not in str(items[0])


class TestResetPassword:
    """`POST /organizations/{org_id}/members/{user_id}/reset-password`."""

    async def test_owner_resets_org_created_member_password(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        create_resp = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Сброс Пароля", "login": "resetme"},
        )
        old_password = create_resp.json()["data"]["password"]
        user_id = create_resp.json()["data"]["member"]["user_id"]

        old_login = await _login(client, login="resetme", password=old_password)
        old_refresh = old_login["data"]["refresh_token"]

        reset_resp = await client.post(
            f"/api/v1/organizations/{org.id}/members/{user_id}/reset-password",
            headers=owner_headers,
            json={"password": None},
        )
        assert reset_resp.status_code == 200, reset_resp.text
        new_data = reset_resp.json()["data"]
        assert new_data["user_id"] == user_id
        assert new_data["login"] == "resetme"
        assert new_data["password"] != old_password

        # Старый refresh-токен отозван.
        refresh_resp = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": old_refresh}
        )
        assert refresh_resp.status_code == 401

        # Старый пароль больше не работает, новый — работает.
        old_after_reset = await _login(client, login="resetme", password=old_password)
        assert old_after_reset["error"] is not None
        new_login = await client.post(
            "/api/v1/auth/login",
            json={"login": "resetme", "password": new_data["password"]},
        )
        assert new_login.status_code == 200

    async def test_reset_with_explicit_password(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        create_resp = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Явный Сброс", "login": "explicitreset"},
        )
        user_id = create_resp.json()["data"]["member"]["user_id"]

        reset_resp = await client.post(
            f"/api/v1/organizations/{org.id}/members/{user_id}/reset-password",
            headers=owner_headers,
            json={"password": "NewPass123"},
        )
        assert reset_resp.status_code == 200
        assert reset_resp.json()["data"]["password"] == "NewPass123"

    async def test_reset_not_allowed_for_self_registered_member(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        self_registered_user: User,
        self_registered_member: OrganizationMember,
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members/{self_registered_user.id}/reset-password",
            headers=owner_headers,
            json={},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PASSWORD_RESET_NOT_ALLOWED"

    async def test_reset_member_not_found(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members/{uuid.uuid4()}/reset-password",
            headers=owner_headers,
            json={},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "MEMBER_NOT_FOUND"

    async def test_employee_forbidden(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        org: Organization,
        employee_member: OrganizationMember,
        employee_user: User,
    ):
        response = await client.post(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}/reset-password",
            headers=employee_headers,
            json={},
        )
        assert response.status_code == 403

    async def test_audit_entry_no_password(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        create_resp = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Аудит Сброса", "login": "auditreset"},
        )
        user_id = create_resp.json()["data"]["member"]["user_id"]

        reset_resp = await client.post(
            f"/api/v1/organizations/{org.id}/members/{user_id}/reset-password",
            headers=owner_headers,
            json={},
        )
        password = reset_resp.json()["data"]["password"]

        audit_resp = await client.get(
            f"/api/v1/organizations/{org.id}/audit-logs",
            headers=owner_headers,
            params={"action": "member.password_reset"},
        )
        items = audit_resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["summary"] == {"login": "auditreset"}
        assert password not in str(items[0])


class TestUpdateMemberLogin:
    """`PATCH /organizations/{org_id}/members/{user_id}` — поле `login`."""

    async def test_owner_changes_login(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        create_resp = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Смена Логина", "login": "oldlogin", "password": "Test1234"},
        )
        user_id = create_resp.json()["data"]["member"]["user_id"]

        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{user_id}",
            headers=owner_headers,
            json={"login": "newlogin"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["user_login"] == "newlogin"

        # Старый логин больше не годится (PATCH сменил его), новый — работает
        # с тем же паролем (PATCH login пароль не трогает).
        old = await _login(client, login="oldlogin", password="Test1234")
        assert old["error"] is not None
        new = await client.post(
            "/api/v1/auth/login", json={"login": "newlogin", "password": "Test1234"}
        )
        assert new.status_code == 200, new.text

    async def test_login_taken_on_patch(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Первый", "login": "taken1"},
        )
        second = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Второй", "login": "taken2"},
        )
        user_id2 = second.json()["data"]["member"]["user_id"]

        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{user_id2}",
            headers=owner_headers,
            json={"login": "TAKEN1"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "LOGIN_TAKEN"

    async def test_change_login_not_allowed_for_self_registered_member(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        self_registered_user: User,
        self_registered_member: OrganizationMember,
    ):
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{self_registered_user.id}",
            headers=owner_headers,
            json={"login": "hijacked"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "PASSWORD_RESET_NOT_ALLOWED"

    async def test_display_name_unaffected_by_absent_key(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        employee_user: User,
        employee_member: OrganizationMember,
    ):
        """Ключ display_name отсутствует в теле — не должен затираться (partial)."""
        await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={"display_name": "Артём"},
        )
        response = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{employee_user.id}",
            headers=owner_headers,
            json={},
        )
        assert response.status_code == 200
        assert response.json()["data"]["display_name"] == "Артём"

    async def test_empty_body_still_checks_permissions(
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
            json={},
        )
        assert response.status_code == 403

    async def test_invalid_login_format_rejected(
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
            json={"login": "a"},
        )
        assert response.status_code == 422


class TestLoginByLoginOrEmail:
    """`POST /auth/login` — вход по `login` или `email`."""

    async def test_login_by_login_field(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Логин Юзер", "login": "loginuser", "password": "Test1234"},
        )
        response = await client.post(
            "/api/v1/auth/login", json={"login": "loginuser", "password": "Test1234"}
        )
        assert response.status_code == 200
        assert response.json()["data"]["access_token"] is not None

    async def test_login_by_old_email_body_still_works(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _create_user(db_session, "legacy@example.com", "Legacy Client")
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "legacy@example.com", "password": "Test1234"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["access_token"] is not None

    async def test_login_by_email_different_case(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _create_user(db_session, "casetest@example.com", "Case Test")
        response = await client.post(
            "/api/v1/auth/login",
            json={"login": "CaseTest@Example.com", "password": "Test1234"},
        )
        assert response.status_code == 200

    async def test_login_neither_field_rejected(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/login", json={"password": "Test1234"})
        assert response.status_code == 422

    async def test_login_both_fields_rejected(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/login",
            json={"login": "x", "email": "x@example.com", "password": "Test1234"},
        )
        assert response.status_code == 422

    async def test_login_wrong_password_message(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        await _create_user(db_session, None, "Только логин", login="onlylogin")
        response = await client.post(
            "/api/v1/auth/login",
            json={"login": "onlylogin", "password": "WrongPass1"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_ambiguous_email_case_match_rejected(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Два пользователя с разным регистром одного email (уникальность email
        сейчас регистрозависима — известная особенность, вне scope) → вход по
        нормализованному идентификатору находит >1 совпадение и отклоняется."""
        await _create_user(db_session, "dup@example.com", "Первый")
        await _create_user(db_session, "DUP@example.com", "Второй")

        response = await client.post(
            "/api/v1/auth/login",
            json={"login": "dup@example.com", "password": "Test1234"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


class TestMemberResponseFields:
    """Регресс: `user_email`/`user_login`/`password_managed` во всех орг-ответах."""

    async def test_login_only_member_shows_empty_email_string(
        self, client: AsyncClient, owner_headers: dict[str, str], org: Organization
    ):
        await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Без Почты", "login": "noemail"},
        )
        response = await client.get(
            f"/api/v1/organizations/{org.id}/members", headers=owner_headers
        )
        items = response.json()["data"]["items"]
        target = next(i for i in items if i["user_login"] == "noemail")
        assert target["user_email"] == ""
        assert target["password_managed"] is True

    async def test_self_registered_member_password_not_managed(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        org: Organization,
        self_registered_user: User,
        self_registered_member: OrganizationMember,
    ):
        response = await client.get(
            f"/api/v1/organizations/{org.id}/members", headers=owner_headers
        )
        items = response.json()["data"]["items"]
        target = next(i for i in items if i["user_id"] == str(self_registered_user.id))
        assert target["password_managed"] is False
        assert target["user_email"] == "selfjoined@example.com"
