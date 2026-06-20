import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, select

from src.app.core import storage
from src.app.core.celery_app import celery_app
from src.app.core.config import get_settings
from src.app.core.database import get_sync_session
from src.app.core.logging import get_logger
from src.app.core.storage import StorageError
from src.app.models.file import File
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


async def _adelete_objects(keys: list[str]) -> None:
    for key in keys:
        try:
            await storage.delete_object(key)
        except StorageError as exc:
            logger.warning("orphan_object_delete_failed", key=key, error=str(exc))


def _delete_orphan_objects(keys: list[str]) -> None:
    """Sync-мост к async-удалению объектов: Celery-воркер синхронный, своего
    event loop нет — поднимаем разовый через asyncio.run."""
    asyncio.run(_adelete_objects(keys))


@celery_app.task(name="cleanup_orphan_files")
def cleanup_orphan_files() -> None:
    """Удаляет файлы-сироты: непривязанные (`is_attached=false`) и старше
    `ORPHAN_FILE_TTL_HOURS`. Удаляет и объект в storage, и строку реестра."""
    cutoff = datetime.now(UTC) - timedelta(hours=settings.orphan_file_ttl_hours)
    with get_sync_session() as session:
        orphans = list(
            session.execute(
                select(File).where(
                    File.is_attached.is_(False),
                    File.created_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
        if not orphans:
            return

        _delete_orphan_objects([f.storage_key for f in orphans])
        for orphan in orphans:
            session.delete(orphan)
        logger.info("orphan_files_cleaned", count=len(orphans))
