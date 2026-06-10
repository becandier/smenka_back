from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # App
    app_env: str = "development"
    debug: bool = False
    secret_key: str = "change-me-in-production"  # noqa: S105 — dev-дефолт, в проде из env

    # CORS (источники для браузерной админки; CSV в env CORS_ORIGINS, пусто = [])
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

    # Shifts
    default_auto_finish_hours: int = 16

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Celery
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
