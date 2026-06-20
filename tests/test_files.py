# tests/test_files.py
import uuid
from dataclasses import replace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core import storage
from src.app.core.security import hash_password
from src.app.models.file import File, FileCategory
from src.app.models.user import User, UserRole
from src.app.services import file_storage

# Магические байты — filetype.guess определяет MIME по сигнатуре содержимого.
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"0" * 64
TEXT_BYTES = b"just some plain text content, not a recognizable binary type at all"


@pytest.fixture(autouse=True)
def _mock_storage(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """In-memory подмена S3-слоя — тесты не требуют реального MinIO."""
    store: dict[str, bytes] = {}

    async def fake_upload(key: str, body: bytes, content_type: str) -> None:
        store[key] = body

    async def fake_presign(key: str, filename: str) -> str:
        return f"https://storage.test/{key}?sig=fake"

    async def fake_delete(key: str) -> None:
        store.pop(key, None)

    monkeypatch.setattr(storage, "upload_object", fake_upload)
    monkeypatch.setattr(storage, "generate_presigned_get", fake_presign)
    monkeypatch.setattr(storage, "delete_object", fake_delete)
    return store


@pytest.fixture(autouse=True)
async def _owner_is_super_admin(verified_user: User, db_session: AsyncSession) -> None:
    """Создание организации требует super_admin; владелец повышается."""
    verified_user.role = UserRole.super_admin
    await db_session.commit()


@pytest.fixture
async def employee_headers(
    client: AsyncClient,
    db_session: AsyncSession,
) -> dict[str, str]:
    """Второй пользователь (обычный, не member ни в одной org по умолчанию)."""
    user = User(
        id=uuid.uuid4(),
        email="employee@example.com",
        password_hash=hash_password("Test1234"),
        name="Employee",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "employee@example.com", "password": "Test1234"},
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _create_org(client: AsyncClient, headers: dict[str, str]) -> dict:
    resp = await client.post("/api/v1/organizations", headers=headers, json={"name": "Org"})
    return resp.json()["data"]


async def _join(client: AsyncClient, headers: dict[str, str], invite_code: str) -> None:
    await client.post(f"/api/v1/organizations/join/{invite_code}", headers=headers)


async def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    category: str,
    content: bytes,
    filename: str,
    content_type: str,
    organization_id: str | None = None,
):
    data: dict[str, str] = {"category": category}
    if organization_id is not None:
        data["organization_id"] = organization_id
    return await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": (filename, content, content_type)},
        data=data,
    )


