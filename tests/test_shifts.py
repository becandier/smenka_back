# tests/test_shifts.py
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User


class TestStartShift:
    async def test_start_shift_success(self, client: AsyncClient, auth_headers):
        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["status"] == "active"
        assert data["pauses"] == []
        assert data["worked_seconds"] >= 0
        assert data["finished_at"] is None

    async def test_start_shift_already_active(self, client: AsyncClient, auth_headers):
        await client.post("/api/v1/shifts/start", headers=auth_headers)
        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SHIFT_ALREADY_ACTIVE"

    async def test_start_shift_unauthorized(self, client: AsyncClient):
        response = await client.post("/api/v1/shifts/start")
        assert response.status_code == 401


class TestPauseShift:
    async def test_pause_active_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        response = await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "paused"
        assert len(data["pauses"]) == 1
        assert data["pauses"][0]["finished_at"] is None

    async def test_pause_already_paused(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        response = await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_NOT_ACTIVE"

    async def test_pause_finished_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        response = await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_NOT_ACTIVE"

    async def test_pause_not_own_shift(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/shifts/00000000-0000-0000-0000-000000000000/pause",
            headers=auth_headers,
        )
        assert response.status_code == 404


class TestResumeShift:
    async def test_resume_paused_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        response = await client.post(f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "active"
        assert len(data["pauses"]) == 1
        assert data["pauses"][0]["finished_at"] is not None

    async def test_resume_active_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        response = await client.post(f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_NOT_PAUSED"

    async def test_resume_not_own_shift(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/shifts/00000000-0000-0000-0000-000000000000/resume",
            headers=auth_headers,
        )
        assert response.status_code == 404


class TestFinishShift:
    async def test_finish_active_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        response = await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "finished"
        assert data["finished_at"] is not None

    async def test_finish_paused_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        response = await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "finished"
        assert data["pauses"][0]["finished_at"] is not None

    async def test_finish_already_finished(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        response = await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_ALREADY_FINISHED"

    async def test_finish_not_own_shift(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/shifts/00000000-0000-0000-0000-000000000000/finish",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_can_start_new_shift_after_finish(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)

        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 201


class TestAutoFinish:
    async def test_personal_stale_shift_never_auto_finished(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        """Персональные смены больше не авто-завершаются (work_schedules) — старая
        смена остаётся активной, новую личную смену начать нельзя (SHIFT_ALREADY_ACTIVE)."""
        from src.app.models.shift import Shift, ShiftStatus

        me_resp = await client.get("/api/v1/users/me", headers=auth_headers)
        user_id = me_resp.json()["data"]["id"]

        stale_shift = Shift(
            user_id=user_id,
            started_at=datetime.now(UTC) - timedelta(hours=100),
            status=ShiftStatus.active,
        )
        db_session.add(stale_shift)
        await db_session.commit()

        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "SHIFT_ALREADY_ACTIVE"

        list_resp = await client.get("/api/v1/shifts", headers=auth_headers)
        shifts = list_resp.json()["data"]["items"]
        assert all(s["status"] == "active" for s in shifts)


class TestListShifts:
    async def test_list_shifts_empty(self, client: AsyncClient, auth_headers):
        response = await client.get("/api/v1/shifts", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    async def test_list_shifts_with_data(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]
        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        await client.post("/api/v1/shifts/start", headers=auth_headers)

        response = await client.get("/api/v1/shifts", headers=auth_headers)
        data = response.json()["data"]
        assert data["total"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["status"] == "active"
        assert data["items"][1]["status"] == "finished"

    async def test_list_shifts_filter_by_status(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]
        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        await client.post("/api/v1/shifts/start", headers=auth_headers)

        response = await client.get(
            "/api/v1/shifts", headers=auth_headers, params={"status": "finished"}
        )
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["status"] == "finished"

    async def test_list_shifts_pagination(self, client: AsyncClient, auth_headers):
        for _ in range(3):
            start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
            shift_id = start_resp.json()["data"]["id"]
            await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)

        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"limit": 2, "offset": 0},
        )
        data = response.json()["data"]
        assert data["total"] == 3
        assert len(data["items"]) == 2
        assert data["limit"] == 2
        assert data["offset"] == 0

    async def test_list_shifts_filter_by_date(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        from src.app.models.shift import Shift, ShiftStatus

        me_resp = await client.get("/api/v1/users/me", headers=auth_headers)
        user_id = me_resp.json()["data"]["id"]

        # Create an old finished shift directly in DB
        old_shift = Shift(
            user_id=user_id,
            started_at=datetime.now(UTC) - timedelta(days=3),
            finished_at=datetime.now(UTC) - timedelta(days=3, hours=-1),
            status=ShiftStatus.finished,
        )
        db_session.add(old_shift)
        await db_session.commit()

        # Create a recent shift via API
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]
        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)

        # Filter to only recent shifts (last 24h)
        date_from = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"date_from": date_from},
        )
        data = response.json()["data"]
        assert data["total"] == 1

    async def test_list_shifts_invalid_status(self, client: AsyncClient, auth_headers):
        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"status": "invalid_value"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_STATUS"

    async def test_list_shifts_unauthorized(self, client: AsyncClient):
        response = await client.get("/api/v1/shifts")
        assert response.status_code in (401, 403)


# --- shift_history_scope: scope/organization_id у GET /shifts и GET /shifts/stats ---


@pytest.fixture
async def scope_org_owner_a(db_session: AsyncSession) -> User:
    """Отдельный владелец org A — owner != member (ADR-001), нужен только для FK."""
    user = User(
        id=uuid.uuid4(),
        email="scope-org-owner-a@example.com",
        password_hash=hash_password("Test1234"),
        name="Scope Org A Owner",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def scope_org_owner_b(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="scope-org-owner-b@example.com",
        password_hash=hash_password("Test1234"),
        name="Scope Org B Owner",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def scope_org_a(db_session: AsyncSession, scope_org_owner_a: User) -> Organization:
    org = Organization(name="Scope Org A", owner_id=scope_org_owner_a.id)
    db_session.add(org)
    await db_session.commit()
    return org


@pytest.fixture
async def scope_org_b(db_session: AsyncSession, scope_org_owner_b: User) -> Organization:
    org = Organization(name="Scope Org B", owner_id=scope_org_owner_b.id)
    db_session.add(org)
    await db_session.commit()
    return org


async def _make_shift(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    *,
    organization_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
    finished: bool = True,
) -> Shift:
    start = started_at or (datetime.now(UTC) - timedelta(hours=1))
    shift = Shift(
        user_id=user_id,
        organization_id=organization_id,
        started_at=start,
        finished_at=start + timedelta(minutes=30) if finished else None,
        status=ShiftStatus.finished if finished else ShiftStatus.active,
    )
    db_session.add(shift)
    await db_session.commit()
    return shift


async def _current_user_id(client: AsyncClient, auth_headers: dict[str, str]) -> uuid.UUID:
    me_resp = await client.get("/api/v1/users/me", headers=auth_headers)
    return uuid.UUID(me_resp.json()["data"]["id"])


class TestListShiftsScope:
    async def test_mixed_history_includes_each_organization_timezone(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
        db_session: AsyncSession,
        scope_org_a: Organization,
        scope_org_b: Organization,
    ) -> None:
        scope_org_a.timezone = "Europe/Moscow"
        scope_org_b.timezone = "Asia/Vladivostok"
        await db_session.commit()
        user_id = await _current_user_id(client, auth_headers)
        org_a_shift = await _make_shift(db_session, user_id, organization_id=scope_org_a.id)
        org_b_shift = await _make_shift(db_session, user_id, organization_id=scope_org_b.id)
        personal_shift = await _make_shift(db_session, user_id)

        timezone_queries = 0

        def on_execute(conn, cursor, statement, *args):
            nonlocal timezone_queries
            statement_lower = statement.lower()
            if "organizations" in statement_lower and "timezone" in statement_lower:
                timezone_queries += 1

        sync_engine = db_session.bind.sync_engine
        event.listen(sync_engine, "before_cursor_execute", on_execute)
        try:
            response = await client.get("/api/v1/shifts", headers=auth_headers)
        finally:
            event.remove(sync_engine, "before_cursor_execute", on_execute)

        assert response.status_code == 200
        items = {item["id"]: item for item in response.json()["data"]["items"]}
        assert items[str(org_a_shift.id)]["organization_timezone"] == "Europe/Moscow"
        assert items[str(org_b_shift.id)]["organization_timezone"] == "Asia/Vladivostok"
        assert items[str(personal_shift.id)]["organization_timezone"] is None
        assert timezone_queries == 1

    async def test_scope_omitted_returns_previous_behavior(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
    ):
        """Отсутствие `scope` = прежнее поведение: персональные и организационные
        смены вперемешку, без изменений."""
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=None)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id)

        response = await client.get("/api/v1/shifts", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2

    async def test_scope_all_explicit_same_as_omitted(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
    ):
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=None)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id)

        response = await client.get(
            "/api/v1/shifts", headers=auth_headers, params={"scope": "all"}
        )
        assert response.status_code == 200
        assert response.json()["data"]["total"] == 2

    async def test_scope_personal_only_organization_id_null(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
    ):
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=None)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id)

        response = await client.get(
            "/api/v1/shifts", headers=auth_headers, params={"scope": "personal"}
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["organization_id"] is None

    async def test_scope_organization_only_that_org(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
        scope_org_b: Organization,
    ):
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=None)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id)
        await _make_shift(db_session, user_id, organization_id=scope_org_b.id)

        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"scope": "organization", "organization_id": str(scope_org_a.id)},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["organization_id"] == str(scope_org_a.id)

    async def test_scope_combines_with_status_filter(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
    ):
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id, finished=True)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id, finished=False)

        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={
                "scope": "organization",
                "organization_id": str(scope_org_a.id),
                "status": "finished",
            },
        )
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["status"] == "finished"

    async def test_scope_combines_with_date_range(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
    ):
        user_id = await _current_user_id(client, auth_headers)
        old_start = datetime.now(UTC) - timedelta(days=3)
        recent_start = datetime.now(UTC) - timedelta(hours=1)
        await _make_shift(
            db_session, user_id, organization_id=scope_org_a.id, started_at=old_start
        )
        await _make_shift(
            db_session, user_id, organization_id=scope_org_a.id, started_at=recent_start
        )

        date_from = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={
                "scope": "organization",
                "organization_id": str(scope_org_a.id),
                "date_from": date_from,
            },
        )
        data = response.json()["data"]
        assert data["total"] == 1

    async def test_scope_pagination_total_reflects_filtered_set(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
    ):
        user_id = await _current_user_id(client, auth_headers)
        for _ in range(3):
            await _make_shift(db_session, user_id, organization_id=scope_org_a.id)
        for _ in range(2):
            await _make_shift(db_session, user_id, organization_id=None)

        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"scope": "personal", "limit": 1, "offset": 0},
        )
        data = response.json()["data"]
        assert data["total"] == 2  # общий набор персональных, не всей истории (5)
        assert len(data["items"]) == 1
        assert data["limit"] == 1
        assert data["offset"] == 0

    async def test_scope_organization_after_membership_removed(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
    ):
        """Членство не проверяется: смены исключённого сотрудника продолжают открываться."""
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id)

        member = OrganizationMember(
            organization_id=scope_org_a.id,
            user_id=user_id,
            role=MemberRole.employee,
        )
        db_session.add(member)
        await db_session.commit()
        # Сотрудника исключили из организации.
        await db_session.delete(member)
        await db_session.commit()

        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"scope": "organization", "organization_id": str(scope_org_a.id)},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1

    async def test_scope_organization_unknown_org_empty_result(
        self, client: AsyncClient, auth_headers
    ):
        """`scope=organization` для организации, где пользователь никогда не был —
        валидный запрос с пустым результатом, не ошибка (backend.md, бизнес-правило 4)."""
        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"scope": "organization", "organization_id": str(uuid.uuid4())},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0

    async def test_invalid_scope_returns_400(self, client: AsyncClient, auth_headers):
        response = await client.get(
            "/api/v1/shifts", headers=auth_headers, params={"scope": "bogus"}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_SCOPE"

    async def test_scope_organization_without_organization_id_returns_400(
        self, client: AsyncClient, auth_headers
    ):
        response = await client.get(
            "/api/v1/shifts", headers=auth_headers, params={"scope": "organization"}
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize("scope_value", ["all", "personal", None])
    async def test_organization_id_forbidden_outside_organization_scope_returns_400(
        self, client: AsyncClient, auth_headers, scope_value
    ):
        params: dict[str, str] = {"organization_id": str(uuid.uuid4())}
        if scope_value is not None:
            params["scope"] = scope_value
        response = await client.get("/api/v1/shifts", headers=auth_headers, params=params)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_organization_id_not_uuid_returns_400(self, client: AsyncClient, auth_headers):
        response = await client.get(
            "/api/v1/shifts",
            headers=auth_headers,
            params={"scope": "organization", "organization_id": "not-a-uuid"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestShiftStats:
    async def test_stats_empty(self, client: AsyncClient, auth_headers):
        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["period"] == "day"
        assert data["total_worked_seconds"] == 0
        assert data["shift_count"] == 0
        assert data["average_shift_seconds"] == 0

    async def test_stats_with_finished_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]
        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)

        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day"},
        )
        data = response.json()["data"]
        assert data["shift_count"] == 1
        assert data["total_worked_seconds"] >= 0
        assert data["average_shift_seconds"] >= 0

    async def test_stats_includes_active_shift(self, client: AsyncClient, auth_headers):
        await client.post("/api/v1/shifts/start", headers=auth_headers)

        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day"},
        )
        data = response.json()["data"]
        assert data["shift_count"] == 1

    async def test_stats_invalid_period(self, client: AsyncClient, auth_headers):
        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "year"},
        )
        assert response.status_code == 400

    async def test_stats_unauthorized(self, client: AsyncClient):
        response = await client.get("/api/v1/shifts/stats", params={"period": "day"})
        assert response.status_code in (401, 403)


