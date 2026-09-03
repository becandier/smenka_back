# tests/test_checklist_photos.py
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core import storage
from src.app.core.storage import StorageError
from src.app.models.checklist import ChecklistItemPhoto
from src.app.models.file import File
from src.app.models.shift import Shift, ShiftStatus
from src.app.services import checklist_instance as instance_service
from tests.test_checklist_instances import (
    _setup,
    _start_org_shift,
)

JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64


@pytest.fixture(autouse=True)
def mock_storage(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """In-memory подмена S3-слоя (включая батч-подпись) — без реального MinIO."""
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


async def _make_template(
    client: AsyncClient,
    owner_headers: dict[str, str],
    org_id: str,
    role_id: str,
    *,
    photo_requirement: str = "required",
    photo_source: str = "camera",
    is_required: bool = True,
) -> tuple[str, str]:
    """Шаблон с одним пунктом, у которого заданы фото-настройки. → (tpl_id, item_id)."""
    tpl_resp = await client.post(
        f"/api/v1/organizations/{org_id}/checklist-templates",
        headers=owner_headers,
        json={"name": "Открытие", "type": "shift_start", "is_required": True},
    )
    tpl_id = tpl_resp.json()["data"]["id"]
    item_resp = await client.post(
        f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/items",
        headers=owner_headers,
        json={
            "text": "Сфотографировать витрину",
            "is_required": is_required,
            "photo_requirement": photo_requirement,
            "photo_source": photo_source,
        },
    )
    item_id = item_resp.json()["data"]["id"]
    await client.put(
        f"/api/v1/organizations/{org_id}/checklist-templates/{tpl_id}/roles",
        headers=owner_headers,
        json={"role_ids": [role_id]},
    )
    return tpl_id, item_id


async def _drill_to_item(
    client: AsyncClient,
    headers: dict[str, str],
    shift_id: str,
) -> tuple[str, str]:
    """Возвращает (instance_id, item_id) первого экземпляра/пункта смены."""
    listing = await client.get(f"/api/v1/shifts/{shift_id}/checklists", headers=headers)
    inst_id = listing.json()["data"]["items"][0]["id"]
    detail = await client.get(f"/api/v1/shifts/{shift_id}/checklists/{inst_id}", headers=headers)
    item_id = detail.json()["data"]["items"][0]["id"]
    return inst_id, item_id


async def _upload_photo(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    category: str = "checklist_photo",
    organization_id: str | None = None,
) -> str:
    data: dict[str, str] = {"category": category}
    if organization_id is not None:
        data["organization_id"] = organization_id
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": ("shot.jpg", JPEG_BYTES, "image/jpeg")},
        data=data,
    )
    return resp.json()["data"]["id"]


class TestTemplatePhotoFields:
    async def test_create_and_read_item_photo_fields(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
            photo_source="camera_or_gallery",
        )
        # photo_source сохранён как есть при requirement != none.
        tpl_list = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates",
            headers=super_admin_headers,
        )
        tpl_id = tpl_list.json()["data"]["items"][0]["id"]
        detail = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}",
            headers=super_admin_headers,
        )
        item = detail.json()["data"]["items"][0]
        assert item["photo_requirement"] == "required"
        assert item["photo_source"] == "camera_or_gallery"

    async def test_requirement_none_normalizes_source(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        tpl_resp = await client.post(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates",
            headers=super_admin_headers,
            json={"name": "X", "type": "shift_start", "is_required": True},
        )
        tpl_id = tpl_resp.json()["data"]["id"]
        # requirement=none, но передаём source=camera_or_gallery → нормализуется к camera.
        item_resp = await client.post(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/items",
            headers=super_admin_headers,
            json={
                "text": "Без фото",
                "photo_requirement": "none",
                "photo_source": "camera_or_gallery",
            },
        )
        assert item_resp.json()["data"]["photo_requirement"] == "none"
        assert item_resp.json()["data"]["photo_source"] == "camera"

    async def test_patch_to_none_forces_camera(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        tpl_id, item_id = await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="optional",
            photo_source="camera_or_gallery",
        )
        patch = await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/items/{item_id}",
            headers=super_admin_headers,
            json={"photo_requirement": "none"},
        )
        assert patch.json()["data"]["photo_requirement"] == "none"
        assert patch.json()["data"]["photo_source"] == "camera"

    async def test_invalid_enum_returns_422(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        tpl_resp = await client.post(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates",
            headers=super_admin_headers,
            json={"name": "X", "type": "shift_start", "is_required": True},
        )
        tpl_id = tpl_resp.json()["data"]["id"]
        resp = await client.post(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/items",
            headers=super_admin_headers,
            json={"text": "t", "photo_requirement": "bogus"},
        )
        assert resp.status_code == 422


class TestSnapshot:
    async def test_instance_snapshots_photo_fields(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        tpl_id, item_id = await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, _item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)

        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        item = detail.json()["data"]["items"][0]
        assert item["photo_requirement"] == "required"
        assert item["photos_count"] == 0
        assert item["photos"] == []
        assert detail.json()["data"]["max_photos_per_item"] == 10

        # Изменение шаблона после старта смены не трогает снимок.
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl_id}/items/{item_id}",
            headers=super_admin_headers,
            json={"photo_requirement": "none"},
        )
        detail2 = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        assert detail2.json()["data"]["items"][0]["photo_requirement"] == "required"


