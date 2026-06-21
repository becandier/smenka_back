"""Файловое хранилище: валидация, политики категорий, реестр `files`, права.

Обобщённый механизм поверх S3-слоя (`core.storage`): загрузка через бэкенд,
реестр блобов, выдача presigned URL и удаление. Конкретные привязки к
чек-листам/базе знаний приедут со своими фичами (паттерн FK-от-потребителя)."""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import filetype
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core import storage
from src.app.core.config import get_settings
from src.app.core.logging import get_logger
from src.app.core.storage import StorageError
from src.app.models.file import File, FileCategory
from src.app.models.user import User, UserRole
from src.app.services.common import (
    AccessError,
    ensure_admin_or_owner,
    ensure_member,
)
from src.app.services.organization import get_organization

logger = get_logger(__name__)
settings = get_settings()

_MB = 1024 * 1024
_READ_CHUNK = _MB
_IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/heic"})


class FileError(Exception):
    """Доменная ошибка файлового хранилища. Маппится в {data,error} в main.py."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class CategoryPolicy:
    prefix: str
    max_size_bytes: int
    # Точный набор разрешённых MIME (поверх флагов ниже).
    allowed_mimes: frozenset[str]
    # Разрешить любой image/* (для knowledge_base).
    allow_any_image: bool = False
    # Разрешить любой тип (для other).
    allow_any: bool = False
    # Категория требует organization_id и проверяется на членство/роль.
    org_scoped: bool = False


CATEGORY_POLICIES: dict[FileCategory, CategoryPolicy] = {
    FileCategory.checklist_photo: CategoryPolicy(
        prefix="checklist-photos/",
        max_size_bytes=10 * _MB,
        allowed_mimes=_IMAGE_MIMES,
        org_scoped=True,
    ),
    FileCategory.knowledge_base: CategoryPolicy(
        prefix="knowledge-base/",
        max_size_bytes=50 * _MB,
        allowed_mimes=frozenset({"application/pdf"}),
        allow_any_image=True,
        org_scoped=True,
    ),
    FileCategory.avatar: CategoryPolicy(
        prefix="avatars/",
        max_size_bytes=5 * _MB,
        allowed_mimes=frozenset({"image/jpeg", "image/png", "image/webp"}),
    ),
    FileCategory.other: CategoryPolicy(
        prefix="other/",
        max_size_bytes=10 * _MB,
        allowed_mimes=frozenset(),
        allow_any=True,
    ),
}


def _parse_category(raw: str) -> FileCategory:
    try:
        return FileCategory(raw)
    except ValueError:
        raise FileError(
            "INVALID_FILE_CATEGORY",
            f"Неизвестная категория файла: {raw}",
            400,
        ) from None


def _sanitize_filename(name: str | None) -> str:
    """Базовое имя без путей и управляющих символов; ограничено 255."""
    candidate = (name or "file").replace("\\", "/").split("/")[-1]
    candidate = re.sub(r'[\x00-\x1f"\\]', "", candidate).strip()
    return (candidate or "file")[:255]


def _mime_allowed(policy: CategoryPolicy, mime: str | None) -> bool:
    if policy.allow_any:
        return True
    if mime is None:
        return False
    if policy.allow_any_image and mime.startswith("image/"):
        return True
    return mime in policy.allowed_mimes


def _build_storage_key(
    policy: CategoryPolicy,
    scope: str,
    ext: str | None,
) -> str:
    now = datetime.now(UTC)
    suffix = f".{ext}" if ext else ""
    return f"{policy.prefix}{scope}/{now:%Y}/{now:%m}/{uuid.uuid4().hex}{suffix}"


async def _read_with_limit(upload: UploadFile, limit: int) -> bytes:
    """Читает файл стримом, обрывая при превышении лимита (не доверяем заголовку)."""
    if upload.size is not None and upload.size > limit:
        raise FileError("FILE_TOO_LARGE", "Файл превышает допустимый размер", 413)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise FileError("FILE_TOO_LARGE", "Файл превышает допустимый размер", 413)
        chunks.append(chunk)
    return b"".join(chunks)


async def _check_upload_access(
    session: AsyncSession,
    policy: CategoryPolicy,
    category: FileCategory,
    organization_id: uuid.UUID | None,
    user: User,
) -> uuid.UUID | None:
    """Проверяет право на загрузку и возвращает effective organization_id.

    Для персональных категорий org_id игнорируется (всегда None)."""
    if not policy.org_scoped:
        return None
    if organization_id is None:
        raise FileError(
            "VALIDATION_ERROR",
            "organization_id обязателен для этой категории",
            422,
        )
    org = await get_organization(session, organization_id)
    if category == FileCategory.knowledge_base:
        await ensure_admin_or_owner(
            session, org, user.id, message="Нет прав на загрузку в базу знаний"
        )
    else:  # checklist_photo — любой участник организации
        await ensure_member(session, org, user.id, message="Нужно быть участником организации")
    return organization_id


async def upload_file(
    session: AsyncSession,
    user: User,
    raw_category: str,
    organization_id: uuid.UUID | None,
    upload: UploadFile,
) -> File:
    """Валидирует, грузит объект в storage и создаёт строку `files`.

    Порядок: проверки (категория, права, размер, реальный MIME) → put_object →
    запись строки (`is_attached=false`). Строка флашится после успешной загрузки;
    коммит — на стороне эндпоинта."""
    category = _parse_category(raw_category)
    policy = CATEGORY_POLICIES[category]

    effective_org_id = await _check_upload_access(session, policy, category, organization_id, user)

    limit = min(policy.max_size_bytes, settings.max_upload_size_mb * _MB)
    content = await _read_with_limit(upload, limit)

    # Реальный MIME — по сигнатуре содержимого, а не по заголовку multipart.
    kind = filetype.guess(content)
    detected_mime: str | None = kind.mime if kind else None
    detected_ext: str | None = kind.extension if kind else None
    if not _mime_allowed(policy, detected_mime):
        raise FileError(
            "UNSUPPORTED_FILE_TYPE",
            "Тип файла не разрешён для этой категории",
            415,
        )

    content_type = detected_mime or upload.content_type or "application/octet-stream"
    original_filename = _sanitize_filename(upload.filename)
    scope = str(effective_org_id) if effective_org_id is not None else str(user.id)
    storage_key = _build_storage_key(policy, scope, detected_ext)

    try:
        await storage.upload_object(storage_key, content, content_type)
    except StorageError as exc:
        raise FileError("STORAGE_UNAVAILABLE", "Хранилище недоступно", 502) from exc

    file = File(
        storage_key=storage_key,
        bucket=settings.s3_bucket,
        category=category,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=len(content),
        checksum_sha256=hashlib.sha256(content).hexdigest(),
        is_attached=False,
        organization_id=effective_org_id,
        owner_user_id=user.id,
    )
    session.add(file)
    await session.flush()
    logger.info(
        "file_uploaded",
        file_id=str(file.id),
        category=category.value,
        size_bytes=file.size_bytes,
        org_id=str(effective_org_id) if effective_org_id else None,
    )
    return file


async def presigned_url_for(file: File) -> tuple[str, datetime]:
    """Свежий presigned GET URL и момент его истечения."""
    try:
        url = await storage.generate_presigned_get(file.storage_key, file.original_filename)
    except StorageError as exc:
        raise FileError("STORAGE_UNAVAILABLE", "Хранилище недоступно", 502) from exc
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.s3_presign_expire_seconds)
    return url, expires_at


async def presigned_urls_for(
    files: list[File],
) -> dict[uuid.UUID, tuple[str | None, datetime | None]]:
    """Свежие presigned URL для пачки файлов: {file_id: (url, url_expires_at)}.

    Без N+1 (один S3-клиент на всю пачку). При сбое storage деградирует до
    `(None, None)` для всех — вызывающий отдаёт фото с `url=null`, не 502."""
    if not files:
        return {}
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.s3_presign_expire_seconds)
    try:
        url_by_key = await storage.generate_presigned_get_many(
            [(f.storage_key, f.original_filename) for f in files]
        )
    except StorageError:
        return {f.id: (None, None) for f in files}
    result: dict[uuid.UUID, tuple[str | None, datetime | None]] = {}
    for f in files:
        url = url_by_key.get(f.storage_key)
        result[f.id] = (url, expires_at if url else None)
    return result


async def _get_file(session: AsyncSession, file_id: uuid.UUID) -> File:
    result = await session.execute(select(File).where(File.id == file_id))
    file = result.scalar_one_or_none()
    if file is None:
        raise FileError("FILE_NOT_FOUND", "Файл не найден", 404)
    return file


async def _ensure_can_read(session: AsyncSession, file: File, user: User) -> None:
    if file.owner_user_id == user.id or user.role == UserRole.super_admin:
        return
    if file.organization_id is not None:
        org = await get_organization(session, file.organization_id)
        if file.category == FileCategory.knowledge_base:
            await ensure_member(session, org, user.id, message="Нет доступа к файлу")
            return
        if file.category == FileCategory.checklist_photo:
            await ensure_admin_or_owner(session, org, user.id, message="Нет доступа к файлу")
            return
    raise AccessError("FORBIDDEN", "Нет доступа к файлу", 403)


async def _ensure_can_delete(session: AsyncSession, file: File, user: User) -> None:
    if file.owner_user_id == user.id or user.role == UserRole.super_admin:
        return
    if file.organization_id is not None:
        org = await get_organization(session, file.organization_id)
        await ensure_admin_or_owner(session, org, user.id, message="Нет прав на удаление файла")
        return
    raise AccessError("FORBIDDEN", "Нет прав на удаление файла", 403)


async def get_file_for_read(
    session: AsyncSession,
    file_id: uuid.UUID,
    user: User,
) -> File:
    file = await _get_file(session, file_id)
    await _ensure_can_read(session, file, user)
    return file


async def delete_file(
    session: AsyncSession,
    file_id: uuid.UUID,
    user: User,
) -> None:
    """Удаляет объект из storage и строку реестра.

    Привязанный файл (`is_attached=true`) удалять нельзя — сначала отвязать со
    стороны фичи-потребителя (или каскад через её `ON DELETE`)."""
    file = await _get_file(session, file_id)
    await _ensure_can_delete(session, file, user)
    if file.is_attached:
        raise FileError(
            "FILE_IN_USE",
            "Файл привязан к сущности — сначала отвяжите его",
            409,
        )

    # Ошибку удаления объекта логируем, строку всё равно убираем: осиротевший
    # объект подберёт lifecycle-политика бакета.
    try:
        await storage.delete_object(file.storage_key)
    except StorageError as exc:
        logger.warning("file_object_delete_failed", file_id=str(file_id), error=str(exc))

    await session.delete(file)
    await session.flush()
    logger.info("file_deleted", file_id=str(file_id))