class TestShiftStatsScope:
    """shift_history_scope: те же `scope`/`organization_id`, что у GET /shifts."""

    async def test_stats_scope_omitted_counts_all_shifts(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
    ):
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=None)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id)

        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"date_from": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        )
        assert response.status_code == 200
        assert response.json()["data"]["shift_count"] == 2

    async def test_stats_scope_personal_counts_only_personal(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
    ):
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=None)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id)

        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={
                "date_from": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "scope": "personal",
            },
        )
        assert response.json()["data"]["shift_count"] == 1

    async def test_stats_scope_organization_counts_only_that_org(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
        scope_org_b: Organization,
    ):
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id)
        await _make_shift(db_session, user_id, organization_id=scope_org_b.id)
        await _make_shift(db_session, user_id, organization_id=None)

        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={
                "date_from": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "scope": "organization",
                "organization_id": str(scope_org_a.id),
            },
        )
        assert response.json()["data"]["shift_count"] == 1

    async def test_stats_invalid_scope_returns_400(self, client: AsyncClient, auth_headers):
        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day", "scope": "bogus"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_SCOPE"

    async def test_stats_scope_organization_without_id_returns_400(
        self, client: AsyncClient, auth_headers
    ):
        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day", "scope": "organization"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_stats_organization_id_forbidden_at_scope_personal_returns_400(
        self, client: AsyncClient, auth_headers
    ):
        response = await client.get(
            "/api/v1/shifts/stats",
            headers=auth_headers,
            params={"period": "day", "scope": "personal", "organization_id": str(uuid.uuid4())},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestShiftHistoryScopeConsistency:
    """При одинаковых scope/organization_id/окне GET /shifts и GET /shifts/stats
    обязаны описывать одно и то же множество смен (backend.md, «Требование»)."""

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"scope": "all"},
            {"scope": "personal"},
            {"scope": "organization"},  # organization_id подставляется в тесте
        ],
    )
    async def test_list_and_stats_agree_on_same_filters(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
        scope_org_a: Organization,
        scope_org_b: Organization,
        params: dict[str, str],
    ):
        user_id = await _current_user_id(client, auth_headers)
        await _make_shift(db_session, user_id, organization_id=None)
        await _make_shift(db_session, user_id, organization_id=scope_org_a.id)
        await _make_shift(db_session, user_id, organization_id=scope_org_b.id)

        query = dict(params)
        if query.get("scope") == "organization":
            query["organization_id"] = str(scope_org_a.id)
        query["date_from"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        query["limit"] = 100

        list_resp = await client.get("/api/v1/shifts", headers=auth_headers, params=query)
        list_data = list_resp.json()["data"]

        stats_query = {k: v for k, v in query.items() if k != "limit"}
        stats_resp = await client.get(
            "/api/v1/shifts/stats", headers=auth_headers, params=stats_query
        )
        stats_data = stats_resp.json()["data"]

        assert list_data["total"] == stats_data["shift_count"]
        assert (
            sum(item["worked_seconds"] for item in list_data["items"])
            == (stats_data["total_worked_seconds"])
        )


class TestShiftLifecycle:
    async def test_full_lifecycle(self, client: AsyncClient, auth_headers):
        """Start → pause → resume → pause → finish — full cycle."""
        resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert resp.status_code == 201
        shift_id = resp.json()["data"]["id"]

        resp = await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        assert resp.json()["data"]["status"] == "paused"

        resp = await client.post(f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers)
        assert resp.json()["data"]["status"] == "active"
        assert resp.json()["data"]["pauses"][0]["finished_at"] is not None

        resp = await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        assert resp.json()["data"]["status"] == "paused"
        assert len(resp.json()["data"]["pauses"]) == 2

        resp = await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        data = resp.json()["data"]
        assert data["status"] == "finished"
        assert data["finished_at"] is not None
        assert all(p["finished_at"] is not None for p in data["pauses"])
        assert data["worked_seconds"] >= 0

    async def test_multiple_pauses_tracked(self, client: AsyncClient, auth_headers):
        """Multiple pause/resume cycles should all be tracked."""
        resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = resp.json()["data"]["id"]

        for _ in range(3):
            await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
            await client.post(f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers)

        resp = await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        assert len(resp.json()["data"]["pauses"]) == 3