class TestAttachPhoto:
    async def test_attach_and_status_flips_to_completed(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)

        # Отметить выполненным без фото — required-пункт остаётся blocking → pending.
        await client.patch(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}",
            headers=ctx["member_headers"],
            json={"is_completed": True},
        )
        listing = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists", headers=ctx["member_headers"]
        )
        summary = listing.json()["data"]["items"][0]
        assert summary["status"] == "pending"
        assert summary["items_summary"]["completed"] == 1
        assert summary["items_summary"]["satisfied_count"] == 0
        assert summary["items_summary"]["photos_required_missing"] == 1

        # Привязать фото → satisfied → completed.
        file_id = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])
        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={
                "file_id": file_id,
                "captured_at": "2026-06-20T08:04:30Z",
                "latitude": 55.751244,
                "longitude": 37.618423,
            },
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["file_id"] == file_id
        assert data["url"].startswith("https://storage.test/")
        assert data["latitude"] == 55.751244

        listing2 = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists", headers=ctx["member_headers"]
        )
        summary2 = listing2.json()["data"]["items"][0]
        assert summary2["status"] == "completed"
        assert summary2["items_summary"]["satisfied_count"] == 1
        assert summary2["items_summary"]["photos_required_missing"] == 0

        # Файл помечен привязанным.
        file = (
            await db_session.execute(select(File).where(File.id == uuid.UUID(file_id)))
        ).scalar_one()
        assert file.is_attached is True

    async def test_attach_to_none_item_forbidden(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="none",
            is_required=False,
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        file_id = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])
        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": file_id},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "PHOTO_NOT_ALLOWED"

    async def test_limit_exceeded(
        self,
        client: AsyncClient,
        super_admin_headers,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(instance_service.settings, "checklist_max_photos_per_item", 1)
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="optional",
            is_required=False,
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)

        first = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])
        second = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])
        ok = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": first},
        )
        assert ok.status_code == 201
        over = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": second},
        )
        assert over.status_code == 409
        assert over.json()["error"]["code"] == "PHOTO_LIMIT_EXCEEDED"

    async def test_wrong_category_file_invalid(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        # avatar — не checklist_photo.
        file_id = await _upload_photo(client, ctx["member_headers"], category="avatar")
        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": file_id},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "PHOTO_FILE_INVALID"

    async def test_nonexistent_file_invalid(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "PHOTO_FILE_INVALID"

    async def test_double_bind_same_file_invalid(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="optional",
            is_required=False,
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        file_id = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])
        url = f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos"
        first = await client.post(url, headers=ctx["member_headers"], json={"file_id": file_id})
        assert first.status_code == 201
        again = await client.post(url, headers=ctx["member_headers"], json={"file_id": file_id})
        assert again.status_code == 400
        assert again.json()["error"]["code"] == "PHOTO_FILE_INVALID"

    async def test_attach_on_finished_shift(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        file_id = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])

        shift = (
            await db_session.execute(select(Shift).where(Shift.id == uuid.UUID(shift_id)))
        ).scalar_one()
        shift.status = ShiftStatus.finished
        await db_session.commit()

        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": file_id},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "SHIFT_FINISHED"

    async def test_attach_within_grace_window(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """checklist_grace_period: привязка фото — та же операция, что и отметка
        пункта, — разрешена, пока окно дозаполнения открыто (дефолт 30 минут)."""
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        file_id = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])

        finish_resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish",
            headers=ctx["member_headers"],
        )
        assert finish_resp.status_code == 200

        resp = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": file_id},
        )
        assert resp.status_code == 201, resp.text


