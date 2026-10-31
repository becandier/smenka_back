# tests/test_access_profiles.py
"""Фича access_profiles (ADR-006): профили доступа к веб-кабинету организации.

Покрывает:
- каталог разделов `GET /access-permissions`;
- CRUD профилей: уникальность имени без регистра, валидация `permissions`,
  soft-delete с отвязкой участников, restore и конфликты;
- выдачу профиля участнику (`PATCH .../members/{uid}/access-profile`) и при
  создании участника, обнуление профиля при смене роли на admin;
- матрицу прав: для каждого раздела каталога `view` проходит чтение и получает
  403 на мутацию, `manage` проходит мутацию, без раздела — 403;
- защиту от эскалации обладателем `members:manage`;
- `my_permissions` / `my_access_profile` в `OrganizationResponse`;
- закрытие `GET /organizations/{org_id}` для не-участника;
- исключения `allow_super_admin=False`;
- записи аудита.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.access_catalog import ACCESS_SECTIONS, full_permissions
from src.app.core.security import hash_password
from src.app.models.access_profile import OrganizationAccessProfile
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.user import User

PASSWORD = "Test1234"


async def _create_user(db_session: AsyncSession, email: str, name: str = "Сотрудник") -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password(PASSWORD),
        name=name,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


async def _add_member(
    db_session: AsyncSession,
    org: Organization,
    user: User,
    role: MemberRole = MemberRole.employee,
    profile: OrganizationAccessProfile | None = None,
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id,
        user_id=user.id,
        role=role,
        access_profile_id=profile.id if profile is not None else None,
    )
    db_session.add(member)
    await db_session.commit()
    return member


async def _create_profile(
    db_session: AsyncSession,
    org: Organization,
    permissions: dict[str, str],
    name: str | None = None,
) -> OrganizationAccessProfile:
    profile = OrganizationAccessProfile(
        organization_id=org.id,
        name=name or f"Профиль {uuid.uuid4().hex[:6]}",
        permissions=permissions,
    )
    db_session.add(profile)
    await db_session.commit()
    return profile


async def _make_holder(
    db_session: AsyncSession,
    client: AsyncClient,
    org: Organization,
    permissions: dict[str, str],
) -> tuple[dict[str, str], OrganizationMember]:
    """Сотрудник-employee с профилем `permissions`; возвращает (headers, member)."""
    email = f"holder-{uuid.uuid4().hex[:8]}@example.com"
    user = await _create_user(db_session, email, "Обладатель Профиля")
    profile = await _create_profile(db_session, org, permissions)
    member = await _add_member(db_session, org, user, profile=profile)
    return await _login(client, email), member


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "ap-owner@example.com", "Владелец")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    return await _login(client, "ap-owner@example.com")


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
async def other_org(db_session: AsyncSession) -> Organization:
    other_owner = await _create_user(db_session, "ap-other-owner@example.com", "Чужой")
    organization = Organization(name="Чужая", owner_id=other_owner.id)
    db_session.add(organization)
    await db_session.commit()
    return organization


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "ap-admin@example.com", "Админ")


@pytest.fixture
async def admin_member(
    db_session: AsyncSession, org: Organization, admin_user: User
) -> OrganizationMember:
    return await _add_member(db_session, org, admin_user, MemberRole.admin)


@pytest.fixture
async def admin_headers(
    admin_member: OrganizationMember, client: AsyncClient
) -> dict[str, str]:
    return await _login(client, "ap-admin@example.com")


@pytest.fixture
async def employee_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "ap-employee@example.com", "Сотрудник Без Профиля")


@pytest.fixture
async def employee_member(
    db_session: AsyncSession, org: Organization, employee_user: User
) -> OrganizationMember:
    return await _add_member(db_session, org, employee_user)


@pytest.fixture
async def employee_headers(
    employee_member: OrganizationMember, client: AsyncClient
) -> dict[str, str]:
    return await _login(client, "ap-employee@example.com")


def _profiles_url(org_id: uuid.UUID, profile_id: uuid.UUID | str | None = None) -> str:
    base = f"/api/v1/organizations/{org_id}/access-profiles"
    return base if profile_id is None else f"{base}/{profile_id}"


def _assign_url(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return f"/api/v1/organizations/{org_id}/members/{user_id}/access-profile"


async def _audit_actions(
    client: AsyncClient, org_id: uuid.UUID, headers: dict[str, str]
) -> list[dict[str, Any]]:
    resp = await client.get(f"/api/v1/organizations/{org_id}/audit-logs", headers=headers)
    assert resp.status_code == 200, resp.text
    return list(resp.json()["data"]["items"])


# ─── Каталог ──────────────────────────────────────────────────────────────────


class TestCatalog:
    async def test_catalog_order_and_levels(
        self, client: AsyncClient, employee_headers: dict[str, str]
    ) -> None:
        resp = await client.get("/api/v1/access-permissions", headers=employee_headers)
        assert resp.status_code == 200
        sections = resp.json()["data"]["sections"]
        assert [s["code"] for s in sections] == [s.code for s in ACCESS_SECTIONS]
        by_code = {s["code"]: s for s in sections}
        assert by_code["shifts"] == {
            "code": "shifts",
            "group": "shifts",
            "levels": ["view", "manage"],
        }
        assert by_code["checklist_instances"]["levels"] == ["view"]
        assert by_code["stats"]["levels"] == ["view"]
        assert by_code["audit"]["levels"] == ["view"]

    async def test_catalog_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/access-permissions")
        assert resp.status_code == 401


# ─── CRUD профилей ────────────────────────────────────────────────────────────


class TestProfileCrud:
    async def test_create_list_get(
        self,
        client: AsyncClient,
        org: Organization,
        owner_headers: dict[str, str],
    ) -> None:
        resp = await client.post(
            _profiles_url(org.id),
            headers=owner_headers,
            json={
                "name": "  Бухгалтер ",
                "description": "Зарплата и штрафы",
                "permissions": {"penalties": "manage", "payroll": "manage", "shifts": "view"},
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["name"] == "Бухгалтер"
        assert data["description"] == "Зарплата и штрафы"
        # Порядок ключей — как в каталоге.
        assert list(data["permissions"]) == ["shifts", "payroll", "penalties"]
        assert data["members_count"] == 0
        assert data["is_deleted"] is False
        assert data["deleted_at"] is None

        resp = await client.get(_profiles_url(org.id, data["id"]), headers=owner_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["id"] == data["id"]

        resp = await client.get(_profiles_url(org.id), headers=owner_headers)
        assert resp.status_code == 200
        assert [p["name"] for p in resp.json()["data"]["items"]] == ["Бухгалтер"]

    async def test_list_sorted_by_name_with_members_count(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner_headers: dict[str, str],
    ) -> None:
        p_b = await _create_profile(db_session, org, {"shifts": "view"}, name="Контролёр")
        await _create_profile(db_session, org, {"payroll": "view"}, name="Бухгалтер")
        for i in range(2):
            user = await _create_user(db_session, f"cnt-{i}@example.com")
            await _add_member(db_session, org, user, profile=p_b)

        resp = await client.get(_profiles_url(org.id), headers=owner_headers)
        items = resp.json()["data"]["items"]
        assert [p["name"] for p in items] == ["Бухгалтер", "Контролёр"]
        assert {p["name"]: p["members_count"] for p in items} == {
            "Бухгалтер": 0,
            "Контролёр": 2,
        }

    async def test_name_unique_case_insensitive(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        other_org: Organization,
        owner_headers: dict[str, str],
    ) -> None:
        await _create_profile(db_session, org, {"shifts": "view"}, name="Бухгалтер")
        # В другой организации то же имя не мешает.
        await _create_profile(db_session, other_org, {"shifts": "view"}, name="Кассир")
        resp = await client.post(
            _profiles_url(org.id),
            headers=owner_headers,
            json={"name": "БУХГАЛТЕР", "permissions": {"payroll": "view"}},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_NAME_TAKEN"

        resp = await client.post(
            _profiles_url(org.id),
            headers=owner_headers,
            json={"name": "кассир", "permissions": {"payroll": "view"}},
        )
        assert resp.status_code == 201

    @pytest.mark.parametrize(
        "permissions",
        [
            {},
            {"unknown_section": "view"},
            {"stats": "manage"},
            {"shifts": "edit"},
            {"audit": "manage"},
        ],
    )
    async def test_permissions_validation(
        self,
        client: AsyncClient,
        org: Organization,
        owner_headers: dict[str, str],
        permissions: dict[str, str],
    ) -> None:
        resp = await client.post(
            _profiles_url(org.id),
            headers=owner_headers,
            json={"name": "Неверный", "permissions": permissions},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_patch_partial_and_rename_conflict(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner_headers: dict[str, str],
    ) -> None:
        profile = await _create_profile(db_session, org, {"shifts": "view"}, name="Контролёр")
        await _create_profile(db_session, org, {"payroll": "view"}, name="Бухгалтер")

        resp = await client.patch(
            _profiles_url(org.id, profile.id),
            headers=owner_headers,
            json={"permissions": {"shifts": "manage", "overtime": "view"}},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["permissions"] == {"shifts": "manage", "overtime": "view"}
        assert data["name"] == "Контролёр"

        resp = await client.patch(
            _profiles_url(org.id, profile.id),
            headers=owner_headers,
            json={"name": "бухгалтер"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_NAME_TAKEN"

        resp = await client.patch(
            _profiles_url(org.id, profile.id),
            headers=owner_headers,
            json={"permissions": {}},
        )
        assert resp.status_code == 422

        resp = await client.patch(
            _profiles_url(org.id, uuid.uuid4()),
            headers=owner_headers,
            json={"name": "Х"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_NOT_FOUND"

    async def test_delete_detaches_members_and_restore(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner_headers: dict[str, str],
    ) -> None:
        profile = await _create_profile(db_session, org, {"shifts": "view"}, name="Контролёр")
        user = await _create_user(db_session, "detach@example.com")
        member = await _add_member(db_session, org, user, profile=profile)

        resp = await client.delete(_profiles_url(org.id, profile.id), headers=owner_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"] == {"message": "Профиль доступа удалён", "detached_members": 1}

        await db_session.refresh(member)
        assert member.access_profile_id is None

        # Повторное удаление — 404.
        resp = await client.delete(_profiles_url(org.id, profile.id), headers=owner_headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_NOT_FOUND"

        # Удалённый виден только с include_deleted.
        resp = await client.get(_profiles_url(org.id), headers=owner_headers)
        assert resp.json()["data"]["items"] == []
        resp = await client.get(
            _profiles_url(org.id), headers=owner_headers, params={"include_deleted": True}
        )
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["is_deleted"] is True
        assert items[0]["deleted_at"] is not None

        # Имя освободилось — занимаем его новым профилем.
        resp = await client.post(
            _profiles_url(org.id),
            headers=owner_headers,
            json={"name": "КОНТРОЛЁР", "permissions": {"stats": "view"}},
        )
        assert resp.status_code == 201
        new_id = resp.json()["data"]["id"]

        # Restore при занятом имени — 409.
        resp = await client.post(f"{_profiles_url(org.id, profile.id)}/restore", headers=owner_headers)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_NAME_TAKEN"

        # Освобождаем имя и восстанавливаем; участник не возвращается.
        resp = await client.delete(_profiles_url(org.id, new_id), headers=owner_headers)
        assert resp.status_code == 200
        resp = await client.post(f"{_profiles_url(org.id, profile.id)}/restore", headers=owner_headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["is_deleted"] is False
        assert data["members_count"] == 0

        # Restore активного — 409.
        resp = await client.post(f"{_profiles_url(org.id, profile.id)}/restore", headers=owner_headers)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_NOT_DELETED"

    async def test_foreign_org_profile_is_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        other_org: Organization,
        owner_headers: dict[str, str],
    ) -> None:
        foreign = await _create_profile(db_session, other_org, {"shifts": "view"})
        resp = await client.get(_profiles_url(org.id, foreign.id), headers=owner_headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_NOT_FOUND"

    async def test_admin_and_super_admin_manage_profiles(
        self,
        client: AsyncClient,
        org: Organization,
        admin_headers: dict[str, str],
        super_admin_headers: dict[str, str],
    ) -> None:
        for i, headers in enumerate((admin_headers, super_admin_headers)):
            resp = await client.post(
                _profiles_url(org.id),
                headers=headers,
                json={"name": f"Профиль {i}", "permissions": {"stats": "view"}},
            )
            assert resp.status_code == 201, resp.text

    async def test_profile_access_rules(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        employee_headers: dict[str, str],
    ) -> None:
        profile = await _create_profile(db_session, org, {"shifts": "view"})
        # employee без профиля — ни чтения, ни мутаций.
        resp = await client.get(_profiles_url(org.id), headers=employee_headers)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

        # members:manage читает профили, но управлять ими не может.
        headers, _ = await _make_holder(db_session, client, org, {"members": "manage"})
        resp = await client.get(_profiles_url(org.id), headers=headers)
        assert resp.status_code == 200
        resp = await client.get(_profiles_url(org.id, profile.id), headers=headers)
        assert resp.status_code == 200
        for method, url, body in (
            ("POST", _profiles_url(org.id), {"name": "Себе", "permissions": {"audit": "view"}}),
            ("PATCH", _profiles_url(org.id, profile.id), {"name": "Новое"}),
            ("DELETE", _profiles_url(org.id, profile.id), None),
            ("POST", f"{_profiles_url(org.id, profile.id)}/restore", None),
        ):
            resp = await client.request(method, url, headers=headers, json=body)
            assert resp.status_code == 403, (method, url, resp.text)

    async def test_permissions_change_applies_immediately(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner_headers: dict[str, str],
    ) -> None:
        headers, member = await _make_holder(db_session, client, org, {"stats": "view"})
        stats_url = f"/api/v1/organizations/{org.id}/stats"
        resp = await client.get(stats_url, headers=headers, params={"period": "day"})
        assert resp.status_code == 200

        resp = await client.patch(
            _profiles_url(org.id, member.access_profile_id),
            headers=owner_headers,
            json={"permissions": {"audit": "view"}},
        )
        assert resp.status_code == 200
        resp = await client.get(stats_url, headers=headers, params={"period": "day"})
        assert resp.status_code == 403
        assert resp.json()["error"]["message"] == (
            "Недостаточно прав: требуется «Статистика» — просмотр"
        )


# ─── Выдача профиля ───────────────────────────────────────────────────────────


class TestAssignment:
    async def test_assign_and_clear(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner_headers: dict[str, str],
        employee_member: OrganizationMember,
        employee_user: User,
    ) -> None:
        profile = await _create_profile(db_session, org, {"shifts": "view"}, name="Контролёр")
        resp = await client.patch(
            _assign_url(org.id, employee_user.id),
            headers=owner_headers,
            json={"access_profile_id": str(profile.id)},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["access_profile"] == {
            "id": str(profile.id),
            "name": "Контролёр",
        }

        resp = await client.get(f"/api/v1/organizations/{org.id}/members", headers=owner_headers)
        by_user = {m["user_id"]: m for m in resp.json()["data"]["items"]}
        assert by_user[str(employee_user.id)]["access_profile"]["id"] == str(profile.id)

        resp = await client.get(_profiles_url(org.id, profile.id), headers=owner_headers)
        assert resp.json()["data"]["members_count"] == 1

        resp = await client.patch(
            _assign_url(org.id, employee_user.id),
            headers=owner_headers,
            json={"access_profile_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["access_profile"] is None

    async def test_assign_to_admin_is_409(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner_headers: dict[str, str],
        admin_member: OrganizationMember,
        admin_user: User,
    ) -> None:
        profile = await _create_profile(db_session, org, {"shifts": "view"})
        resp = await client.patch(
            _assign_url(org.id, admin_user.id),
            headers=owner_headers,
            json={"access_profile_id": str(profile.id)},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_REQUIRES_EMPLOYEE"

    async def test_cannot_assign_to_self(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        admin_headers: dict[str, str],
        admin_user: User,
    ) -> None:
        profile = await _create_profile(db_session, org, {"shifts": "view"})
        resp = await client.patch(
            _assign_url(org.id, admin_user.id),
            headers=admin_headers,
            json={"access_profile_id": str(profile.id)},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_profile_holder_cannot_assign_or_clear_own(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
        employee_user: User,
    ) -> None:
        headers, member = await _make_holder(db_session, client, org, {"members": "manage"})
        for target in (member.user_id, employee_user.id):
            resp = await client.patch(
                _assign_url(org.id, target),
                headers=headers,
                json={"access_profile_id": None},
            )
            assert resp.status_code == 403

    async def test_foreign_or_deleted_profile_is_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        other_org: Organization,
        owner_headers: dict[str, str],
        employee_member: OrganizationMember,
        employee_user: User,
    ) -> None:
        foreign = await _create_profile(db_session, other_org, {"shifts": "view"})
        deleted = await _create_profile(db_session, org, {"shifts": "view"})
        deleted.is_deleted = True
        await db_session.commit()
        for profile_id in (foreign.id, deleted.id, uuid.uuid4()):
            resp = await client.patch(
                _assign_url(org.id, employee_user.id),
                headers=owner_headers,
                json={"access_profile_id": str(profile_id)},
            )
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "ACCESS_PROFILE_NOT_FOUND"

        resp = await client.patch(
            _assign_url(org.id, uuid.uuid4()),
            headers=owner_headers,
            json={"access_profile_id": None},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "MEMBER_NOT_FOUND"

    async def test_super_admin_can_assign(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        super_admin_headers: dict[str, str],
        employee_member: OrganizationMember,
        employee_user: User,
    ) -> None:
        profile = await _create_profile(db_session, org, {"shifts": "view"})
        resp = await client.patch(
            _assign_url(org.id, employee_user.id),
            headers=super_admin_headers,
            json={"access_profile_id": str(profile.id)},
        )
        assert resp.status_code == 200

    async def test_role_change_to_admin_clears_profile(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner_headers: dict[str, str],
    ) -> None:
        _, member = await _make_holder(db_session, client, org, {"shifts": "view"})
        role_url = f"/api/v1/organizations/{org.id}/members/{member.user_id}/role"
        resp = await client.patch(role_url, headers=owner_headers, json={"role": "admin"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["access_profile"] is None
        await db_session.refresh(member)
        assert member.access_profile_id is None

        resp = await client.patch(role_url, headers=owner_headers, json={"role": "employee"})
        assert resp.status_code == 200
        assert resp.json()["data"]["access_profile"] is None

    async def test_create_member_with_profile(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        other_org: Organization,
        owner_headers: dict[str, str],
    ) -> None:
        profile = await _create_profile(db_session, org, {"payroll": "view"}, name="Бухгалтер")
        url = f"/api/v1/organizations/{org.id}/members"
        resp = await client.post(
            url,
            headers=owner_headers,
            json={"name": "Бух", "login": "buh_one", "access_profile_id": str(profile.id)},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["member"]["access_profile"] == {
            "id": str(profile.id),
            "name": "Бухгалтер",
        }

        resp = await client.post(
            url,
            headers=owner_headers,
            json={
                "name": "Админ",
                "login": "adm_one",
                "role": "admin",
                "access_profile_id": str(profile.id),
            },
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_REQUIRES_EMPLOYEE"

        foreign = await _create_profile(db_session, other_org, {"payroll": "view"})
        resp = await client.post(
            url,
            headers=owner_headers,
            json={"name": "Чужой", "login": "foreign_one", "access_profile_id": str(foreign.id)},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ACCESS_PROFILE_NOT_FOUND"


# ─── Матрица прав по разделам каталога ───────────────────────────────────────


@dataclass(frozen=True)
class Call:
    method: str
    path: str  # `{org}` подставляется id организации
    json: dict[str, Any] | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SectionCase:
    section: str
    read: Call | None
    # Чтение раздела закрыто для employee без профиля (иначе оно member-level
    # и «без раздела» проверяется на мутации).
    read_gated: bool
    mutation: Call | None
    # Статус успешной мутации обладателем `manage`.
    mutation_ok: int | None = None


_RANDOM = "00000000-0000-0000-0000-000000000001"

SECTION_CASES: list[SectionCase] = [
    SectionCase(
        "shifts",
        Call("GET", "/organizations/{org}/shifts"),
        True,
        Call("DELETE", f"/organizations/{{org}}/shifts/{_RANDOM}"),
        404,  # guard пройден, смены нет
    ),
    SectionCase(
        "overtime",
        Call("GET", "/organizations/{org}/overtime-requests"),
        True,
        Call(
            "PATCH",
            f"/organizations/{{org}}/overtime-requests/{_RANDOM}",
            {"status": "approved"},
        ),
        404,
    ),
    SectionCase(
        "schedules",
        Call("GET", "/organizations/{org}/work-schedules"),
        True,
        Call("DELETE", f"/organizations/{{org}}/work-schedules/{_RANDOM}"),
        404,
    ),
    SectionCase(
        "members",
        Call("GET", "/organizations/{org}/access-profiles"),
        True,
        Call("POST", "/organizations/{org}/members", {"name": "Новенький", "login": "new_hire"}),
        201,
    ),
    SectionCase(
        "roles",
        Call("GET", "/organizations/{org}/roles"),
        False,
        Call("POST", "/organizations/{org}/roles", {"name": "Бариста"}),
        201,
    ),
    SectionCase(
        "checklist_instances",
        Call("GET", "/organizations/{org}/checklist-instances"),
        True,
        None,
    ),
    SectionCase(
        "checklist_templates",
        Call("GET", "/organizations/{org}/checklist-templates"),
        True,
        Call(
            "POST",
            "/organizations/{org}/checklist-templates",
            {"name": "Открытие", "type": "shift_start"},
        ),
        201,
    ),
    SectionCase(
        "tests",
        Call("GET", "/organizations/{org}/test-templates"),
        True,
        Call("DELETE", f"/organizations/{{org}}/test-templates/{_RANDOM}"),
        404,
    ),
    SectionCase(
        "payroll",
        Call("GET", "/organizations/{org}/payroll"),
        True,
        Call("POST", "/organizations/{org}/adjustment-categories", {"name": "Премия"}),
        201,
    ),
    SectionCase(
        "penalties",
        Call("GET", "/organizations/{org}/penalty-templates"),
        True,
        Call(
            "POST",
            "/organizations/{org}/penalty-templates",
            {"reason": "Опоздание", "amount_minor": 10000},
        ),
        201,
    ),
    SectionCase(
        "stats",
        Call("GET", "/organizations/{org}/stats", params={"period": "day"}),
        True,
        None,
    ),
    SectionCase(
        "knowledge",
        Call("GET", "/organizations/{org}/knowledge/nodes"),
        False,
        Call(
            "POST",
            "/organizations/{org}/knowledge/nodes",
            {"kind": "section", "title": "Регламенты"},
        ),
        201,
    ),
    SectionCase(
        "work_locations",
        Call("GET", "/organizations/{org}/locations"),
        False,
        Call(
            "POST",
            "/organizations/{org}/locations",
            {"name": "Центр", "latitude": 55.75, "longitude": 37.61},
        ),
        201,
    ),
    SectionCase(
        "invites",
        Call("GET", "/organizations/{org}"),
        False,
        Call("POST", "/organizations/{org}/rotate-invite"),
        200,
    ),
    SectionCase(
        "settings",
        Call("GET", "/organizations/{org}/settings"),
        True,
        Call("PATCH", "/organizations/{org}/settings", {"max_pauses_per_shift": 3}),
        200,
    ),
    SectionCase(
        "audit",
        Call("GET", "/organizations/{org}/audit-logs"),
        True,
        None,
    ),
]


def test_matrix_covers_every_catalog_section() -> None:
    assert [c.section for c in SECTION_CASES] == [s.code for s in ACCESS_SECTIONS]
    for case in SECTION_CASES:
        has_manage = "manage" in next(s for s in ACCESS_SECTIONS if s.code == case.section).levels
        assert (case.mutation is not None) == has_manage, case.section


async def _call(
    client: AsyncClient, call: Call, org: Organization, headers: dict[str, str]
) -> Any:
    return await client.request(
        call.method,
        "/api/v1" + call.path.format(org=org.id),
        headers=headers,
        json=call.json,
        params=call.params,
    )


def _other_section(section: str) -> str:
    return "audit" if section != "audit" else "stats"


@pytest.mark.parametrize("case", SECTION_CASES, ids=lambda c: c.section)
class TestPermissionMatrix:
    async def test_view_reads_and_cannot_mutate(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        case: SectionCase,
    ) -> None:
        headers, _ = await _make_holder(db_session, client, org, {case.section: "view"})
        if case.read is not None:
            resp = await _call(client, case.read, org, headers)
            assert resp.status_code == 200, resp.text
        if case.mutation is not None:
            resp = await _call(client, case.mutation, org, headers)
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "FORBIDDEN"
            assert "управление" in resp.json()["error"]["message"]

    async def test_manage_mutates(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        case: SectionCase,
    ) -> None:
        if case.mutation is None:
            pytest.skip("раздел только для чтения")
        headers, _ = await _make_holder(db_session, client, org, {case.section: "manage"})
        resp = await _call(client, case.mutation, org, headers)
        assert resp.status_code == case.mutation_ok, resp.text
        if case.read is not None:
            resp = await _call(client, case.read, org, headers)
            assert resp.status_code == 200, resp.text

    async def test_without_section_forbidden(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        case: SectionCase,
    ) -> None:
        headers, _ = await _make_holder(
            db_session, client, org, {_other_section(case.section): "view"}
        )
        if case.read is not None and case.read_gated:
            resp = await _call(client, case.read, org, headers)
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "FORBIDDEN"
        if case.mutation is not None:
            resp = await _call(client, case.mutation, org, headers)
            assert resp.status_code == 403, resp.text

    async def test_employee_without_profile_forbidden(
        self,
        client: AsyncClient,
        org: Organization,
        employee_headers: dict[str, str],
        case: SectionCase,
    ) -> None:
        if case.read is not None and case.read_gated:
            resp = await _call(client, case.read, org, employee_headers)
            assert resp.status_code == 403, resp.text
        if case.mutation is not None:
            resp = await _call(client, case.mutation, org, employee_headers)
            assert resp.status_code == 403, resp.text


class TestDeletedProfileGrantsNothing:
    async def test_soft_deleted_profile_has_no_effect(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
    ) -> None:
        headers, member = await _make_holder(db_session, client, org, {"stats": "view"})
        profile = await db_session.get(OrganizationAccessProfile, member.access_profile_id)
        assert profile is not None
        profile.is_deleted = True
        await db_session.commit()
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/stats", headers=headers, params={"period": "day"}
        )
        assert resp.status_code == 403


class TestCurrentRateVisibility:
    async def test_current_rate_only_with_payroll_view(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner_headers: dict[str, str],
        employee_member: OrganizationMember,
    ) -> None:
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/members/{employee_member.id}/rates",
            headers=owner_headers,
            json={
                "rate_amount_minor": 30000,
                "rate_type": "hourly",
                "effective_from": "2026-01-01T00:00:00Z",
            },
        )
        assert resp.status_code == 201, resp.text

        def rate_of(resp: Any) -> Any:
            items = resp.json()["data"]["items"]
            return next(m for m in items if m["id"] == str(employee_member.id))["current_rate"]

        members_url = f"/api/v1/organizations/{org.id}/members"
        payroll_headers, _ = await _make_holder(db_session, client, org, {"payroll": "view"})
        resp = await client.get(members_url, headers=payroll_headers)
        assert rate_of(resp) is not None

        members_headers, _ = await _make_holder(db_session, client, org, {"members": "manage"})
        resp = await client.get(members_url, headers=members_headers)
        assert rate_of(resp) is None


# ─── Эскалация ────────────────────────────────────────────────────────────────


class TestEscalation:
    async def test_members_manage_cannot_create_admin_or_pass_profile(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
    ) -> None:
        headers, _ = await _make_holder(db_session, client, org, {"members": "manage"})
        profile = await _create_profile(db_session, org, {"audit": "view"})
        url = f"/api/v1/organizations/{org.id}/members"

        resp = await client.post(
            url, headers=headers, json={"name": "Админ", "login": "adm_x", "role": "admin"}
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

        resp = await client.post(
            url,
            headers=headers,
            json={"name": "Сотр", "login": "emp_x", "access_profile_id": str(profile.id)},
        )
        assert resp.status_code == 403

        resp = await client.post(url, headers=headers, json={"name": "Сотр", "login": "emp_y"})
        assert resp.status_code == 201
        assert resp.json()["data"]["member"]["role"] == "employee"
        assert resp.json()["data"]["member"]["access_profile"] is None

    async def test_members_manage_cannot_touch_admins(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        admin_member: OrganizationMember,
        admin_user: User,
        employee_member: OrganizationMember,
        employee_user: User,
    ) -> None:
        headers, _ = await _make_holder(db_session, client, org, {"members": "manage"})
        base = f"/api/v1/organizations/{org.id}/members"
        role_resp = await client.post(
            f"/api/v1/organizations/{org.id}/roles",
            headers=await _login(client, "ap-owner@example.com"),
            json={"name": "Кассир"},
        )
        role_id = role_resp.json()["data"]["id"]

        for method, suffix, body in (
            ("PATCH", "", {"display_name": "Хакер"}),
            ("POST", "/reset-password", {}),
            ("PATCH", "/custom-role", {"role_id": role_id}),
            ("DELETE", "", None),
        ):
            resp = await client.request(
                method, f"{base}/{admin_user.id}{suffix}", headers=headers, json=body
            )
            assert resp.status_code == 403, (method, suffix, resp.text)

        # Системную роль меняет только полный админ — даже у employee.
        resp = await client.patch(
            f"{base}/{employee_user.id}/role", headers=headers, json={"role": "admin"}
        )
        assert resp.status_code == 403

        # С участниками-employee — можно.
        resp = await client.patch(
            f"{base}/{employee_user.id}", headers=headers, json={"display_name": "Тёма"}
        )
        assert resp.status_code == 200, resp.text
        resp = await client.patch(
            f"{base}/{employee_user.id}/custom-role", headers=headers, json={"role_id": role_id}
        )
        assert resp.status_code == 200, resp.text
        resp = await client.delete(f"{base}/{employee_user.id}", headers=headers)
        assert resp.status_code == 200, resp.text

    async def test_profile_holder_cannot_manage_billing(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
    ) -> None:
        headers, _ = await _make_holder(db_session, client, org, full_permissions())
        resp = await client.delete(f"/api/v1/organizations/{org.id}", headers=headers)
        assert resp.status_code == 403
        resp = await client.get(f"/api/v1/organizations/{org.id}/subscription", headers=headers)
        assert resp.status_code == 403


# ─── my_permissions / my_access_profile ───────────────────────────────────────


class TestMyPermissions:
    async def test_owner_admin_employee(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner_headers: dict[str, str],
        admin_headers: dict[str, str],
        employee_headers: dict[str, str],
    ) -> None:
        full = full_permissions()
        for headers in (owner_headers, admin_headers):
            resp = await client.get("/api/v1/organizations", headers=headers)
            item = resp.json()["data"]["items"][0]
            assert item["my_permissions"] == full
            assert item["my_access_profile"] is None

        resp = await client.get("/api/v1/organizations", headers=employee_headers)
        item = resp.json()["data"]["items"][0]
        assert item["my_permissions"] == {}
        assert item["my_access_profile"] is None

        holder_headers, member = await _make_holder(
            db_session, client, org, {"payroll": "view", "penalties": "manage"}
        )
        resp = await client.get("/api/v1/organizations", headers=holder_headers)
        item = resp.json()["data"]["items"][0]
        assert item["my_permissions"] == {"payroll": "view", "penalties": "manage"}
        assert item["my_access_profile"]["id"] == str(member.access_profile_id)

        resp = await client.get(f"/api/v1/organizations/{org.id}", headers=holder_headers)
        assert resp.json()["data"]["my_permissions"] == {"payroll": "view", "penalties": "manage"}
        assert resp.json()["data"]["my_access_profile"]["id"] == str(member.access_profile_id)

    async def test_super_admin_outside_membership(
        self,
        client: AsyncClient,
        org: Organization,
        super_admin_headers: dict[str, str],
    ) -> None:
        resp = await client.get("/api/v1/organizations/all", headers=super_admin_headers)
        item = next(o for o in resp.json()["data"]["items"] if o["id"] == str(org.id))
        assert item["my_role"] is None
        assert item["my_permissions"] == full_permissions()
        assert item["my_access_profile"] is None

        resp = await client.get(f"/api/v1/organizations/{org.id}", headers=super_admin_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["my_permissions"] == full_permissions()


class TestGetOrganizationRequiresMembership:
    async def test_non_member_forbidden(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
    ) -> None:
        await _create_user(db_session, "stranger@example.com", "Посторонний")
        headers = await _login(client, "stranger@example.com")
        resp = await client.get(f"/api/v1/organizations/{org.id}", headers=headers)
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"
        assert "invite_code" not in resp.text

    async def test_member_allowed(
        self,
        client: AsyncClient,
        org: Organization,
        employee_headers: dict[str, str],
    ) -> None:
        resp = await client.get(f"/api/v1/organizations/{org.id}", headers=employee_headers)
        assert resp.status_code == 200


# ─── allow_super_admin=False ──────────────────────────────────────────────────


class TestSuperAdminExceptions:
    async def test_super_admin_still_excluded_from_operational_tools(
        self,
        client: AsyncClient,
        org: Organization,
        super_admin_headers: dict[str, str],
    ) -> None:
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/adjustment-categories",
            headers=super_admin_headers,
            json={"name": "Премия"},
        )
        assert resp.status_code == 403
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/adjustments", headers=super_admin_headers
        )
        assert resp.status_code == 403
        resp = await client.delete(
            f"/api/v1/organizations/{org.id}/shifts/{_RANDOM}", headers=super_admin_headers
        )
        assert resp.status_code == 403

    async def test_profile_holder_passes_operational_tools(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
    ) -> None:
        headers, _ = await _make_holder(db_session, client, org, {"payroll": "view"})
        resp = await client.get(f"/api/v1/organizations/{org.id}/adjustments", headers=headers)
        assert resp.status_code == 200, resp.text


# ─── Аудит ────────────────────────────────────────────────────────────────────


class TestAudit:
    async def test_profile_lifecycle_and_assignment_audited(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        org: Organization,
        owner: User,
        owner_headers: dict[str, str],
        employee_member: OrganizationMember,
        employee_user: User,
    ) -> None:
        resp = await client.post(
            _profiles_url(org.id),
            headers=owner_headers,
            json={"name": "Контролёр", "permissions": {"shifts": "view"}},
        )
        profile_id = resp.json()["data"]["id"]
        await client.patch(
            _profiles_url(org.id, profile_id),
            headers=owner_headers,
            json={"permissions": {"shifts": "manage"}},
        )
        await client.patch(
            _assign_url(org.id, employee_user.id),
            headers=owner_headers,
            json={"access_profile_id": profile_id},
        )
        await client.delete(_profiles_url(org.id, profile_id), headers=owner_headers)
        await client.post(f"{_profiles_url(org.id, profile_id)}/restore", headers=owner_headers)
        await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=owner_headers,
            json={"name": "Новый", "login": "audit_new", "access_profile_id": profile_id},
        )

        entries = await _audit_actions(client, org.id, owner_headers)
        by_action: dict[str, dict[str, Any]] = {}
        for e in entries:
            by_action.setdefault(e["action"], e)
        for action in (
            "access_profile.create",
            "access_profile.update",
            "access_profile.delete",
            "access_profile.restore",
            "member.access_profile_update",
            "member.create",
        ):
            assert action in by_action, action
            assert by_action[action]["actor_user_id"] == str(owner.id)

        assert by_action["access_profile.create"]["resource_type"] == "access_profile"
        assert by_action["access_profile.update"]["summary"]["changed_fields"] == ["permissions"]
        assert by_action["access_profile.update"]["summary"]["permissions"] == {"shifts": "manage"}
        assert by_action["access_profile.delete"]["summary"]["detached_members"] == 1
        assign = by_action["member.access_profile_update"]["summary"]
        assert assign == {
            "user_id": str(employee_user.id),
            "access_profile_id": profile_id,
            "access_profile_name": "Контролёр",
        }
        assert by_action["member.create"]["summary"]["access_profile_id"] == profile_id

        members = (
            await db_session.execute(
                select(OrganizationMember).where(OrganizationMember.organization_id == org.id)
            )
        ).scalars()
        assert any(str(m.access_profile_id) == profile_id for m in members)
