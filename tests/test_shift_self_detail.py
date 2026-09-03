# tests/test_shift_self_detail.py
"""Фича shift_self_detail: GET /shifts/{shift_id} — деталь СВОЕЙ смены по id."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User


def _data(resp: Any) -> Any:
    return resp.json()["data"]


def _err(resp: Any) -> str:
    return resp.json()["error"]["code"]


# --- fixtures ------------------------------------------------------------------
@pytest.fixture
async def other_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="ssd_other@example.com",
        password_hash=hash_password("Test1234"),
        name="Other User",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="ssd_owner@example.com",
        password_hash=hash_password("Test1234"),
        name="Owner",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(
        name="Shift Self Detail Org",
        owner_id=owner.id,
        timezone="Asia/Yekaterinburg",
    )
    db_session.add(organization)
    await db_session.commit()
    return organization


@pytest.fixture
async def employee_member(
    db_session: AsyncSession,
    org: Organization,
    verified_user: User,
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id,
        user_id=verified_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return member


BASE = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)


async def _insert_shift(
    db_session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID | None = None,
    is_deleted: bool = False,
    created_by_user_id: uuid.UUID | None = None,
    edited_by_user_id: uuid.UUID | None = None,
    edited_at: datetime | None = None,
    manual_note: str | None = None,
) -> Shift:
    shift = Shift(
        user_id=user_id,
        organization_id=organization_id,
        started_at=BASE,
        finished_at=BASE + timedelta(hours=8),
        status=ShiftStatus.finished,
        is_deleted=is_deleted,
        created_by_user_id=created_by_user_id,
        edited_by_user_id=edited_by_user_id,
        edited_at=edited_at,
        manual_note=manual_note,
    )
    db_session.add(shift)
    await db_session.commit()
    await db_session.refresh(shift)
    return shift


class TestOwnShiftDetail:
    async def test_own_personal_shift_returned(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        response = await client.get(f"/api/v1/shifts/{shift_id}", headers=auth_headers)
        assert response.status_code == 200
        data = _data(response)
        assert data["id"] == shift_id
        assert data["organization_id"] is None
        assert data["organization_timezone"] is None
        assert data["status"] == "active"
        # Персональный контекст: как в GET /shifts — идентификация сотрудника null
        assert data["user_name"] is None
        assert data["user_email"] is None
        assert data["role"] is None
        assert data["custom_role_name"] is None
        assert data["checklists_summary"] is None
        assert data["created_by_name"] is None
        assert data["edited_by_name"] is None

    async def test_own_org_shift_returned(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
        verified_user: User,
        owner: User,
    ):
        """Смена в организации, где текущий пользователь — сотрудник (её владелец)."""
        shift = await _insert_shift(
            db_session,
            user_id=verified_user.id,
            organization_id=org.id,
            created_by_user_id=owner.id,
            manual_note="Забыл отметиться",
        )

        response = await client.get(f"/api/v1/shifts/{shift.id}", headers=auth_headers)
        assert response.status_code == 200
        data = _data(response)
        assert data["id"] == str(shift.id)
        assert data["organization_id"] == str(org.id)
        assert data["organization_timezone"] == "Asia/Yekaterinburg"
        response_started_at = datetime.fromisoformat(data["started_at"].replace("Z", "+00:00"))
        assert response_started_at == shift.started_at
        # Ручные поля видны сотруднику (R7)
        assert data["is_manual"] is True
        assert data["is_edited"] is False
        assert data["manual_note"] == "Забыл отметиться"
        # ...но имена админов — только орг-контекст, тут null
        assert data["created_by_name"] is None
        assert data["edited_by_name"] is None
        # Идентификация сотрудника/checklists_summary — персональный контекст
        assert data["user_name"] is None
        assert data["checklists_summary"] is None

    async def test_own_org_shift_edited_fields_visible_but_names_null(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        org: Organization,
        employee_member: OrganizationMember,
        verified_user: User,
        owner: User,
    ):
        shift = await _insert_shift(
            db_session,
            user_id=verified_user.id,
            organization_id=org.id,
            created_by_user_id=owner.id,
            edited_by_user_id=owner.id,
            edited_at=BASE + timedelta(hours=1),
            manual_note="Правка времени",
        )

        response = await client.get(f"/api/v1/shifts/{shift.id}", headers=auth_headers)
        assert response.status_code == 200
        data = _data(response)
        assert data["is_edited"] is True
        assert data["edited_at"] is not None
        assert data["edited_by_name"] is None
        assert data["created_by_name"] is None

    async def test_foreign_shift_returns_404(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        other_user: User,
    ):
        shift = await _insert_shift(db_session, user_id=other_user.id)

        response = await client.get(f"/api/v1/shifts/{shift.id}", headers=auth_headers)
        assert response.status_code == 404
        assert _err(response) == "SHIFT_NOT_FOUND"

    async def test_nonexistent_shift_returns_404(self, client: AsyncClient, auth_headers):
        response = await client.get(f"/api/v1/shifts/{uuid.uuid4()}", headers=auth_headers)
        assert response.status_code == 404
        assert _err(response) == "SHIFT_NOT_FOUND"

    async def test_soft_deleted_own_shift_returns_404(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        verified_user: User,
    ):
        shift = await _insert_shift(db_session, user_id=verified_user.id, is_deleted=True)

        response = await client.get(f"/api/v1/shifts/{shift.id}", headers=auth_headers)
        assert response.status_code == 404
        assert _err(response) == "SHIFT_NOT_FOUND"

    async def test_unauthorized(self, client: AsyncClient):
        response = await client.get(f"/api/v1/shifts/{uuid.uuid4()}")
        assert response.status_code in (401, 403)

    async def test_invalid_uuid_returns_422(self, client: AsyncClient, auth_headers):
        """Не UUID — 422 от валидации пути, а не 404 (важно отличать от /stats и /start)."""
        response = await client.get("/api/v1/shifts/not-a-uuid", headers=auth_headers)
        assert response.status_code == 422


class TestRouteOrderingRegression:
    """Новый GET /{shift_id} объявлен после статических /stats и /start — не должен
    их перехватывать."""

    async def test_stats_not_shadowed_by_shift_id_route(self, client: AsyncClient, auth_headers):
        response = await client.get(
            "/api/v1/shifts/stats", headers=auth_headers, params={"period": "day"}
        )
        assert response.status_code == 200
        data = _data(response)
        assert data["period"] == "day"
        assert data["shift_count"] == 0

    async def test_start_not_shadowed_by_shift_id_route(self, client: AsyncClient, auth_headers):
        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 201
        assert _data(response)["status"] == "active"
