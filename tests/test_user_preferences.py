"""Фича table_column_settings: личные настройки пользователя /users/me/preferences."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.subscription import Subscription
from src.app.models.user import User
from src.app.models.user_preference import UserPreference
from src.app.schemas.user_preference import PREFERENCE_VALUE_MAX_BYTES, serialized_size
from src.app.services.entitlements import GRACE_DAYS
from src.app.services.user_preference import PREFERENCES_LIMIT

BASE = "/api/v1/users/me/preferences"
VALUE = {"v": 1, "order": ["employee", "status"], "hidden": ["email"]}


async def _make_user(db_session: AsyncSession, email: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("Test1234"),
        name="User",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "Test1234"})
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


def _data(resp: Response) -> Any:
    assert resp.json()["error"] is None, resp.text
    return resp.json()["data"]


def _validation_fields(resp: Response) -> list[str]:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["data"] is None
    assert body["error"]["code"] == "VALIDATION_ERROR"
    validation = body["error"]["validation"]
    assert validation
    return [item["field"] for item in validation]


async def _put(client: AsyncClient, headers: dict[str, str], key: str, value: Any) -> Response:
    return await client.put(f"{BASE}/{key}", headers=headers, json={"value": value})


class TestCrud:
    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.get(BASE)
        assert resp.status_code in (401, 403)

    async def test_empty_set(self, client: AsyncClient, auth_headers: dict[str, str]):
        resp = await client.get(BASE, headers=auth_headers)
        assert resp.status_code == 200
        assert _data(resp) == {"preferences": {}}

    async def test_create_overwrite_delete(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ):
        key = "admin.table.org-shifts"
        created = await _put(client, auth_headers, key, VALUE)
        assert created.status_code == 200
        created_data = _data(created)
        assert created_data["key"] == key
        assert created_data["value"] == VALUE
        created_at = datetime.fromisoformat(created_data["updated_at"])
        assert created_at.utcoffset() == timedelta(0)

        new_value = {"v": 1, "order": ["status"], "hidden": []}
        overwritten = await _put(client, auth_headers, key, new_value)
        assert overwritten.status_code == 200
        overwritten_data = _data(overwritten)
        assert overwritten_data["value"] == new_value
        assert datetime.fromisoformat(overwritten_data["updated_at"]) >= created_at

        listed = _data(await client.get(BASE, headers=auth_headers))
        assert listed == {"preferences": {key: new_value}}
        count = await db_session.scalar(select(func.count()).select_from(UserPreference))
        assert count == 1

        deleted = await client.delete(f"{BASE}/{key}", headers=auth_headers)
        assert deleted.status_code == 200
        assert _data(deleted) == {"deleted": 1}
        assert _data(await client.get(BASE, headers=auth_headers)) == {"preferences": {}}

        again = await client.delete(f"{BASE}/{key}", headers=auth_headers)
        assert again.status_code == 200
        assert _data(again) == {"deleted": 0}

    async def test_put_replaces_value_entirely(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ):
        await _put(client, auth_headers, "admin.table.members", {"a": 1, "b": 2})
        resp = await _put(client, auth_headers, "admin.table.members", {"c": 3})
        assert _data(resp)["value"] == {"c": 3}

    async def test_prefix_filter(self, client: AsyncClient, auth_headers: dict[str, str]):
        for key in ("admin.table.org-shifts", "admin.table.members", "mobile.home", "adminx"):
            assert (await _put(client, auth_headers, key, {"k": key})).status_code == 200

        admin = _data(await client.get(BASE, headers=auth_headers, params={"prefix": "admin."}))
        assert set(admin["preferences"]) == {"admin.table.org-shifts", "admin.table.members"}
        assert admin["preferences"]["admin.table.members"] == {"k": "admin.table.members"}

        everything = _data(await client.get(BASE, headers=auth_headers))
        assert len(everything["preferences"]) == 4

        none = _data(await client.get(BASE, headers=auth_headers, params={"prefix": "zzz"}))
        assert none == {"preferences": {}}

    async def test_prefix_underscore_is_literal(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ):
        """`_` в префиксе — обычный символ, не LIKE-шаблон «любой символ»."""
        await _put(client, auth_headers, "a_b.x", {})
        await _put(client, auth_headers, "axb.x", {})
        resp = _data(await client.get(BASE, headers=auth_headers, params={"prefix": "a_b"}))
        assert set(resp["preferences"]) == {"a_b.x"}

    async def test_empty_object_value_allowed(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ):
        resp = await _put(client, auth_headers, "admin.empty", {})
        assert resp.status_code == 200
        assert _data(resp)["value"] == {}


class TestIsolation:
    async def test_other_user_cannot_see_or_change(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ):
        key = "admin.table.org-shifts"
        await _put(client, auth_headers, key, VALUE)

        await _make_user(db_session, "other@example.com")
        other = await _login(client, "other@example.com")

        assert _data(await client.get(BASE, headers=other)) == {"preferences": {}}
        assert _data(await client.delete(f"{BASE}/{key}", headers=other)) == {"deleted": 0}
        assert (await _put(client, other, key, {"mine": True})).status_code == 200

        mine = _data(await client.get(BASE, headers=auth_headers))
        assert mine == {"preferences": {key: VALUE}}
        theirs = _data(await client.get(BASE, headers=other))
        assert theirs == {"preferences": {key: {"mine": True}}}

    async def test_super_admin_sees_only_own(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        super_admin_headers: dict[str, str],
    ):
        await _put(client, auth_headers, "admin.table.users", VALUE)
        assert _data(await client.get(BASE, headers=super_admin_headers)) == {"preferences": {}}

    async def test_cascade_on_user_delete(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ):
        await _put(client, auth_headers, "admin.table.users", VALUE)
        user = await db_session.scalar(select(User).where(User.email == "test@example.com"))
        assert user is not None
        await db_session.delete(user)
        await db_session.commit()
        count = await db_session.scalar(select(func.count()).select_from(UserPreference))
        assert count == 0


class TestValidation:
    async def test_invalid_keys(self, client: AsyncClient, auth_headers: dict[str, str]):
        for bad_key in ("Admin.table", ".admin", "-admin", "admin%20x", "a" * 121, "admin:x"):
            put = await _put(client, auth_headers, bad_key, VALUE)
            assert _validation_fields(put) == ["path.key"], bad_key
            delete = await client.delete(f"{BASE}/{bad_key}", headers=auth_headers)
            assert _validation_fields(delete) == ["path.key"], bad_key

    async def test_key_max_length_ok(self, client: AsyncClient, auth_headers: dict[str, str]):
        key = "a" * 120
        resp = await _put(client, auth_headers, key, VALUE)
        assert resp.status_code == 200
        assert _data(resp)["key"] == key

    async def test_value_not_object(self, client: AsyncClient, auth_headers: dict[str, str]):
        for bad_value in ([1, 2], "str", 42, True, None):
            resp = await _put(client, auth_headers, "admin.table.x", bad_value)
            assert _validation_fields(resp) == ["body.value"], bad_value

    async def test_missing_value(self, client: AsyncClient, auth_headers: dict[str, str]):
        resp = await client.put(f"{BASE}/admin.table.x", headers=auth_headers, json={})
        assert _validation_fields(resp) == ["body.value"]

    async def test_value_too_large(self, client: AsyncClient, auth_headers: dict[str, str]):
        base = {"s": ""}
        filler = PREFERENCE_VALUE_MAX_BYTES - serialized_size(base)
        exact = {"s": "x" * filler}
        assert serialized_size(exact) == PREFERENCE_VALUE_MAX_BYTES
        ok = await _put(client, auth_headers, "admin.big", exact)
        assert ok.status_code == 200, ok.text

        too_big = {"s": "x" * (filler + 1)}
        resp = await _put(client, auth_headers, "admin.big", too_big)
        assert _validation_fields(resp) == ["body.value"]

    async def test_value_size_counts_utf8_bytes(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ):
        # 3000 кириллических символов = 6000 байт UTF-8 + обвязка: влезает;
        # 4100 символов = 8200 байт: не влезает, хотя символов меньше 8192.
        assert (await _put(client, auth_headers, "a.ru", {"s": "я" * 3000})).status_code == 200
        resp = await _put(client, auth_headers, "a.ru", {"s": "я" * 4100})
        assert _validation_fields(resp) == ["body.value"]

    async def test_invalid_prefix(self, client: AsyncClient, auth_headers: dict[str, str]):
        for bad_prefix in ("", "Admin", "a" * 121, "a b", "a%"):
            resp = await client.get(BASE, headers=auth_headers, params={"prefix": bad_prefix})
            assert _validation_fields(resp) == ["query.prefix"], bad_prefix


class TestLimit:
    async def _fill(self, db_session: AsyncSession, user: User, count: int) -> None:
        now = datetime.now(UTC)
        db_session.add_all(
            UserPreference(
                user_id=user.id, key=f"k{i:03d}", value={}, created_at=now, updated_at=now
            )
            for i in range(count)
        )
        await db_session.commit()

    async def test_new_key_over_limit_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        verified_user: User,
        db_session: AsyncSession,
    ):
        await self._fill(db_session, verified_user, PREFERENCES_LIMIT)

        resp = await _put(client, auth_headers, "admin.new", VALUE)
        assert resp.status_code == 409, resp.text
        body = resp.json()
        assert body["data"] is None
        assert body["error"]["code"] == "PREFERENCES_LIMIT_REACHED"

        overwrite = await _put(client, auth_headers, "k000", VALUE)
        assert overwrite.status_code == 200, overwrite.text
        assert _data(overwrite)["value"] == VALUE

        # Удалили одну — снова можно создать новую.
        await client.delete(f"{BASE}/k001", headers=auth_headers)
        assert (await _put(client, auth_headers, "admin.new", VALUE)).status_code == 200

    async def test_last_slot_allowed(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        verified_user: User,
        db_session: AsyncSession,
    ):
        await self._fill(db_session, verified_user, PREFERENCES_LIMIT - 1)
        assert (await _put(client, auth_headers, "admin.last", VALUE)).status_code == 200
        assert (await _put(client, auth_headers, "admin.more", VALUE)).status_code == 409

    async def test_limit_is_per_user(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        verified_user: User,
        db_session: AsyncSession,
    ):
        await self._fill(db_session, verified_user, PREFERENCES_LIMIT)
        await _make_user(db_session, "other@example.com")
        other = await _login(client, "other@example.com")
        assert (await _put(client, other, "admin.new", VALUE)).status_code == 200


class TestReadOnlyOrganization:
    async def test_member_of_suspended_org_can_write(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "ro-owner@example.com")
        org = Organization(name="ROOrg", owner_id=owner.id)
        db_session.add(org)
        await db_session.commit()
        db_session.add(OrganizationSettings(organization_id=org.id))
        db_session.add(
            Subscription(
                organization_id=org.id,
                plan_code="premium",
                status="active",
                current_period_start=datetime.now(UTC) - timedelta(days=60),
                current_period_end=datetime.now(UTC) - timedelta(days=GRACE_DAYS + 5),
            )
        )
        employee = await _make_user(db_session, "ro-emp@example.com")
        db_session.add(
            OrganizationMember(organization_id=org.id, user_id=employee.id, role=MemberRole.admin)
        )
        await db_session.commit()
        headers = await _login(client, "ro-emp@example.com")

        # Организация действительно в read-only: организационная запись блокируется.
        blocked = await client.post(
            "/api/v1/shifts/start", headers=headers, json={"organization_id": str(org.id)}
        )
        assert blocked.status_code == 402, blocked.text
        assert blocked.json()["error"]["code"] == "SUBSCRIPTION_INACTIVE"

        put = await _put(client, headers, "admin.table.org-shifts", VALUE)
        assert put.status_code == 200, put.text
        assert _data(await client.get(BASE, headers=headers)) == {
            "preferences": {"admin.table.org-shifts": VALUE}
        }
        deleted = await client.delete(f"{BASE}/admin.table.org-shifts", headers=headers)
        assert _data(deleted) == {"deleted": 1}
