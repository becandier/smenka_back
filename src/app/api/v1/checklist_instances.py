import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.core.config import get_settings
from src.app.models.checklist import (
    ChecklistInstance,
    ChecklistInstanceItem,
    ChecklistItemPhoto,
)
from src.app.models.file import File
from src.app.schemas.base import ApiResponse
from src.app.schemas.checklist import (
    ChecklistInstanceDetailResponse,
    ChecklistInstanceListResponse,
    ChecklistInstanceResponse,
    InstanceItemResponse,
    InstanceItemUpdate,
    ItemsSummary,
    OrgChecklistInstanceResponse,
    PhotoBindRequest,
    PhotoResponse,
)
from src.app.schemas.shift import ShiftWorkLocation
from src.app.services import checklist_instance as instance_service
from src.app.services import file_storage

router = APIRouter(prefix="/shifts/{shift_id}/checklists", tags=["checklist-instances"])

settings = get_settings()


# url_map: file_id -> (presigned url | None, url_expires_at | None)
_UrlMap = dict[uuid.UUID, tuple[str | None, datetime | None]]


def _instance_to_response(
    instance: ChecklistInstance,
    total: int,
    completed: int,
    satisfied_count: int,
    photos_required_missing: int,
) -> dict[str, Any]:
    return ChecklistInstanceResponse(
        id=str(instance.id),
        name=instance.name,
        type=instance.type.value,
        is_required=instance.is_required,
        status=instance.status.value,
        completed_at=instance.completed_at,
        items_summary=ItemsSummary(
            total=total,
            completed=completed,
            satisfied_count=satisfied_count,
            photos_required_missing=photos_required_missing,
        ),
        created_at=instance.created_at,
    ).model_dump(mode="json")


def _org_instance_row_to_response(
    row: instance_service.OrgChecklistInstanceRow,
) -> dict[str, Any]:
    """Строка реестра организации (checklist_reports) в JSON-совместимый dict."""
    instance = row.instance
    shift = row.shift
    user = row.user
    work_location = row.work_location
    return OrgChecklistInstanceResponse(
        id=str(instance.id),
        shift_id=str(instance.shift_id),
        template_id=str(instance.template_id) if instance.template_id else None,
        name=instance.name,
        type=instance.type.value,
        is_required=instance.is_required,
        status=instance.status.value,
        completed_at=instance.completed_at,
        created_at=instance.created_at,
        items_summary=ItemsSummary(
            total=row.items_total,
            completed=row.items_completed,
            satisfied_count=row.satisfied_count,
            photos_required_missing=row.photos_required_missing,
        ),
        photos_count=row.photos_count,
        user_id=str(shift.user_id),
        user_name=user.name if user is not None else None,
        user_email=user.email if user is not None else None,
        shift_started_at=shift.started_at,
        shift_finished_at=shift.finished_at,
        shift_status=shift.status.value,
        work_location=(
            ShiftWorkLocation(
                id=str(work_location.id),
                name=work_location.name,
                address=work_location.address,
            )
            if work_location is not None
            else None
        ),
    ).model_dump(mode="json")


def _photo_to_response(photo: ChecklistItemPhoto, url_map: _UrlMap) -> PhotoResponse:
    url, expires_at = url_map.get(photo.file_id, (None, None))
    return PhotoResponse(
        id=str(photo.id),
        file_id=str(photo.file_id),
        url=url,
        url_expires_at=expires_at,
        captured_at=photo.captured_at,
        latitude=photo.latitude,
        longitude=photo.longitude,
        position=photo.position,
    )


def _item_to_response(item: ChecklistInstanceItem, url_map: _UrlMap) -> dict[str, Any]:
    photos = [
        _photo_to_response(p, url_map) for p in sorted(item.photos, key=lambda x: x.position)
    ]
    return InstanceItemResponse(
        id=str(item.id),
        text=item.text,
        is_required=item.is_required,
        position=item.position,
        is_completed=item.is_completed,
        comment=item.comment,
        completed_at=item.completed_at,
        change_count=item.change_count,
        photo_requirement=item.photo_requirement.value,
        photo_source=item.photo_source.value,
        photos_count=len(item.photos),
        photos=photos,
    ).model_dump(mode="json")


