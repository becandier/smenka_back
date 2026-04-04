# tests/test_shifts.py
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


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

        response = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "paused"
        assert len(data["pauses"]) == 1
        assert data["pauses"][0]["finished_at"] is None

    async def test_pause_already_paused(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_NOT_ACTIVE"

    async def test_pause_finished_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
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
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "active"
        assert len(data["pauses"]) == 1
        assert data["pauses"][0]["finished_at"] is not None

    async def test_resume_active_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        response = await client.post(
            f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers
        )
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

        response = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "finished"
        assert data["finished_at"] is not None

    async def test_finish_paused_shift(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers)
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["status"] == "finished"
        assert data["pauses"][0]["finished_at"] is not None

    async def test_finish_already_finished(self, client: AsyncClient, auth_headers):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)
        response = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "SHIFT_ALREADY_FINISHED"

    async def test_finish_not_own_shift(self, client: AsyncClient, auth_headers):
        response = await client.post(
            "/api/v1/shifts/00000000-0000-0000-0000-000000000000/finish",
            headers=auth_headers,
        )
        assert response.status_code == 404

    async def test_can_start_new_shift_after_finish(
        self, client: AsyncClient, auth_headers
    ):
        start_resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        shift_id = start_resp.json()["data"]["id"]

        await client.post(f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers)

        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 201


class TestAutoFinish:
    async def test_stale_shift_auto_finished_on_start(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        """A shift older than 16h should be auto-finished when starting a new one."""
        from src.app.models.shift import Shift, ShiftStatus

        me_resp = await client.get("/api/v1/users/me", headers=auth_headers)
        user_id = me_resp.json()["data"]["id"]

        stale_shift = Shift(
            user_id=user_id,
            started_at=datetime.now(UTC) - timedelta(hours=17),
            status=ShiftStatus.active,
        )
        db_session.add(stale_shift)
        await db_session.commit()

        response = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert response.status_code == 201

        list_resp = await client.get("/api/v1/shifts", headers=auth_headers)
        shifts = list_resp.json()["data"]["items"]
        finished_shifts = [s for s in shifts if s["status"] == "finished"]
        assert len(finished_shifts) == 1


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

    async def test_list_shifts_filter_by_status(
        self, client: AsyncClient, auth_headers
    ):
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
            start_resp = await client.post(
                "/api/v1/shifts/start", headers=auth_headers
            )
            shift_id = start_resp.json()["data"]["id"]
            await client.post(
                f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
            )

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

    async def test_stats_includes_active_shift(
        self, client: AsyncClient, auth_headers
    ):
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
        response = await client.get(
            "/api/v1/shifts/stats", params={"period": "day"}
        )
        assert response.status_code in (401, 403)


class TestShiftLifecycle:
    async def test_full_lifecycle(self, client: AsyncClient, auth_headers):
        """Start → pause → resume → pause → finish — full cycle."""
        resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert resp.status_code == 201
        shift_id = resp.json()["data"]["id"]

        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
        assert resp.json()["data"]["status"] == "paused"

        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers
        )
        assert resp.json()["data"]["status"] == "active"
        assert resp.json()["data"]["pauses"][0]["finished_at"] is not None

        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
        )
        assert resp.json()["data"]["status"] == "paused"
        assert len(resp.json()["data"]["pauses"]) == 2

        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
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
            await client.post(
                f"/api/v1/shifts/{shift_id}/pause", headers=auth_headers
            )
            await client.post(
                f"/api/v1/shifts/{shift_id}/resume", headers=auth_headers
            )

        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=auth_headers
        )
        assert len(resp.json()["data"]["pauses"]) == 3
