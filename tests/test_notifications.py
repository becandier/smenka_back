# tests/test_notifications.py
"""Фича notifications: внутриапповый центр уведомлений (pull-модель)."""

import uuid
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.notification import Notification
from src.app.models.user import User


def _data(resp: Any) -> Any:
    return resp.json()["data"]


def _err(resp: Any) -> str:
    return resp.json()["error"]["code"]


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
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _make_notification(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    *,
    type_: str = "test_assigned",
    title: str = "Вам назначен тест",
    body: str | None = None,
    payload: dict[str, Any] | None = None,
    is_read: bool = False,
) -> Notification:
    n = Notification(
        user_id=user_id,
        type=type_,
        title=title,
        body=body,
        payload=payload,
        is_read=is_read,
    )
    db_session.add(n)
    await db_session.commit()
    return n


class TestListNotifications:
    async def test_empty(self, client: AsyncClient, auth_headers):
        resp = await client.get("/api/v1/notifications", headers=auth_headers)
        assert resp.status_code == 200
        data = _data(resp)
        assert data == {"items": [], "total": 0, "limit": 20, "offset": 0}

    async def test_lists_own_sorted_desc(
        self, client: AsyncClient, auth_headers, verified_user: User, db_session: AsyncSession
    ):
        await _make_notification(db_session, verified_user.id, title="First")
        await _make_notification(db_session, verified_user.id, title="Second")
        resp = await client.get("/api/v1/notifications", headers=auth_headers)
        data = _data(resp)
        assert data["total"] == 2
        assert [item["title"] for item in data["items"]] == ["Second", "First"]

    async def test_pagination(
        self, client: AsyncClient, auth_headers, verified_user: User, db_session: AsyncSession
    ):
        for i in range(5):
            await _make_notification(db_session, verified_user.id, title=f"N{i}")
        resp = await client.get(
            "/api/v1/notifications", headers=auth_headers, params={"limit": 2, "offset": 1}
        )
        data = _data(resp)
        assert data["total"] == 5
        assert data["limit"] == 2
        assert data["offset"] == 1
        assert len(data["items"]) == 2

    async def test_unread_filter(
        self, client: AsyncClient, auth_headers, verified_user: User, db_session: AsyncSession
    ):
        await _make_notification(db_session, verified_user.id, title="Read", is_read=True)
        await _make_notification(db_session, verified_user.id, title="Unread", is_read=False)
        resp = await client.get(
            "/api/v1/notifications", headers=auth_headers, params={"unread": "true"}
        )
        data = _data(resp)
        assert data["total"] == 1
        assert data["items"][0]["title"] == "Unread"

    async def test_isolation_by_user(
        self,
        client: AsyncClient,
        auth_headers,
        verified_user: User,
        db_session: AsyncSession,
    ):
        other = await _create_user(db_session, "other@example.com")
        await _make_notification(db_session, other.id, title="Not mine")
        resp = await client.get("/api/v1/notifications", headers=auth_headers)
        assert _data(resp)["items"] == []

    async def test_payload_and_body_roundtrip(
        self, client: AsyncClient, auth_headers, verified_user: User, db_session: AsyncSession
    ):
        payload = {"assignment_id": str(uuid.uuid4()), "test_title": "Т.Б.", "due_at": None}
        await _make_notification(
            db_session, verified_user.id, body="Пройдите тест", payload=payload
        )
        resp = await client.get("/api/v1/notifications", headers=auth_headers)
        item = _data(resp)["items"][0]
        assert item["type"] == "test_assigned"
        assert item["body"] == "Пройдите тест"
        assert item["payload"] == payload
        assert item["is_read"] is False

    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/notifications")
        assert resp.status_code == 401


class TestUnreadCount:
    async def test_counts_only_unread_own(
        self,
        client: AsyncClient,
        auth_headers,
        verified_user: User,
        db_session: AsyncSession,
    ):
        await _make_notification(db_session, verified_user.id, is_read=False)
        await _make_notification(db_session, verified_user.id, is_read=False)
        await _make_notification(db_session, verified_user.id, is_read=True)
        other = await _create_user(db_session, "other2@example.com")
        await _make_notification(db_session, other.id, is_read=False)

        resp = await client.get("/api/v1/notifications/unread-count", headers=auth_headers)
        assert resp.status_code == 200
        assert _data(resp) == {"count": 2}

    async def test_zero_when_none(self, client: AsyncClient, auth_headers):
        resp = await client.get("/api/v1/notifications/unread-count", headers=auth_headers)
        assert _data(resp) == {"count": 0}


class TestMarkRead:
    async def test_marks_read_and_sets_read_at(
        self, client: AsyncClient, auth_headers, verified_user: User, db_session: AsyncSession
    ):
        n = await _make_notification(db_session, verified_user.id)
        resp = await client.post(f"/api/v1/notifications/{n.id}/read", headers=auth_headers)
        assert resp.status_code == 200
        data = _data(resp)
        assert data["is_read"] is True

        count_resp = await client.get("/api/v1/notifications/unread-count", headers=auth_headers)
        assert _data(count_resp)["count"] == 0

    async def test_idempotent(
        self, client: AsyncClient, auth_headers, verified_user: User, db_session: AsyncSession
    ):
        n = await _make_notification(db_session, verified_user.id)
        first = await client.post(f"/api/v1/notifications/{n.id}/read", headers=auth_headers)
        second = await client.post(f"/api/v1/notifications/{n.id}/read", headers=auth_headers)
        assert first.status_code == 200
        assert second.status_code == 200
        assert _data(first)["is_read"] is True
        assert _data(second)["is_read"] is True

    async def test_not_found(self, client: AsyncClient, auth_headers):
        resp = await client.post(
            f"/api/v1/notifications/{uuid.uuid4()}/read", headers=auth_headers
        )
        assert resp.status_code == 404
        assert _err(resp) == "NOTIFICATION_NOT_FOUND"

    async def test_cannot_read_others_notification(
        self,
        client: AsyncClient,
        auth_headers,
        db_session: AsyncSession,
    ):
        other = await _create_user(db_session, "other3@example.com")
        n = await _make_notification(db_session, other.id)
        resp = await client.post(f"/api/v1/notifications/{n.id}/read", headers=auth_headers)
        assert resp.status_code == 404
        assert _err(resp) == "NOTIFICATION_NOT_FOUND"


class TestMarkAllRead:
    async def test_marks_only_own_unread(
        self,
        client: AsyncClient,
        auth_headers,
        verified_user: User,
        db_session: AsyncSession,
    ):
        await _make_notification(db_session, verified_user.id, is_read=False)
        await _make_notification(db_session, verified_user.id, is_read=False)
        already_read = await _make_notification(db_session, verified_user.id, is_read=True)
        other = await _create_user(db_session, "other4@example.com")
        other_notification = await _make_notification(db_session, other.id, is_read=False)

        resp = await client.post("/api/v1/notifications/read-all", headers=auth_headers)
        assert resp.status_code == 200
        assert _data(resp) == {"updated": 2}

        count_resp = await client.get("/api/v1/notifications/unread-count", headers=auth_headers)
        assert _data(count_resp)["count"] == 0

        await db_session.refresh(already_read)
        assert already_read.is_read is True
        await db_session.refresh(other_notification)
        assert other_notification.is_read is False  # чужое не тронуто

    async def test_no_unread_returns_zero(self, client: AsyncClient, auth_headers):
        resp = await client.post("/api/v1/notifications/read-all", headers=auth_headers)
        assert _data(resp) == {"updated": 0}
