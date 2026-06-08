# tests/test_admin.py
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User, UserRole


# ─── helpers ────────────────────────────────────────────────────────────────
async def _make_user(
    session: AsyncSession,
    email: str,
    *,
    name: str = "User",
    verified: bool = True,
    role: UserRole = UserRole.user,
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("Test1234"),
        name=name,
        is_verified=verified,
        role=role,
    )
    session.add(user)
    await session.commit()
    return user


async def _make_org(
    session: AsyncSession,
    owner_id: uuid.UUID,
    *,
    name: str = "Org",
    is_deleted: bool = False,
    with_settings: bool = True,
) -> Organization:
    org = Organization(id=uuid.uuid4(), name=name, owner_id=owner_id, is_deleted=is_deleted)
    session.add(org)
    await session.flush()
    if with_settings:
        session.add(
            OrganizationSettings(
                organization_id=org.id, geo_check_enabled=False, auto_finish_hours=16,
            )
        )
    await session.commit()
    return org


async def _add_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: MemberRole = MemberRole.employee,
) -> OrganizationMember:
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=role)
    session.add(member)
    await session.commit()
    return member


async def _add_shift(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    started_at: datetime,
    status: ShiftStatus = ShiftStatus.finished,
    organization_id: uuid.UUID | None = None,
) -> Shift:
    shift = Shift(
        id=uuid.uuid4(),
        user_id=user_id,
        organization_id=organization_id,
        started_at=started_at,
        status=status,
    )
    session.add(shift)
    await session.commit()
    return shift


# ─── Block C: доступ только super_admin ─────────────────────────────────────
class TestAdminAuth:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/v1/admin/users"),
            ("get", "/api/v1/admin/organizations"),
            ("get", "/api/v1/admin/stats"),
        ],
    )
    async def test_regular_user_forbidden(
        self, client: AsyncClient, auth_headers, method, path,
    ):
        response = await getattr(client, method)(path, headers=auth_headers)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "SUPER_ADMIN_REQUIRED"

    async def test_unauthenticated_forbidden(self, client: AsyncClient):
        response = await client.get("/api/v1/admin/users")
        # без токена HTTPBearer отклоняет запрос (401 нет токена / 403 нет прав)
        assert response.status_code in (401, 403)

    async def test_super_admin_allowed(
        self, client: AsyncClient, super_admin_headers,
    ):
        response = await client.get("/api/v1/admin/users", headers=super_admin_headers)
        assert response.status_code == 200


