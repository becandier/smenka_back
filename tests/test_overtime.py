"""Фича work_schedules: заявки на переработку (R6)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.shift import Shift, ShiftFinishReason, ShiftStatus
from src.app.models.user import User


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


async def _login_as(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "overtime-owner@example.com", "Owner")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    return await _login_as(client, "overtime-owner@example.com")


@pytest.fixture
async def employee(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "overtime-employee@example.com", "Employee")


@pytest.fixture
async def employee_headers(employee: User, client: AsyncClient) -> dict[str, str]:
    return await _login_as(client, "overtime-employee@example.com")


@pytest.fixture
async def org(db_session: AsyncSession, owner: User, employee: User) -> Organization:
    organization = Organization(name="Overtime Org", owner_id=owner.id)
    db_session.add(organization)
    await db_session.flush()
    settings = OrganizationSettings(organization_id=organization.id, overtime_request_days=7)
    db_session.add(settings)
    member = OrganizationMember(
        organization_id=organization.id, user_id=employee.id, role=MemberRole.employee
    )
    db_session.add(member)
    await db_session.commit()
    return organization


async def _make_shift(
    db_session: AsyncSession,
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    status: ShiftStatus = ShiftStatus.finished,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    scheduled_start_at: datetime | None = None,
    scheduled_end_at: datetime | None = None,
    finish_reason: ShiftFinishReason | None = ShiftFinishReason.auto_schedule,
) -> Shift:
    now = datetime.now(UTC)
    started_at = started_at or (now - timedelta(hours=9))
    shift = Shift(
        user_id=user_id,
        organization_id=org_id,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        scheduled_start_at=scheduled_start_at,
        scheduled_end_at=scheduled_end_at,
        finish_reason=finish_reason if status == ShiftStatus.finished else None,
    )
    db_session.add(shift)
    await db_session.commit()
    return shift


class TestCreateOvertimeRequest:
    async def test_success_when_fact_within_plan(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(hours=1)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=scheduled_end,
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=scheduled_end,
        )

        resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 30, "comment": "Задержался закрыть кассу"},
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["minutes"] == 30
        assert data["status"] == "pending"
        assert data["comment"] == "Задержался закрыть кассу"

    async def test_active_shift_rejected(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            status=ShiftStatus.active,
            finished_at=None,
            scheduled_end_at=datetime.now(UTC) - timedelta(hours=1),
        )
        resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "тест"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "OVERTIME_NOT_APPLICABLE"

    async def test_shift_without_schedule_rejected(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=datetime.now(UTC),
            scheduled_start_at=None,
            scheduled_end_at=None,
            finish_reason=ShiftFinishReason.manual,
        )
        resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "тест"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "OVERTIME_NOT_APPLICABLE"

    async def test_fact_exceeded_plan_rejected(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(hours=1)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=now,  # факт позже плана
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=scheduled_end,
            finish_reason=ShiftFinishReason.manual,
        )
        resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "тест"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "OVERTIME_NOT_APPLICABLE"

    async def test_period_expired_rejected(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(days=10)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=scheduled_end,
            scheduled_start_at=scheduled_end - timedelta(hours=8),
            scheduled_end_at=scheduled_end,
        )
        resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "тест"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "OVERTIME_PERIOD_EXPIRED"

    async def test_duplicate_pending_rejected(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(hours=1)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=scheduled_end,
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=scheduled_end,
        )
        await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "первая"},
        )
        resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 20, "comment": "вторая"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "OVERTIME_ALREADY_REQUESTED"

    async def test_shift_not_found_for_other_user(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        owner: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        shift = await _make_shift(
            db_session,
            user_id=owner.id,
            org_id=org.id,
            finished_at=datetime.now(UTC) - timedelta(hours=1),
            scheduled_start_at=datetime.now(UTC) - timedelta(hours=9),
            scheduled_end_at=datetime.now(UTC) - timedelta(hours=1),
        )
        resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "тест"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SHIFT_NOT_FOUND"

    async def test_resubmit_after_rejection_allowed(
        self,
        client: AsyncClient,
        employee_headers,
        owner_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(hours=1)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=scheduled_end,
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=scheduled_end,
        )
        create_resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "первая"},
        )
        request_id = create_resp.json()["data"]["id"]

        await client.patch(
            f"/api/v1/organizations/{org.id}/overtime-requests/{request_id}",
            headers=owner_headers,
            json={"status": "rejected", "review_comment": "Недостаточно оснований"},
        )

        resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 20, "comment": "вторая попытка"},
        )
        assert resp.status_code == 201


class TestDeleteOvertimeRequest:
    async def test_owner_deletes_pending_request(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(hours=1)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=scheduled_end,
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=scheduled_end,
        )
        await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "тест"},
        )

        resp = await client.delete(f"/api/v1/shifts/{shift.id}/overtime", headers=employee_headers)
        assert resp.status_code == 200
        assert resp.json()["error"] is None

        # Можно подать заново.
        resp2 = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 20, "comment": "тест2"},
        )
        assert resp2.status_code == 201

    async def test_delete_without_request_returns_not_found(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=datetime.now(UTC) - timedelta(hours=1),
            scheduled_start_at=datetime.now(UTC) - timedelta(hours=9),
            scheduled_end_at=datetime.now(UTC) - timedelta(hours=1),
        )
        resp = await client.delete(f"/api/v1/shifts/{shift.id}/overtime", headers=employee_headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "OVERTIME_REQUEST_NOT_FOUND"

    async def test_delete_reviewed_request_rejected(
        self,
        client: AsyncClient,
        employee_headers,
        owner_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(hours=1)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=scheduled_end,
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=scheduled_end,
        )
        create_resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "тест"},
        )
        request_id = create_resp.json()["data"]["id"]
        await client.patch(
            f"/api/v1/organizations/{org.id}/overtime-requests/{request_id}",
            headers=owner_headers,
            json={"status": "approved"},
        )

        resp = await client.delete(f"/api/v1/shifts/{shift.id}/overtime", headers=employee_headers)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "OVERTIME_ALREADY_REVIEWED"


class TestReviewOvertimeRequest:
    async def test_owner_approves(
        self,
        client: AsyncClient,
        employee_headers,
        owner_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(hours=1)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=scheduled_end,
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=scheduled_end,
        )
        create_resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 30, "comment": "тест"},
        )
        request_id = create_resp.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/overtime-requests/{request_id}",
            headers=owner_headers,
            json={"status": "approved", "review_comment": "Согласовано"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "approved"
        assert data["review_comment"] == "Согласовано"
        assert data["reviewed_at"] is not None

    async def test_review_twice_rejected(
        self,
        client: AsyncClient,
        employee_headers,
        owner_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(hours=1)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=scheduled_end,
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=scheduled_end,
        )
        create_resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 30, "comment": "тест"},
        )
        request_id = create_resp.json()["data"]["id"]
        await client.patch(
            f"/api/v1/organizations/{org.id}/overtime-requests/{request_id}",
            headers=owner_headers,
            json={"status": "approved"},
        )

        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/overtime-requests/{request_id}",
            headers=owner_headers,
            json={"status": "rejected"},
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "OVERTIME_ALREADY_REVIEWED"

    async def test_unknown_request_not_found(
        self,
        client: AsyncClient,
        owner_headers,
        org: Organization,
    ):
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/overtime-requests/{uuid.uuid4()}",
            headers=owner_headers,
            json={"status": "approved"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "OVERTIME_REQUEST_NOT_FOUND"

    async def test_employee_cannot_review(
        self,
        client: AsyncClient,
        employee_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        scheduled_end = now - timedelta(hours=1)
        shift = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=scheduled_end,
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=scheduled_end,
        )
        create_resp = await client.post(
            f"/api/v1/shifts/{shift.id}/overtime",
            headers=employee_headers,
            json={"minutes": 30, "comment": "тест"},
        )
        request_id = create_resp.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/overtime-requests/{request_id}",
            headers=employee_headers,
            json={"status": "approved"},
        )
        assert resp.status_code == 403


class TestListOvertimeRequests:
    async def test_owner_lists_and_filters_by_status(
        self,
        client: AsyncClient,
        employee_headers,
        owner_headers,
        employee: User,
        org: Organization,
        db_session: AsyncSession,
    ):
        now = datetime.now(UTC)
        shift1 = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            finished_at=now - timedelta(hours=1),
            scheduled_start_at=now - timedelta(hours=9),
            scheduled_end_at=now - timedelta(hours=1),
        )
        shift2 = await _make_shift(
            db_session,
            user_id=employee.id,
            org_id=org.id,
            started_at=now - timedelta(days=1, hours=9),
            finished_at=now - timedelta(days=1, hours=1),
            scheduled_start_at=now - timedelta(days=1, hours=9),
            scheduled_end_at=now - timedelta(days=1, hours=1),
        )
        resp1 = await client.post(
            f"/api/v1/shifts/{shift1.id}/overtime",
            headers=employee_headers,
            json={"minutes": 15, "comment": "первая"},
        )
        await client.post(
            f"/api/v1/shifts/{shift2.id}/overtime",
            headers=employee_headers,
            json={"minutes": 25, "comment": "вторая"},
        )
        await client.patch(
            f"/api/v1/organizations/{org.id}/overtime-requests/{resp1.json()['data']['id']}",
            headers=owner_headers,
            json={"status": "approved"},
        )

        all_resp = await client.get(
            f"/api/v1/organizations/{org.id}/overtime-requests", headers=owner_headers
        )
        assert all_resp.json()["data"]["total"] == 2

        pending_resp = await client.get(
            f"/api/v1/organizations/{org.id}/overtime-requests",
            headers=owner_headers,
            params={"status": "pending"},
        )
        assert pending_resp.json()["data"]["total"] == 1
        item = pending_resp.json()["data"]["items"][0]
        assert item["user"]["user_name"] == "Employee"
        assert item["shift"]["schedule_name"] is None or isinstance(
            item["shift"]["schedule_name"], str
        )

    async def test_employee_cannot_list(
        self, client: AsyncClient, employee_headers, org: Organization
    ):
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/overtime-requests", headers=employee_headers
        )
        assert resp.status_code == 403


class TestOrganizationResponseExposesOvertimeRequestDays:
    """Контракт-гап (addendum): employee должен видеть overtime_request_days в
    контексте org, т.к. к /settings (owner/admin) у него доступа нет — иначе
    кнопка «Добавить переработку» показывается и после истечения срока."""

    async def test_employee_sees_value_in_org_detail(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        org: Organization,
    ) -> None:
        resp = await client.get(f"/api/v1/organizations/{org.id}", headers=employee_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["overtime_request_days"] == 7

    async def test_employee_sees_value_in_org_list(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        org: Organization,
    ) -> None:
        resp = await client.get("/api/v1/organizations", headers=employee_headers)
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        target = next(i for i in items if i["id"] == str(org.id))
        assert target["overtime_request_days"] == 7

    async def test_value_reflects_settings_update_and_is_read_only(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        employee_headers: dict[str, str],
        org: Organization,
    ) -> None:
        patch_resp = await client.patch(
            f"/api/v1/organizations/{org.id}/settings",
            headers=owner_headers,
            json={"overtime_request_days": 14},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["data"]["overtime_request_days"] == 14

        detail_resp = await client.get(f"/api/v1/organizations/{org.id}", headers=employee_headers)
        assert detail_resp.json()["data"]["overtime_request_days"] == 14

        # Поле read-only в OrganizationResponse — попытка передать его через
        # PATCH /organizations/{id} (не /settings) молча игнорируется схемой.
        rename_resp = await client.patch(
            f"/api/v1/organizations/{org.id}",
            headers=owner_headers,
            json={"name": "Overtime Org Renamed", "overtime_request_days": 1},
        )
        assert rename_resp.status_code == 200
        assert rename_resp.json()["data"]["overtime_request_days"] == 14

    async def test_default_seven_when_settings_record_missing(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        owner: User,
        owner_headers: dict[str, str],
    ) -> None:
        organization = Organization(name="No Settings Org", owner_id=owner.id)
        db_session.add(organization)
        await db_session.commit()

        resp = await client.get(f"/api/v1/organizations/{organization.id}", headers=owner_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["overtime_request_days"] == 7
