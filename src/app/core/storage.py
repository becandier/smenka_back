"""S3-совместимый слой хранения (обёртка над aioboto3).

Единая точка работы с объектным хранилищем. Локально это MinIO, в проде —
managed S3 (AWS / Yandex Object Storage / Timeweb S3 — любой S3-совместимый).
Переезд = смена `S3_*` env, без правок кода.

Нюанс presigned + MinIO: бэкенд ходит в storage по внутреннему адресу
(`S3_ENDPOINT_URL`), но presigned URL должен открываться с устройства клиента,
где внутренний хост не резолвится. Поэтому presigned-ссылка генерируется
клиентом, настроенным на ПУБЛИЧНЫЙ endpoint (`S3_PUBLIC_ENDPOINT_URL`) — подпись
сразу считается от публичного хоста, переписывать строку не нужно. В managed-S3
оба адреса совпадают → no-op.
"""

import aioboto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from src.app.core.config import get_settings
from src.app.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()


class StorageError(Exception):
    """Ошибка объектного хранилища (S3 недоступен / отклонил операцию).

    Сервисный слой маппит её в STORAGE_UNAVAILABLE (502)."""


def _session() -> aioboto3.Session:
    return aioboto3.Session()


def _config() -> Config:
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "path" if settings.s3_force_path_style else "auto"},
    )


def _client_kwargs(*, public: bool) -> dict[str, object]:
    """Параметры клиента. `public=True` → клиент на публичном endpoint для
    генерации presigned URL; иначе внутренний endpoint для put/delete."""
    endpoint = settings.s3_public_endpoint if public else settings.s3_endpoint_url
    kwargs: dict[str, object] = {
        "region_name": settings.s3_region,
        "use_ssl": settings.s3_use_ssl,
        "config": _config(),
    }
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if settings.s3_access_key:
        kwargs["aws_access_key_id"] = settings.s3_access_key
        kwargs["aws_secret_access_key"] = settings.s3_secret_key
    return kwargs


async def upload_object(key: str, body: bytes, content_type: str) -> None:
    try:
        async with _session().client("s3", **_client_kwargs(public=False)) as client:
            await client.put_object(
                Bucket=settings.s3_bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
            )
    except (BotoCoreError, ClientError) as exc:
        logger.error("s3_put_failed", key=key, error=repr(exc))
        raise StorageError(str(exc)) from exc


async def generate_presigned_get(key: str, filename: str) -> str:
    """Подписанный URL на скачивание объекта с TTL `S3_PRESIGN_EXPIRE_SECONDS`.

    `filename` уходит в `Content-Disposition`, чтобы клиент скачал файл под
    исходным именем (значение уже санитизировано на стороне сервиса)."""
    try:
        async with _session().client("s3", **_client_kwargs(public=True)) as client:
            url = await client.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": settings.s3_bucket,
                    "Key": key,
                    "ResponseContentDisposition": f'inline; filename="{filename}"',
                },
                ExpiresIn=settings.s3_presign_expire_seconds,
            )
        return str(url)
    except (BotoCoreError, ClientError) as exc:
        logger.error("s3_presign_failed", key=key, error=repr(exc))
        raise StorageError(str(exc)) from exc


async def generate_presigned_get_many(items: list[tuple[str, str]]) -> dict[str, str]:
    """Подписать пачку (key, filename) одним клиентом → {key: url}.

    Подпись считается локально (без сетевых вызовов), поэтому один открытый
    клиент покрывает любое число ключей — это и убирает N+1 при отдаче галереи
    фото. При недоступности storage поднимается `StorageError` (вызывающий код
    решает, деградировать ли до `url=None`)."""
    if not items:
        return {}
    try:
        async with _session().client("s3", **_client_kwargs(public=True)) as client:
            result: dict[str, str] = {}
            for key, filename in items:
                url = await client.generate_presigned_url(
                    "get_object",
                    Params={
                        "Bucket": settings.s3_bucket,
                        "Key": key,
                        "ResponseContentDisposition": f'inline; filename="{filename}"',
                    },
                    ExpiresIn=settings.s3_presign_expire_seconds,
                )
                result[key] = str(url)
        return result
    except (BotoCoreError, ClientError) as exc:
        logger.error("s3_presign_many_failed", error=repr(exc))
        raise StorageError(str(exc)) from exc


async def delete_object(key: str) -> None:
    try:
        async with _session().client("s3", **_client_kwargs(public=False)) as client:
            await client.delete_object(Bucket=settings.s3_bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:
        logger.error("s3_delete_failed", key=key, error=repr(exc))
        raise StorageError(str(exc)) from exc


async def ensure_bucket() -> None:
    """Идемпотентно создаёт бакет — только для dev/MinIO.

    В проде бакет (и его политики) создаёт инфра, см. docs/tasks/file_storage/devops.md."""
    try:
        async with _session().client("s3", **_client_kwargs(public=False)) as client:
            try:
                await client.head_bucket(Bucket=settings.s3_bucket)
            except ClientError:
                await client.create_bucket(Bucket=settings.s3_bucket)
    except (BotoCoreError, ClientError) as exc:
        logger.error("s3_ensure_bucket_failed", error=repr(exc))
        raise StorageError(str(exc)) from exc
