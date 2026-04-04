import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import Organization, OrganizationMember, MemberRole
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.user import User


@pytest.fixture
async def second_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="employee@example.com",
        password_hash=hash_password("Test1234"),
        name="Employee User",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def second_auth_headers(second_user: User, client: AsyncClient) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "employee@example.com", "password": "Test1234"},
    )
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def organization(db_session: AsyncSession, verified_user: User) -> Organization:
    org = Organization(name="Test Org", owner_id=verified_user.id)
    db_session.add(org)
    await db_session.flush()
    settings = OrganizationSettings(organization_id=org.id)
    db_session.add(settings)
    await db_session.commit()
    return org


class TestGetSettings:
    async def test_owner_can_get_settings(
        self, client: AsyncClient, auth_headers: dict, organization: Organization,
    ):
        resp = await client.get(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["geo_check_enabled"] is False
        assert data["auto_finish_hours"] == 16
        assert data["max_pause_minutes"] is None
        assert data["max_pauses_per_shift"] is None

    async def test_member_cannot_get_settings(
        self,
        client: AsyncClient,
        second_auth_headers: dict,
        organization: Organization,
        second_user: User,
        db_session: AsyncSession,
    ):
        member = OrganizationMember(
            organization_id=organization.id,
            user_id=second_user.id,
            role=MemberRole.employee,
        )
        db_session.add(member)
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=second_auth_headers,
        )
        assert resp.status_code == 403


class TestUpdateSettings:
    async def test_owner_can_update(
        self, client: AsyncClient, auth_headers: dict, organization: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=auth_headers,
            json={
                "geo_check_enabled": True,
                "max_pause_minutes": 30,
                "max_pauses_per_shift": 3,
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["geo_check_enabled"] is True
        assert data["max_pause_minutes"] == 30
        assert data["max_pauses_per_shift"] == 3
        assert data["auto_finish_hours"] == 16  # unchanged

    async def test_non_owner_cannot_update(
        self,
        client: AsyncClient,
        second_auth_headers: dict,
        organization: Organization,
        second_user: User,
        db_session: AsyncSession,
    ):
        member = OrganizationMember(
            organization_id=organization.id,
            user_id=second_user.id,
            role=MemberRole.admin,
        )
        db_session.add(member)
        await db_session.commit()

        resp = await client.patch(
            f"/api/v1/organizations/{organization.id}/settings",
            headers=second_auth_headers,
            json={"geo_check_enabled": True},
        )
        assert resp.status_code == 403
