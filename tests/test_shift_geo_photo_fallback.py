"""Тесты старта смены по фото при недоступной геолокации (shift_geo_photo_fallback).

Покрывают: happy path (фото вместо координат), матрицу валидаций пары
`geo_fallback_photo_id`/`geo_fallback_reason`, права и «захват» файла
(is_attached), фильтр `geo_fallback` в реестре смен организации и обратную
совместимость `ShiftResponse` для обычных смен.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core import storage
from src.app.core.security import hash_password
from src.app.models.file import File, FileCategory
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.user import User
from src.app.models.work_location import WorkLocation

# Магические байты JPEG — MIME определяется по содержимому, не по заголовку.
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64

REASON = "GEO_PERMISSION_DENIED_FOREVER"


@pytest.fixture(autouse=True)
def mock_storage(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """In-memory подмена S3-слоя — реальный MinIO тестам не нужен."""
    store: dict[str, bytes] = {}

    async def fake_upload(key: str, body: bytes, content_type: str) -> None:
        store[key] = body

    async def fake_presign(key: str, filename: str) -> str:
        return f"https://storage.test/{key}?sig=fake"

    async def fake_presign_many(items: list[tuple[str, str]]) -> dict[str, str]:
        return {key: f"https://storage.test/{key}?sig=fake" for key, _ in items}

    async def fake_delete(key: str) -> None:
        store.pop(key, None)

    monkeypatch.setattr(storage, "upload_object", fake_upload)
    monkeypatch.setattr(storage, "generate_presigned_get", fake_presign)
    monkeypatch.setattr(storage, "generate_presigned_get_many", fake_presign_many)
    monkeypatch.setattr(storage, "delete_object", fake_delete)
    return store


async def _make_user(db_session: AsyncSession, email: str, name: str) -> User:
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


async def _headers(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "owner@example.com", "Owner")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "owner@example.com")


@pytest.fixture
async def employee_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "employee@example.com", "Employee")


@pytest.fixture
async def employee_headers(employee_user: User, client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "employee@example.com")


@pytest.fixture
async def colleague_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "colleague@example.com", "Colleague")


@pytest.fixture
async def colleague_headers(colleague_user: User, client: AsyncClient) -> dict[str, str]:
    return await _headers(client, "colleague@example.com")


async def _make_org(
    db_session: AsyncSession,
    owner: User,
    members: list[User],
    *,
    geo: bool = True,
    name: str = "Org",
) -> tuple[Organization, WorkLocation]:
    """Организация с одной рабочей точкой и участниками-сотрудниками."""
    org = Organization(name=name, owner_id=owner.id)
    db_session.add(org)
    await db_session.flush()

    db_session.add(OrganizationSettings(organization_id=org.id, geo_check_enabled=geo))
    location = WorkLocation(
        organization_id=org.id,
        name="Точка",
        latitude=55.7558,
        longitude=37.6173,
        radius_meters=200,
    )
    db_session.add(location)
    for member in members:
        db_session.add(
            OrganizationMember(
                organization_id=org.id,
                user_id=member.id,
                role=MemberRole.employee,
            )
        )
    await db_session.commit()
    return org, location


async def _upload_photo(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: uuid.UUID,
    *,
    category: str = "shift_geo_photo",
) -> str:
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": ("selfie.jpg", JPEG_BYTES, "image/jpeg")},
        data={"category": category, "organization_id": str(org_id)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _get_file(db_session: AsyncSession, file_id: str) -> File:
    file = (
        await db_session.execute(select(File).where(File.id == uuid.UUID(file_id)))
    ).scalar_one()
    await db_session.refresh(file)
    return file


def _start_body(
    org: Organization,
    location: WorkLocation,
    photo_id: str,
    reason: str = REASON,
) -> dict[str, Any]:
    return {
        "organization_id": str(org.id),
        "work_location_id": str(location.id),
        "geo_fallback_photo_id": photo_id,
        "geo_fallback_reason": reason,
    }


class TestFileCategory:
    async def test_member_can_upload_shift_geo_photo(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)
        file = await _get_file(db_session, photo_id)
        assert file.category == FileCategory.shift_geo_photo
        assert file.storage_key.startswith("shift-geo-photos/")
        assert file.organization_id == org.id
        assert file.is_attached is False

    async def test_non_member_cannot_upload(
        self,
        client: AsyncClient,
        colleague_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(db_session, owner, [employee_user])
        resp = await client.post(
            "/api/v1/files",
            headers=colleague_headers,
            files={"file": ("selfie.jpg", JPEG_BYTES, "image/jpeg")},
            data={"category": "shift_geo_photo", "organization_id": str(org.id)},
        )
        assert resp.status_code == 403

    async def test_org_owner_can_read_employee_photo(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner_headers: dict[str, str],
        colleague_headers: dict[str, str],
        owner: User,
        employee_user: User,
        colleague_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Разбирает фото админ/owner организации; коллега-сотрудник — нет."""
        org, _ = await _make_org(db_session, owner, [employee_user, colleague_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)

        owner_resp = await client.get(f"/api/v1/files/{photo_id}", headers=owner_headers)
        assert owner_resp.status_code == 200
        assert owner_resp.json()["data"]["url"].startswith("https://storage.test/")

        colleague_resp = await client.get(f"/api/v1/files/{photo_id}", headers=colleague_headers)
        assert colleague_resp.status_code == 403


class TestFallbackStartHappyPath:
    async def test_start_marks_shift_and_attaches_photo(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["geo_fallback"] is True
        assert data["geo_fallback_reason"] == REASON
        assert data["geo_fallback_photo_file_id"] == photo_id
        assert data["work_location_id"] == str(location.id)
        assert data["status"] == "active"

        file = await _get_file(db_session, photo_id)
        assert file.is_attached is True

    async def test_attached_photo_cannot_be_deleted(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Файл смены защищён существующим FILE_IN_USE — фото не пропадёт из-под админа."""
        org, location = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)
        start = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert start.status_code == 201

        resp = await client.delete(f"/api/v1/files/{photo_id}", headers=employee_headers)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "FILE_IN_USE"


class TestFallbackValidation:
    async def test_photo_without_reason_rejected(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={
                "organization_id": str(org.id),
                "work_location_id": str(location.id),
                "geo_fallback_photo_id": photo_id,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        file = await _get_file(db_session, photo_id)
        assert file.is_attached is False

    async def test_reason_without_photo_rejected(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user])
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={
                "organization_id": str(org.id),
                "work_location_id": str(location.id),
                "geo_fallback_reason": REASON,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_coords_together_with_photo_rejected(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Двусмысленность запрещена: фото НЕ обходит проверку «вне зоны»."""
        org, location = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)
        body = _start_body(org, location, photo_id)
        body["latitude"] = 55.7558
        body["longitude"] = 37.6173
        resp = await client.post("/api/v1/shifts/start", headers=employee_headers, json=body)
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        file = await _get_file(db_session, photo_id)
        assert file.is_attached is False

    async def test_unknown_reason_rejected(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id, reason="GEO_SOMETHING_ELSE"),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_work_location_required(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={
                "organization_id": str(org.id),
                "geo_fallback_photo_id": photo_id,
                "geo_fallback_reason": REASON,
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "WORK_LOCATION_REQUIRED"
        # Отказ старта не должен «съедать» фото — его подберёт чистка сирот.
        file = await _get_file(db_session, photo_id)
        assert file.is_attached is False

    async def test_foreign_work_location_rejected(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(db_session, owner, [employee_user])
        other_org, other_location = await _make_org(
            db_session, owner, [employee_user], name="Other"
        )
        photo_id = await _upload_photo(client, employee_headers, org.id)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, other_location, photo_id),
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "WORK_LOCATION_NOT_FOUND"
        assert other_org.id != org.id

    async def test_no_coords_and_no_photo_still_coords_required(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Регрессия: без фото поведение прежнее — 400 COORDS_REQUIRED."""
        org, _ = await _make_org(db_session, owner, [employee_user])
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"organization_id": str(org.id)},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "COORDS_REQUIRED"

    async def test_org_without_geo_check_rejects_fallback(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user], geo=False)
        photo_id = await _upload_photo(client, employee_headers, org.id)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
        file = await _get_file(db_session, photo_id)
        assert file.is_attached is False

    async def test_personal_shift_rejects_fallback(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, _ = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json={"geo_fallback_photo_id": photo_id, "geo_fallback_reason": REASON},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


class TestFallbackPhotoInvalid:
    async def test_unknown_file(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user])
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, str(uuid.uuid4())),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "GEO_FALLBACK_PHOTO_INVALID"

    async def test_malformed_file_id(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user])
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, "not-a-uuid"),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "GEO_FALLBACK_PHOTO_INVALID"

    async def test_foreign_owner_file(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        colleague_headers: dict[str, str],
        owner: User,
        employee_user: User,
        colleague_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Файл коллеги из той же организации — тоже отказ (владелец обязан совпасть)."""
        org, location = await _make_org(db_session, owner, [employee_user, colleague_user])
        photo_id = await _upload_photo(client, colleague_headers, org.id)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "GEO_FALLBACK_PHOTO_INVALID"

    async def test_file_of_another_org(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user])
        other_org, _ = await _make_org(db_session, owner, [employee_user], name="Other")
        photo_id = await _upload_photo(client, employee_headers, other_org.id)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "GEO_FALLBACK_PHOTO_INVALID"

    async def test_wrong_category(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(
            client, employee_headers, org.id, category="checklist_photo"
        )
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "GEO_FALLBACK_PHOTO_INVALID"

    async def test_already_attached_file(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)
        file = await _get_file(db_session, photo_id)
        file.is_attached = True
        await db_session.commit()

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "GEO_FALLBACK_PHOTO_INVALID"

    async def test_photo_cannot_be_reused_for_second_shift(
        self,
        client: AsyncClient,
        employee_headers: dict[str, str],
        owner: User,
        employee_user: User,
        db_session: AsyncSession,
    ) -> None:
        """Одно фото — одна смена: после старта файл захвачен навсегда."""
        org, location = await _make_org(db_session, owner, [employee_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)

        first = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert first.status_code == 201
        finish = await client.post(
            f"/api/v1/shifts/{first.json()['data']['id']}/finish",
            headers=employee_headers,
        )
        assert finish.status_code == 200

        second = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert second.status_code == 422
        assert second.json()["error"]["code"] == "GEO_FALLBACK_PHOTO_INVALID"


class TestOrgShiftsExposeFallback:
    async def test_list_detail_and_filter(
        self,
        client: AsyncClient,
        owner_headers: dict[str, str],
        employee_headers: dict[str, str],
        colleague_headers: dict[str, str],
        owner: User,
        employee_user: User,
        colleague_user: User,
        db_session: AsyncSession,
    ) -> None:
        org, location = await _make_org(db_session, owner, [employee_user, colleague_user])
        photo_id = await _upload_photo(client, employee_headers, org.id)

        fallback_start = await client.post(
            "/api/v1/shifts/start",
            headers=employee_headers,
            json=_start_body(org, location, photo_id),
        )
        assert fallback_start.status_code == 201
        fallback_shift_id = fallback_start.json()["data"]["id"]

        normal_start = await client.post(
            "/api/v1/shifts/start",
            headers=colleague_headers,
            json={
                "organization_id": str(org.id),
                "latitude": 55.7558,
                "longitude": 37.6173,
            },
        )
        assert normal_start.status_code == 201
        normal_shift_id = normal_start.json()["data"]["id"]
        assert normal_start.json()["data"]["geo_fallback"] is False
        assert normal_start.json()["data"]["geo_fallback_reason"] is None
        assert normal_start.json()["data"]["geo_fallback_photo_file_id"] is None

        listed = await client.get(f"/api/v1/organizations/{org.id}/shifts", headers=owner_headers)
        assert listed.status_code == 200
        assert listed.json()["data"]["total"] == 2

        only_fallback = await client.get(
            f"/api/v1/organizations/{org.id}/shifts?geo_fallback=true",
            headers=owner_headers,
        )
        assert only_fallback.status_code == 200
        payload = only_fallback.json()["data"]
        assert payload["total"] == 1
        assert payload["items"][0]["id"] == fallback_shift_id
        assert payload["items"][0]["geo_fallback"] is True
        assert payload["items"][0]["geo_fallback_photo_file_id"] == photo_id

        only_normal = await client.get(
            f"/api/v1/organizations/{org.id}/shifts?geo_fallback=false",
            headers=owner_headers,
        )
        assert only_normal.json()["data"]["total"] == 1
        assert only_normal.json()["data"]["items"][0]["id"] == normal_shift_id

        detail = await client.get(
            f"/api/v1/organizations/{org.id}/shifts/{fallback_shift_id}",
            headers=owner_headers,
        )
        assert detail.status_code == 200
        assert detail.json()["data"]["geo_fallback_reason"] == REASON

    async def test_personal_shift_backward_compatible(
        self,
        client: AsyncClient,
        auth_headers: dict[str, str],
    ) -> None:
        resp = await client.post("/api/v1/shifts/start", headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["geo_fallback"] is False
        assert data["geo_fallback_reason"] is None
        assert data["geo_fallback_photo_file_id"] is None
