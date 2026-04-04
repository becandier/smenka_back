# tests/test_shifts.py
from httpx import AsyncClient


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
