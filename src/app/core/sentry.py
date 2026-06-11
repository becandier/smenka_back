"""Инициализация Sentry (error-tracking).

Включается только при заданном `SENTRY_DSN`. При пустом DSN Sentry полностью
выключен — dev/CI/тесты работают без сети. PII не отправляется
(`send_default_pii=False`), тела запросов (пароли/коды/токены) не захватываются
(`max_request_body_size="never"`).
"""

import sentry_sdk

from src.app.core.config import get_settings
from src.app.core.logging import get_logger

logger = get_logger(__name__)


def init_sentry() -> None:
    """Инициализировать Sentry, если задан DSN. Иначе — no-op."""
    settings = get_settings()
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment or settings.app_env,
        release=settings.sentry_release or None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
    )
    logger.info(
        "sentry_initialized",
        environment=settings.sentry_environment or settings.app_env,
    )
