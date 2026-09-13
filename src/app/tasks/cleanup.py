import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, Select, delete, select, update
from sqlalchemy.orm import Session

from src.app.core import storage
from src.app.core.celery_app import celery_app
from src.app.core.config import get_settings
from src.app.core.database import get_sync_session
from src.app.core.logging import get_logger
from src.app.core.storage import ObjectSummary, StorageError
from src.app.models.file import File, FileCategory
from src.app.models.organization import Organization
from src.app.models.user import RefreshToken, VerificationCode
from src.app.services.file_storage import CATEGORY_POLICIES

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
    checklist_photo_retention/backend.md «Исправление cleanup_orphan_files»).

    Если в батче были неудачные удаления, после коммита этого батча цикл
    останавливается (не крутит оставшиеся до `_ORPHAN_MAX_BATCHES`) — иначе при
    недоступном storage задача десятки раз подряд повторяет одни и те же
    неудачные ключи (сортировка по `created_at` возвращает их первыми же в
    следующем батче), впустую занимая воркер. Остаток батча и неудачные ключи
    подхватит следующий плановый запуск."""
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
            succeeded_keys, failed_keys = _delete_objects_report(list(by_key))
            if succeeded_keys:
                ids = [by_key[key].id for key in succeeded_keys]
                session.execute(delete(File).where(File.id.in_(ids)))
                deleted += len(ids)
            session.commit()

            if failed_keys:
                break

            if len(batch) < _ORPHAN_BATCH_SIZE:
                break
    if deleted:
        logger.info("orphan_files_cleaned", count=deleted)


_PURGE_BATCH_SIZE = 500
_PURGE_MAX_BATCHES = 20


def _purge_rule_batches(
    session: Session,
    extra_where: Sequence[ColumnElement[bool]],
) -> tuple[int, int]:
    """Общий батч-цикл ОДНОГО правила `purge_expired_files` поверх инварианта
    `is_attached=true AND purged_at IS NULL` (общий для всех правил — см.
    backend.md storage_housekeeping «Правила выполняются по очереди»).

    Механика батчей 1-в-1 унаследована от `checklist_photo_retention`
    (`purge_expired_checklist_photos`, до этой фичи — единственное правило):
    `LIMIT`+`FOR UPDATE SKIP LOCKED`, не больше `_PURGE_MAX_BATCHES` по
    `_PURGE_BATCH_SIZE`, commit после каждого батча, `purged_at` — только для
    успешно удалённых объектов. Если в батче были неудачные удаления, цикл
    останавливается после коммита ЭТОГО батча (не крутит оставшиеся до
    `_PURGE_MAX_BATCHES`) — иначе недоступный storage долбит одни и те же
    неудачные ключи (сортировка по `created_at` возвращает их первыми же в
    следующем батче), впустую занимая воркер. Остаток и неудачные ключи
    подхватит следующий плановый запуск. `StorageError` останавливает только
    ЭТО правило — вызывающий код переходит к следующему."""
    purged = 0
    failed = 0
    for _ in range(_PURGE_MAX_BATCHES):
        stmt: Select[tuple[File]] = (
            select(File)
            .where(
                File.is_attached.is_(True),
                File.purged_at.is_(None),
                *extra_where,
            )
            .order_by(File.created_at)
            .limit(_PURGE_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
        batch = list(session.execute(stmt).scalars().all())
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

        if failed_keys:
            break

        if len(batch) < _PURGE_BATCH_SIZE:
            break

    return purged, failed


@celery_app.task(name="purge_expired_files")
def purge_expired_files() -> None:
    """storage_housekeeping: заменяет `purge_expired_checklist_photos` — одна
    задача с тремя правилами удаления ОБЪЕКТОВ S3 (не строк `files`) вместо
    нескольких похожих задач (beat, ежедневно 02:00 UTC). Правила выполняются
    ПО ОЧЕРЕДИ в фиксированном порядке (см. таблицу ниже) — файл, подходящий
    под несколько правил, обрабатывается первым же и повторно не выбирается
    (после обработки у него уже стоит `purged_at`). Все правила требуют общий
    `is_attached=true AND purged_at IS NULL`, поверх него — свои кандидаты:

    1. `checklist_photo` — `category=checklist_photo AND created_at < now -
       CHECKLIST_PHOTO_RETENTION_DAYS`.
    2. `shift_geo_photo` — `category=shift_geo_photo AND created_at < now -
       SHIFT_GEO_PHOTO_RETENTION_DAYS`.
    3. `deleted_organization` — `organization_id` принадлежит организации с
       `is_deleted=true AND deleted_at < now - DELETED_ORG_FILE_RETENTION_DAYS`
       (любая категория).

    Правило с `retention_days <= 0` пропускается целиком (не выполняет ни одного
    батча). Строки `files`/`checklist_item_photos` не удаляются — только объект
    (`purged_at` проставляется). Непривязанные файлы по-прежнему удаляет
    `cleanup_orphan_files` — сюда они не попадают ни при каком правиле."""
    now = datetime.now(UTC)
    checklist_days = settings.checklist_photo_retention_days
    geo_days = settings.shift_geo_photo_retention_days
    org_days = settings.deleted_org_file_retention_days

    checklist_purged = checklist_failed = 0
    geo_purged = geo_failed = 0
    org_purged = org_failed = 0

    with get_sync_session() as session:
        if checklist_days > 0:
            cutoff = now - timedelta(days=checklist_days)
            checklist_purged, checklist_failed = _purge_rule_batches(
                session,
                [File.category == FileCategory.checklist_photo, File.created_at < cutoff],
            )

        if geo_days > 0:
            cutoff = now - timedelta(days=geo_days)
            geo_purged, geo_failed = _purge_rule_batches(
                session,
                [File.category == FileCategory.shift_geo_photo, File.created_at < cutoff],
            )

        if org_days > 0:
            org_cutoff = now - timedelta(days=org_days)
            deleted_org_ids = select(Organization.id).where(
                Organization.is_deleted.is_(True),
                Organization.deleted_at.is_not(None),
                Organization.deleted_at < org_cutoff,
            )
            org_purged, org_failed = _purge_rule_batches(
                session,
                [File.organization_id.in_(deleted_org_ids)],
            )

    logger.info(
        "files_purged",
        checklist_photo_purged=checklist_purged,
        checklist_photo_failed=checklist_failed,
        checklist_photo_retention_days=checklist_days,
        shift_geo_photo_purged=geo_purged,
        shift_geo_photo_failed=geo_failed,
        shift_geo_photo_retention_days=geo_days,
        deleted_organization_purged=org_purged,
        deleted_organization_failed=org_failed,
        deleted_org_file_retention_days=org_days,
    )


_RECONCILE_PAGE_SIZE = 1000


def _list_objects_page(
    prefix: str, continuation_token: str | None
) -> tuple[list[ObjectSummary], str | None]:
    """Sync-мост к постраничному листингу S3 (тот же паттерн, что и
    `_delete_objects_report`/`_adelete_objects_report`: Celery-воркер
    синхронный, своего event loop нет — поднимаем разовый через `asyncio.run`
    на каждую страницу)."""
    return asyncio.run(storage.list_objects_page(prefix, continuation_token, _RECONCILE_PAGE_SIZE))


def _find_orphan_candidates(
    session: Session, page: list[ObjectSummary], cutoff: datetime
) -> list[ObjectSummary]:
    """Из одной страницы листинга S3 выбирает кандидатов на удаление:
    `LastModified` старше `cutoff` (защита загрузок, чей `put_object` уже
    прошёл, а строка `files` ещё не закоммичена) И (нет строки `files` с таким
    `storage_key`, ИЛИ строка есть, но `purged_at IS NOT NULL` — объект пережил
    удаление). Сопоставление с БД — одним запросом на страницу (до
    `_RECONCILE_PAGE_SIZE` ключей), без загрузки всей таблицы `files`."""
    old_enough = [obj for obj in page if obj.last_modified < cutoff]
    if not old_enough:
        return []
    keys = [obj.key for obj in old_enough]
    rows = session.execute(
        select(File.storage_key, File.purged_at).where(File.storage_key.in_(keys))
    ).all()
    purged_at_by_key = {row.storage_key: row.purged_at for row in rows}
    return [
        obj
        for obj in old_enough
        if obj.key not in purged_at_by_key or purged_at_by_key[obj.key] is not None
    ]


@celery_app.task(name="reconcile_storage_objects")
def reconcile_storage_objects() -> None:
    """storage_housekeeping: еженедельная сверка «объекты S3 без строки в
    `files`» (beat, воскресенье 04:00 UTC). Они появляются, когда `delete_file`
    или падение коммита после `put_object` оставляют объект без записи —
    lifecycle-правил, на которые рассчитывал код, у Timeweb S3 нет.

    `STORAGE_RECONCILE_ENABLED=false` — задача выходит немедленно, S3 не
    опрашивается. Иначе — постранично (`ListObjectsV2` + continuation token)
    проходит ТОЛЬКО префиксы категорий из `CATEGORY_POLICIES`
    (`checklist-photos/`, `knowledge-base/`, `shift-geo-photos/`, `avatars/`,
    `other/`) — `db-backups/` и всё вне этих префиксов не трогает никогда.

    Предохранитель: если кандидатов на удаление больше
    `STORAGE_RECONCILE_MAX_DELETES` за весь запуск — не удаляется НИЧЕГО,
    пишется `error` `storage_reconcile_aborted` (массовое расхождение — это
    ошибка конфигурации: не тот бакет/БД, а не мусор; решает человек)."""
    if not settings.storage_reconcile_enabled:
        return

    cutoff = datetime.now(UTC) - timedelta(hours=settings.orphan_file_ttl_hours)
    scanned = 0
    candidates: list[ObjectSummary] = []

    with get_sync_session() as session:
        for policy in CATEGORY_POLICIES.values():
            token: str | None = None
            while True:
                page, token = _list_objects_page(policy.prefix, token)
                scanned += len(page)
                if page:
                    candidates.extend(_find_orphan_candidates(session, page, cutoff))
                if token is None:
                    break

    if len(candidates) > settings.storage_reconcile_max_deletes:
        logger.error(
            "storage_reconcile_aborted",
            candidates=len(candidates),
            max_deletes=settings.storage_reconcile_max_deletes,
        )
        return

    deleted = 0
    failed = 0
    bytes_freed = 0
    if candidates:
        by_key = {obj.key: obj for obj in candidates}
        succeeded_keys, failed_keys = _delete_objects_report(list(by_key))
        deleted = len(succeeded_keys)
        failed = len(failed_keys)
        bytes_freed = sum(by_key[key].size for key in succeeded_keys)

    logger.info(
        "storage_reconciled",
        scanned=scanned,
        candidates=len(candidates),
        deleted=deleted,
        failed=failed,
        bytes_freed=bytes_freed,
    )
