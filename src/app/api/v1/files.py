import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, UploadFile

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.file import File as FileModel
from src.app.schemas.base import ApiResponse
from src.app.schemas.file import FileResponse
from src.app.services import file_storage as file_service

router = APIRouter(prefix="/files", tags=["files"])


def _file_response(file: FileModel, url: str, url_expires_at: datetime) -> dict[str, Any]:
    return FileResponse(
        id=str(file.id),
        category=file.category.value,
        original_filename=file.original_filename,
        content_type=file.content_type,
        size_bytes=file.size_bytes,
        url=url,
        url_expires_at=url_expires_at,
        created_at=file.created_at,
    ).model_dump(mode="json")


@router.post(
    "",
    status_code=201,
    summary="Загрузить файл",
    description=(
        "Загружает файл через бэкенд в приватное хранилище и регистрирует его в "
        "реестре `files` (is_attached=false). Тип проверяется по реальному "
        "содержимому, размер — по политике категории. Право проверяется по "
        "`category`: knowledge_base — только admin/owner организации; "
        "checklist_photo — любой участник; avatar/other — персональные. "
        "Возвращает метаданные и свежий presigned GET URL."
    ),
)
async def upload_file(
    user: CurrentUserDep,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="Бинарный файл")],
    category: Annotated[str, Form(description="Категория из FileCategory")],
    organization_id: Annotated[
        uuid.UUID | None,
        Form(description="Обязателен для org-категорий; для персональных игнорируется"),
    ] = None,
) -> ApiResponse:
    created = await file_service.upload_file(session, user, category, organization_id, file)
    await session.commit()
    url, expires_at = await file_service.presigned_url_for(created)
    return ApiResponse.success(_file_response(created, url, expires_at))


@router.get(
    "/{file_id}",
    summary="Метаданные файла + свежий presigned URL",
    description=(
        "Возвращает метаданные файла и новый presigned GET URL (для обновления "
        "протухшей ссылки). Доступ — загрузивший, admin/owner организации файла "
        "или участник (для knowledge_base)."
    ),
)
async def get_file(
    file_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    file = await file_service.get_file_for_read(session, file_id, user)
    url, expires_at = await file_service.presigned_url_for(file)
    return ApiResponse.success(_file_response(file, url, expires_at))


@router.delete(
    "/{file_id}",
    summary="Удалить файл",
    description=(
        "Удаляет объект из хранилища и строку реестра. Привязанный файл "
        "(is_attached=true) удалить нельзя — вернётся FILE_IN_USE (409). "
        "Доступ — загрузивший, admin/owner организации файла или super_admin."
    ),
)
async def delete_file(
    file_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await file_service.delete_file(session, file_id, user)
    await session.commit()
    return ApiResponse.success({"message": "Файл удалён"})
