from functools import lru_cache
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # App
    app_env: str = "development"
    debug: bool = False
    secret_key: str = "change-me-in-production"  # noqa: S105 — dev-дефолт, в проде из env

    # CORS (origins браузерных клиентов — админка и веб-мобилка; CSV в CORS_ORIGINS, пусто = [])
    # NoDecode отключает JSON-предпарсинг pydantic-settings, чтобы CSV-строка
    # дошла до валидатора ниже как есть.
    cors_origins: Annotated[list[str], NoDecode] = []

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v

    # Database
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "smenka"
    postgres_password: str = "smenka"  # noqa: S105 — dev-дефолт, в проде из env
    postgres_db: str = "smenka"

    # Auth
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 30

    # Verification
    verification_code_expire_minutes: int = 15
    verification_code_length: int = 4
    verification_code_cooldown_seconds: int = 30
    # Сколько неверных вводов кода допускается, прежде чем код «сжигается».
    max_code_attempts: int = 5

    # Shifts
    default_auto_finish_hours: int = 16

    # Rate limiting (slowapi, per-IP). Строки в формате limits: "5/minute;30/hour".
    # Хранилище счётчиков — Redis в проде (см. rate_limit_storage_uri), общий с Celery.
    rate_limit_enabled: bool = True
    # Пусто → берётся redis_url. В тестах переопределяется на "memory://".
    rate_limit_storage_uri: str = ""
    login_rate_limit: str = "5/minute;30/hour"
    verify_rate_limit: str = "10/minute;50/hour"
    resend_rate_limit: str = "3/minute;10/hour"
    register_rate_limit: str = "5/minute;20/hour"

    # Account lockout (Redis, ключ по email). После N неудачных логинов —
    # блокировка на account_lockout_minutes (TTL ключа).
    max_login_failures: int = 10
    account_lockout_minutes: int = 15

    # S3 / file storage (dev = MinIO, prod = managed S3; см. docs/tasks/file_storage).
    # Один контракт, разные бэкенды — переезд = смена env, без правок кода.
    # endpoint для SDK (внутренний адрес); пусто = AWS default endpoint.
    s3_endpoint_url: str = ""
    # хост в presigned URL (открывается с устройства клиента); пусто = s3_endpoint_url.
    s3_public_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "smenka-files"
    s3_use_ssl: bool = False
    # path-style адресация (нужно MinIO; для AWS — virtual-hosted).
    s3_force_path_style: bool = True
    # TTL presigned GET URL (короткий — клиент обновляет через GET /files/{id}).
    s3_presign_expire_seconds: int = 3600
    # Глобальный потолок размера загрузки (≥ максимума per-category политики).
    max_upload_size_mb: int = 50
    # Возраст удаления непривязанных файлов-сирот (Celery cleanup_orphan_files).
    orphan_file_ttl_hours: int = 24

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Celery
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # Sentry (error tracking). Пустой DSN = Sentry полностью выключен (dev/CI).
    sentry_dsn: str = ""
    sentry_environment: str = ""  # пусто → app_env
    sentry_release: str = ""  # версия образа/коммит, передаётся ENV при сборке
    sentry_traces_sample_rate: float = 0.0

    @property
    def s3_public_endpoint(self) -> str:
        """Хост для генерации presigned URL. Падает на внутренний endpoint,
        если публичный не задан (в managed-S3 оба совпадают)."""
        return self.s3_public_endpoint_url or self.s3_endpoint_url

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_url_sync(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @model_validator(mode="after")
    def _guard_production_secret(self) -> "Settings":
        # В проде запрещаем дефолтный SECRET_KEY (делегировано из devops-трека).
        if self.app_env == "production" and self.secret_key in (
            "",
            "change-me-in-production",
        ):
            msg = (
                "SECRET_KEY must be set to a non-default value in production "
                "(generate: openssl rand -hex 32)"
            )
            raise ValueError(msg)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
