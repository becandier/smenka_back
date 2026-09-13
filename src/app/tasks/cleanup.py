import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select, update

from src.app.core import storage
from src.app.core.celery_app import celery_app
from src.app.core.config import get_settings
from src.app.core.database import get_sync_session
from src.app.core.logging import get_logger
from src.app.core.storage import StorageError
from src.app.models.file import File, FileCategory
from src.app.models.user import RefreshToken, VerificationCode

logger = get_logger(__name__)
settings = get_settings()


@celery_app.task(name="cleanup_expired_tokens")
def cleanup_expired_tokens() -> None:
    """Delete expired/revoked refresh tokens and expired verification codes."""
    with get_sync_session() as session:
        now = datetime.now(UTC)

        # Delete expired or revoked refresh tokens
        tokens_result = cast(
            "CursorResult[Any]",
            session.execute(
                delete(RefreshToken).where(
                    (RefreshToken.expires_at < now) | (RefreshToken.revoked.is_(True))
                )
            ),
        )
        tokens_deleted = tokens_result.rowcount

        # Delete expired verification codes
        codes_result = cast(
            "CursorResult[Any]",
            session.execute(delete(VerificationCode).where(VerificationCode.expires_at < now)),
        )
        codes_deleted = codes_result.rowcount

        if tokens_deleted > 0 or codes_deleted > 0:
            logger.info(
                "expired_data_cleaned",
                tokens_deleted=tokens_deleted,
                codes_deleted=codes_deleted,
            )


async def _adelete_objects_report(keys: list[str]) -> tuple[list[str], list[str]]:
    """Пытается удалить каждый объект storage; возвращает (succeeded_keys, failed_keys).

    DeleteObject идемпотентен — «объекта уже нет» тоже считается успехом (не
    поднимает StorageError). Используется и `cleanup_orphan_files`, и
    `purge_expired_checklist_photos` — обеим нужно знать, какие именно ключи
    реально ушли из storage, чтобы удалять/помечать только их."""
    succeeded: list[str] = []
    failed: list[str] = []
    for key in keys:
        try:
            await storage.delete_object(key)
        except StorageError as exc:
            logger.warning("storage_object_delete_failed", key=key, error=str(exc))
            failed.append(key)
        else:
            succeeded.append(key)
    return succeeded, failed


def _delete_objects_report(keys: list[str]) -> tuple[list[str], list[str]]:
    """Sync-мост к async-удалению: Celery-воркер синхронный, своего event loop
    нет — поднимаем разовый через asyncio.run."""
    return asyncio.run(_adelete_objects_report(keys))


# Батчи выборки сирот/кандидатов на очистку: LIMIT + FOR UPDATE SKIP LOCKED,
# не один неограниченный SELECT (несколько воркеров/тиков не конкурируют за
# одни и те же строки). Потолок батчей на один запуск — защита от того, чтобы
# большой backlog не занял воркер на неопределённое время; остаток уходит в
# следующий запуск (расписание — ежечасно/ежедневно, см. core/celery_app.py).
_ORPHAN_BATCH_SIZE = 500
_ORPHAN_MAX_BATCHES = 20


@celery_app.task(name="cleanup_orphan_files")
def cleanup_orphan_files() -> None:
    """Удаляет файлы-сироты: непривязанные (`is_attached=false`) и старше
    `ORPHAN_FILE_TTL_HOURS`. Строка `files` удаляется, только если объект в
    storage удалён успешно (или уже отсутствовал) — при `StorageError` строка
    остаётся, чтобы следующий часовой запуск повторил попытку (иначе объект
    «теряется» в бакете навсегда, а строка о нём уже исчезла — этот баг чинит
    checklist_photo_retention/backend.md «Исправление cleanup_orphan_files»)."""
    cutoff = datetime.now(UTC) - timedelta(hours=settings.orphan_file_ttl_hours)
    deleted = 0
    with get_sync_session() as session:
        for _ in range(_ORPHAN_MAX_BATCHES):
            batch = list(
                session.execute(
                    select(File)
                    .where(
                        File.is_attached.is_(False),
                        File.created_at < cutoff,
                    )
                    .order_by(File.created_at)
                    .limit(_ORPHAN_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            if not batch:
                break

            by_key = {f.storage_key: f for f in batch}
            succeeded_keys, _failed_keys = _delete_objects_report(list(by_key))
            if succeeded_keys:
                ids = [by_key[key].id for key in succeeded_keys]
                session.execute(delete(File).where(File.id.in_(ids)))
                deleted += len(ids)
            session.commit()

            if len(batch) < _ORPHAN_BATCH_SIZE:
                break
    if deleted:
        logger.info("orphan_files_cleaned", count=deleted)


_PURGE_BATCH_SIZE = 500
_PURGE_MAX_BATCHES = 20


@celery_app.task(name="purge_expired_checklist_photos")
def purge_expired_checklist_photos() -> None:
    """checklist_photo_retention: удаляет ОБЪЕКТ S3 (не строку) привязанных фото
    чек-листов старше `CHECKLIST_PHOTO_RETENTION_DAYS` от `files.created_at`.

    Строки `files`/`checklist_item_photos` остаются нетронутыми — история
    чек-листа (кто/когда/где) не меняется, только `files.purged_at`
    проставляется. `CHECKLIST_PHOTO_RETENTION_DAYS=0` — задача выключена.
    Кандидаты — `category=checklist_photo AND is_attached=true AND
    purged_at IS NULL AND created_at < cutoff`, батчами по `_PURGE_BATCH_SIZE`
    с `FOR UPDATE SKIP LOCKED`, не больше `_PURGE_MAX_BATCHES` за запуск —
    остаток уйдёт в следующий (беат — ежедневно 02:00 UTC). `StorageError` на
    конкретном файле не мешает остальным батчам — `purged_at` для него просто
    не проставляется, и следующий запуск повторит попытку."""
    retention_days = settings.checklist_photo_retention_days
    if retention_days <= 0:
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    purged = 0
    failed = 0
    with get_sync_session() as session:
        for _ in range(_PURGE_MAX_BATCHES):
            batch = list(
                session.execute(
                    select(File)
                    .where(
                        File.category == FileCategory.checklist_photo,
                        File.is_attached.is_(True),
                        File.purged_at.is_(None),
                        File.created_at < cutoff,
                    )
                    .order_by(File.created_at)
                    .limit(_PURGE_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
                .scalars()
                .all()
            )
            if not batch:
                break

            by_key = {f.storage_key: f for f in batch}
            succeeded_keys, failed_keys = _delete_objects_report(list(by_key))
            if succeeded_keys:
                ids: list[uuid.UUID] = [by_key[key].id for key in succeeded_keys]
                session.execute(
                    update(File).where(File.id.in_(ids)).values(purged_at=datetime.now(UTC))
                )
                purged += len(ids)
            failed += len(failed_keys)
            session.commit()

            if len(batch) < _PURGE_BATCH_SIZE:
                break

    logger.info(
        "checklist_photos_purged",
        purged=purged,
        failed=failed,
        retention_days=retention_days,
    )