class TestDetachPhoto:
    async def test_detach_deletes_file_and_object(
        self,
        client: AsyncClient,
        super_admin_headers,
        db_session: AsyncSession,
        mock_storage: dict[str, bytes],
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        file_id = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])
        bind = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": file_id},
        )
        photo_id = bind.json()["data"]["id"]
        assert len(mock_storage) == 1  # объект загружен

        resp = await client.delete(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos/{photo_id}",
            headers=ctx["member_headers"],
        )
        assert resp.status_code == 200
        assert resp.json() == {"data": None, "error": None}

        # Строки files и связи нет, объект удалён.
        assert (
            await db_session.execute(select(File).where(File.id == uuid.UUID(file_id)))
        ).scalar_one_or_none() is None
        assert (
            await db_session.execute(
                select(ChecklistItemPhoto).where(ChecklistItemPhoto.id == uuid.UUID(photo_id))
            )
        ).scalar_one_or_none() is None
        assert len(mock_storage) == 0

        # Статус откатился обратно в pending.
        listing = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists", headers=ctx["member_headers"]
        )
        assert listing.json()["data"]["items"][0]["status"] == "pending"

    async def test_detach_within_grace_window(
        self,
        client: AsyncClient,
        super_admin_headers,
        db_session: AsyncSession,
        mock_storage: dict[str, bytes],
    ):
        """checklist_grace_period: отвязка фото разрешена, пока окно открыто —
        привязали ДО завершения смены, отвязываем ПОСЛЕ (в пределах окна)."""
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        file_id = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])
        bind = await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": file_id},
        )
        photo_id = bind.json()["data"]["id"]

        finish_resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish",
            headers=ctx["member_headers"],
        )
        assert finish_resp.status_code == 200

        resp = await client.delete(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos/{photo_id}",
            headers=ctx["member_headers"],
        )
        assert resp.status_code == 200
        assert resp.json() == {"data": None, "error": None}

    async def test_detach_missing_photo(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        resp = await client.delete(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos/{uuid.uuid4()}",
            headers=ctx["member_headers"],
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "PHOTO_NOT_FOUND"


class TestStorageDegradation:
    async def test_detail_url_null_on_storage_failure(
        self,
        client: AsyncClient,
        super_admin_headers,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        file_id = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])
        await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": file_id},
        )

        async def boom(items: list[tuple[str, str]]) -> dict[str, str]:
            raise StorageError("down")

        monkeypatch.setattr(storage, "generate_presigned_get_many", boom)

        detail = await client.get(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}",
            headers=ctx["member_headers"],
        )
        assert detail.status_code == 200
        photo = detail.json()["data"]["items"][0]["photos"][0]
        assert photo["url"] is None
        assert photo["url_expires_at"] is None
        assert photo["file_id"] == file_id


class TestCleanupHook:
    async def test_cleanup_shift_photo_files(
        self,
        client: AsyncClient,
        super_admin_headers,
        db_session: AsyncSession,
        mock_storage: dict[str, bytes],
    ):
        ctx = await _setup(client, db_session, super_admin_headers)
        await _make_template(
            client,
            super_admin_headers,
            ctx["org_id"],
            ctx["role_id"],
            photo_requirement="required",
        )
        shift_id = await _start_org_shift(client, ctx["member_headers"], ctx["org_id"])
        inst_id, item_id = await _drill_to_item(client, ctx["member_headers"], shift_id)
        file_id = await _upload_photo(client, ctx["member_headers"], organization_id=ctx["org_id"])
        await client.post(
            f"/api/v1/shifts/{shift_id}/checklists/{inst_id}/items/{item_id}/photos",
            headers=ctx["member_headers"],
            json={"file_id": file_id},
        )
        assert len(mock_storage) == 1

        member = ctx["member_user"]
        deleted = await instance_service.cleanup_shift_photo_files(
            db_session, uuid.UUID(shift_id), member
        )
        await db_session.commit()
        assert deleted == 1
        assert len(mock_storage) == 0
        assert (
            await db_session.execute(select(File).where(File.id == uuid.UUID(file_id)))
        ).scalar_one_or_none() is None