class TestUpload:
    async def test_avatar_upload_success(self, client: AsyncClient, auth_headers):
        resp = await _upload(
            client,
            auth_headers,
            category="avatar",
            content=JPEG_BYTES,
            filename="me.jpg",
            content_type="image/jpeg",
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["category"] == "avatar"
        assert data["content_type"] == "image/jpeg"
        assert data["original_filename"] == "me.jpg"
        assert data["size_bytes"] == len(JPEG_BYTES)
        assert data["url"].startswith("https://storage.test/")
        assert "url_expires_at" in data

    async def test_checklist_photo_by_member(
        self, client: AsyncClient, auth_headers, employee_headers
    ):
        org = await _create_org(client, auth_headers)
        await _join(client, employee_headers, org["invite_code"])

        resp = await _upload(
            client,
            employee_headers,
            category="checklist_photo",
            content=PNG_BYTES,
            filename="proof.png",
            content_type="image/png",
            organization_id=org["id"],
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["content_type"] == "image/png"

    async def test_knowledge_base_pdf_by_owner(self, client: AsyncClient, auth_headers):
        org = await _create_org(client, auth_headers)
        resp = await _upload(
            client,
            auth_headers,
            category="knowledge_base",
            content=PDF_BYTES,
            filename="guide.pdf",
            content_type="application/pdf",
            organization_id=org["id"],
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["content_type"] == "application/pdf"

    async def test_other_accepts_unknown_type(self, client: AsyncClient, auth_headers):
        resp = await _upload(
            client,
            auth_headers,
            category="other",
            content=TEXT_BYTES,
            filename="notes.txt",
            content_type="text/plain",
        )
        assert resp.status_code == 201
        # filetype не распознаёт текст → падаем на заявленный content_type.
        assert resp.json()["data"]["content_type"] == "text/plain"

    async def test_invalid_category(self, client: AsyncClient, auth_headers):
        resp = await _upload(
            client,
            auth_headers,
            category="bogus",
            content=JPEG_BYTES,
            filename="x.jpg",
            content_type="image/jpeg",
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_FILE_CATEGORY"

    async def test_unsupported_type(self, client: AsyncClient, auth_headers):
        org = await _create_org(client, auth_headers)
        # PDF под видом фото чек-листа (только image/*) → 415 по реальному MIME.
        resp = await _upload(
            client,
            auth_headers,
            category="checklist_photo",
            content=PDF_BYTES,
            filename="fake.jpg",
            content_type="image/jpeg",
            organization_id=org["id"],
        )
        assert resp.status_code == 415
        assert resp.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"

    async def test_too_large(self, client: AsyncClient, auth_headers, monkeypatch):
        tiny = replace(file_storage.CATEGORY_POLICIES[FileCategory.other], max_size_bytes=10)
        monkeypatch.setitem(file_storage.CATEGORY_POLICIES, FileCategory.other, tiny)

        resp = await _upload(
            client,
            auth_headers,
            category="other",
            content=b"x" * 50,
            filename="big.bin",
            content_type="application/octet-stream",
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "FILE_TOO_LARGE"

    async def test_org_category_requires_org_id(self, client: AsyncClient, auth_headers):
        resp = await _upload(
            client,
            auth_headers,
            category="checklist_photo",
            content=JPEG_BYTES,
            filename="x.jpg",
            content_type="image/jpeg",
        )
        assert resp.status_code == 422


class TestUploadRBAC:
    async def test_knowledge_base_by_employee_forbidden(
        self, client: AsyncClient, auth_headers, employee_headers
    ):
        org = await _create_org(client, auth_headers)
        await _join(client, employee_headers, org["invite_code"])

        resp = await _upload(
            client,
            employee_headers,
            category="knowledge_base",
            content=PDF_BYTES,
            filename="guide.pdf",
            content_type="application/pdf",
            organization_id=org["id"],
        )
        assert resp.status_code == 403

    async def test_checklist_photo_foreign_org_forbidden(
        self, client: AsyncClient, auth_headers, employee_headers
    ):
        org = await _create_org(client, auth_headers)  # employee НЕ вступал

        resp = await _upload(
            client,
            employee_headers,
            category="checklist_photo",
            content=JPEG_BYTES,
            filename="x.jpg",
            content_type="image/jpeg",
            organization_id=org["id"],
        )
        assert resp.status_code == 403


class TestGetFile:
    async def test_get_refreshes_url(self, client: AsyncClient, auth_headers):
        up = await _upload(
            client,
            auth_headers,
            category="avatar",
            content=JPEG_BYTES,
            filename="me.jpg",
            content_type="image/jpeg",
        )
        file_id = up.json()["data"]["id"]

        resp = await client.get(f"/api/v1/files/{file_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["id"] == file_id
        assert resp.json()["data"]["url"].startswith("https://storage.test/")

    async def test_get_not_found(self, client: AsyncClient, auth_headers):
        resp = await client.get(f"/api/v1/files/{uuid.uuid4()}", headers=auth_headers)
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "FILE_NOT_FOUND"

    async def test_get_others_personal_forbidden(
        self, client: AsyncClient, auth_headers, employee_headers
    ):
        up = await _upload(
            client,
            auth_headers,
            category="avatar",
            content=JPEG_BYTES,
            filename="me.jpg",
            content_type="image/jpeg",
        )
        file_id = up.json()["data"]["id"]

        resp = await client.get(f"/api/v1/files/{file_id}", headers=employee_headers)
        assert resp.status_code == 403


class TestDeleteFile:
    async def test_delete_success_and_idempotent(self, client: AsyncClient, auth_headers):
        up = await _upload(
            client,
            auth_headers,
            category="avatar",
            content=JPEG_BYTES,
            filename="me.jpg",
            content_type="image/jpeg",
        )
        file_id = up.json()["data"]["id"]

        resp = await client.delete(f"/api/v1/files/{file_id}", headers=auth_headers)
        assert resp.status_code == 200

        again = await client.delete(f"/api/v1/files/{file_id}", headers=auth_headers)
        assert again.status_code == 404
        assert again.json()["error"]["code"] == "FILE_NOT_FOUND"

    async def test_delete_attached_conflict(
        self, client: AsyncClient, auth_headers, db_session: AsyncSession
    ):
        up = await _upload(
            client,
            auth_headers,
            category="avatar",
            content=JPEG_BYTES,
            filename="me.jpg",
            content_type="image/jpeg",
        )
        file_id = up.json()["data"]["id"]

        file = (
            await db_session.execute(select(File).where(File.id == uuid.UUID(file_id)))
        ).scalar_one()
        file.is_attached = True
        await db_session.commit()

        resp = await client.delete(f"/api/v1/files/{file_id}", headers=auth_headers)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "FILE_IN_USE"

    async def test_delete_others_personal_forbidden(
        self, client: AsyncClient, auth_headers, employee_headers
    ):
        up = await _upload(
            client,
            auth_headers,
            category="avatar",
            content=JPEG_BYTES,
            filename="me.jpg",
            content_type="image/jpeg",
        )
        file_id = up.json()["data"]["id"]

        resp = await client.delete(f"/api/v1/files/{file_id}", headers=employee_headers)
        assert resp.status_code == 403