# ─── Block C1: пользователи ─────────────────────────────────────────────────
class TestAdminUsers:
    async def test_list_envelope_and_total(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        for i in range(4):
            await _make_user(db_session, f"u{i}@example.com")
        response = await client.get(
            "/api/v1/admin/users", headers=super_admin_headers, params={"limit": 2, "offset": 0},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        # 4 созданных + сам super_admin
        assert data["total"] == 5
        assert data["limit"] == 2
        assert data["offset"] == 0
        assert len(data["items"]) == 2
        assert {"id", "email", "name", "phone", "is_verified", "role", "created_at"} <= set(
            data["items"][0].keys()
        )

    async def test_search_by_email(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        await _make_user(db_session, "needle@example.com", name="Needle")
        await _make_user(db_session, "other@example.com", name="Other")
        response = await client.get(
            "/api/v1/admin/users", headers=super_admin_headers, params={"search": "needle"},
        )
        items = response.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["email"] == "needle@example.com"

    async def test_filter_role(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        await _make_user(db_session, "plain@example.com")
        response = await client.get(
            "/api/v1/admin/users", headers=super_admin_headers, params={"role": "super_admin"},
        )
        items = response.json()["data"]["items"]
        assert all(u["role"] == "super_admin" for u in items)
        assert any(u["email"] == "admin@example.com" for u in items)

    async def test_filter_is_verified(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        await _make_user(db_session, "unverified@example.com", verified=False)
        response = await client.get(
            "/api/v1/admin/users", headers=super_admin_headers, params={"is_verified": "false"},
        )
        items = response.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["email"] == "unverified@example.com"

    async def test_sort_email_asc(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        await _make_user(db_session, "zeta@example.com")
        await _make_user(db_session, "alpha@example.com")
        response = await client.get(
            "/api/v1/admin/users",
            headers=super_admin_headers,
            params={"sort": "email", "order": "asc"},
        )
        emails = [u["email"] for u in response.json()["data"]["items"]]
        assert emails == sorted(emails)

    async def test_user_detail_aggregates(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        target = await _make_user(db_session, "target@example.com")
        owner_of_other = await _make_user(db_session, "owner2@example.com")
        # target владеет одной организацией
        await _make_org(db_session, target.id, name="Owned")
        # target состоит в другой организации
        other_org = await _make_org(db_session, owner_of_other.id, name="Other")
        await _add_member(db_session, other_org.id, target.id)
        # две смены target
        now = datetime.now(UTC)
        await _add_shift(db_session, target.id, started_at=now)
        await _add_shift(db_session, target.id, started_at=now - timedelta(days=1))

        response = await client.get(
            f"/api/v1/admin/users/{target.id}", headers=super_admin_headers,
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["owned_organizations_count"] == 1
        assert data["member_organizations_count"] == 1
        assert data["shifts_count"] == 2

    async def test_user_detail_404(
        self, client: AsyncClient, super_admin_user, super_admin_headers,
    ):
        response = await client.get(
            f"/api/v1/admin/users/{uuid.uuid4()}", headers=super_admin_headers,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "USER_NOT_FOUND"

    async def test_promote_user(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        target = await _make_user(db_session, "promote@example.com")
        response = await client.patch(
            f"/api/v1/admin/users/{target.id}/role",
            headers=super_admin_headers,
            json={"role": "super_admin"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["role"] == "super_admin"

    async def test_cannot_demote_self(
        self, client: AsyncClient, super_admin_user, super_admin_headers,
    ):
        response = await client.patch(
            f"/api/v1/admin/users/{super_admin_user.id}/role",
            headers=super_admin_headers,
            json={"role": "user"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "CANNOT_DEMOTE_SELF"

    async def test_self_to_super_admin_noop_ok(
        self, client: AsyncClient, super_admin_user, super_admin_headers,
    ):
        response = await client.patch(
            f"/api/v1/admin/users/{super_admin_user.id}/role",
            headers=super_admin_headers,
            json={"role": "super_admin"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["role"] == "super_admin"

    async def test_update_role_404(
        self, client: AsyncClient, super_admin_user, super_admin_headers,
    ):
        response = await client.patch(
            f"/api/v1/admin/users/{uuid.uuid4()}/role",
            headers=super_admin_headers,
            json={"role": "user"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "USER_NOT_FOUND"

    async def test_update_role_invalid_value(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        target = await _make_user(db_session, "bad@example.com")
        response = await client.patch(
            f"/api/v1/admin/users/{target.id}/role",
            headers=super_admin_headers,
            json={"role": "banana"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# ─── Block C2: обзор организаций ─────────────────────────────────────────────
class TestAdminOrganizations:
    async def test_overview_fields_and_total(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        owner = await _make_user(db_session, "orgowner@example.com")
        org = await _make_org(db_session, owner.id, name="Visible")
        member = await _make_user(db_session, "m1@example.com")
        await _add_member(db_session, org.id, member.id)

        response = await client.get(
            "/api/v1/admin/organizations", headers=super_admin_headers,
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        item = data["items"][0]
        assert item["owner_email"] == "orgowner@example.com"
        assert item["member_count"] == 1
        assert item["is_deleted"] is False

    async def test_filter_is_deleted(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        owner = await _make_user(db_session, "o@example.com")
        await _make_org(db_session, owner.id, name="Active")
        await _make_org(db_session, owner.id, name="Trashed", is_deleted=True)

        resp_deleted = await client.get(
            "/api/v1/admin/organizations",
            headers=super_admin_headers,
            params={"is_deleted": "true"},
        )
        items = resp_deleted.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["name"] == "Trashed"

    async def test_search_by_name(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        owner = await _make_user(db_session, "o2@example.com")
        await _make_org(db_session, owner.id, name="Pizzeria")
        await _make_org(db_session, owner.id, name="Bakery")
        response = await client.get(
            "/api/v1/admin/organizations",
            headers=super_admin_headers,
            params={"search": "pizz"},
        )
        items = response.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["name"] == "Pizzeria"


# ─── Block C3: статистика ────────────────────────────────────────────────────
class TestAdminStats:
    async def test_stats_counts(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_user, super_admin_headers,
    ):
        # пользователи: super_admin (verified) + 1 verified + 1 unverified
        u1 = await _make_user(db_session, "v1@example.com", verified=True)
        await _make_user(db_session, "v2@example.com", verified=False)
        # организации: 1 активная + 1 удалённая
        await _make_org(db_session, u1.id, name="Live", with_settings=False)
        await _make_org(db_session, u1.id, name="Dead", is_deleted=True, with_settings=False)
        # смены: 1 активная сегодня + 1 старая (30 дней назад)
        now = datetime.now(UTC)
        await _add_shift(db_session, u1.id, started_at=now, status=ShiftStatus.active)
        await _add_shift(
            db_session, u1.id, started_at=now - timedelta(days=30), status=ShiftStatus.finished,
        )

        response = await client.get("/api/v1/admin/stats", headers=super_admin_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["users_total"] == 3
        assert data["users_verified"] == 2
        assert data["organizations_total"] == 2
        assert data["organizations_active"] == 1
        assert data["shifts_active"] == 1
        assert data["shifts_today"] == 1
        assert data["shifts_week"] == 1


# ─── Block C2: сквозной доступ super_admin к org-ресурсам ────────────────────
class TestSuperAdminPassthrough:
    @pytest.fixture
    async def foreign_org(
        self, db_session: AsyncSession, verified_user,
    ) -> Organization:
        """Организация, которой владеет обычный пользователь (не super_admin)."""
        org = await _make_org(db_session, verified_user.id, name="Foreign")
        member = await _make_user(db_session, "emp@example.com")
        await _add_member(db_session, org.id, member.id)
        return org

    @pytest.mark.parametrize(
        "suffix",
        ["/members", "/settings", "/roles", "/locations", "/checklist-templates"],
    )
    async def test_super_admin_can_read_foreign_org(
        self,
        client: AsyncClient,
        super_admin_user,
        super_admin_headers,
        foreign_org,
        suffix,
    ):
        response = await client.get(
            f"/api/v1/organizations/{foreign_org.id}{suffix}", headers=super_admin_headers,
        )
        assert response.status_code == 200, response.text

    async def test_non_member_still_forbidden(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        foreign_org,
    ):
        await _make_user(db_session, "stranger@example.com")
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "stranger@example.com", "password": "Test1234"},
        )
        token = login.json()["data"]["access_token"]
        response = await client.get(
            f"/api/v1/organizations/{foreign_org.id}/members",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"


# ─── Block B: сортировка списков смен ────────────────────────────────────────
class TestShiftSort:
    async def test_personal_shifts_sort_started_at(
        self, client: AsyncClient, db_session: AsyncSession, verified_user, auth_headers,
    ):
        base = datetime.now(UTC)
        for hours in (3, 1, 2):
            await _add_shift(
                db_session, verified_user.id, started_at=base - timedelta(hours=hours),
            )

        asc = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"sort": "started_at", "order": "asc"},
        )
        starts_asc = [s["started_at"] for s in asc.json()["data"]["items"]]
        assert starts_asc == sorted(starts_asc)

        desc = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"sort": "started_at", "order": "desc"},
        )
        starts_desc = [s["started_at"] for s in desc.json()["data"]["items"]]
        assert starts_desc == sorted(starts_desc, reverse=True)