def _collect_files(items: list[ChecklistInstanceItem]) -> list[File]:
    return [p.file for it in items for p in it.photos if p.file is not None]


async def _instance_detail_to_response(instance: ChecklistInstance) -> dict[str, Any]:
    url_map = await file_storage.presigned_urls_for(_collect_files(list(instance.items)))
    return ChecklistInstanceDetailResponse(
        id=str(instance.id),
        name=instance.name,
        type=instance.type.value,
        is_required=instance.is_required,
        status=instance.status.value,
        completed_at=instance.completed_at,
        created_at=instance.created_at,
        max_photos_per_item=settings.checklist_max_photos_per_item,
        items=[
            _item_to_response(it, url_map)
            for it in sorted(instance.items, key=lambda x: x.position)
        ],
    ).model_dump(mode="json")


@router.get(
    "",
    summary="Чек-листы смены",
    description="Список экземпляров чек-листов смены со сводкой. Доступно владельцу "
    "смены, владельцу и админам организации.",
)
async def list_shift_checklists(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    rows = await instance_service.get_shift_checklists(session, shift_id, user.id)
    return ApiResponse.success(
        ChecklistInstanceListResponse(
            items=[
                _instance_to_response(inst, total, completed, satisfied, missing)
                for inst, total, completed, satisfied, missing in rows
            ],
        ).model_dump(mode="json")
    )


@router.get(
    "/{instance_id}",
    summary="Детали экземпляра чек-листа",
    description="Экземпляр с упорядоченными пунктами.",
)
async def get_instance(
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    instance = await instance_service.get_instance_detail(
        session,
        shift_id,
        instance_id,
        user.id,
    )
    return ApiResponse.success(await _instance_detail_to_response(instance))


@router.patch(
    "/{instance_id}/items/{item_id}",
    summary="Обновить пункт",
    description="Отметить/снять пункт и добавить комментарий. Только владелец смены, "
    "только пока смена активна.",
)
async def update_item(
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    item_id: uuid.UUID,
    body: InstanceItemUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    item = await instance_service.update_instance_item(
        session,
        shift_id,
        instance_id,
        item_id,
        user.id,
        is_completed=body.is_completed,
        comment=body.comment,
    )
    await session.commit()
    url_map = await file_storage.presigned_urls_for(
        [p.file for p in item.photos if p.file is not None]
    )
    return ApiResponse.success(_item_to_response(item, url_map))


@router.post(
    "/{instance_id}/items/{item_id}/photos",
    status_code=201,
    summary="Привязать фото к пункту",
    description="Привязывает ранее загруженный файл checklist_photo к пункту-экземпляру. "
    "Только владелец активной смены.",
)
async def add_photo(
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    item_id: uuid.UUID,
    body: PhotoBindRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    photo, file = await instance_service.attach_photo(
        session,
        shift_id,
        instance_id,
        item_id,
        user,
        file_id=body.file_id,
        captured_at=body.captured_at,
        latitude=body.latitude,
        longitude=body.longitude,
    )
    await session.commit()
    url_map = await file_storage.presigned_urls_for([file])
    return ApiResponse.success(_photo_to_response(photo, url_map).model_dump(mode="json"))


@router.delete(
    "/{instance_id}/items/{item_id}/photos/{photo_id}",
    summary="Отвязать и удалить фото",
    description="Снимает привязку, удаляет объект из storage и строку файла. "
    "Только владелец активной смены.",
)
async def remove_photo(
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    item_id: uuid.UUID,
    photo_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await instance_service.detach_photo(
        session,
        shift_id,
        instance_id,
        item_id,
        photo_id,
        user,
    )
    await session.commit()
    return ApiResponse.success(None)
