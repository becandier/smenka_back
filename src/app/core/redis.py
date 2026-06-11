"""Асинхронный Redis-клиент приложения.

Отдельный от Celery-брокера клиент для прикладных нужд: блокировка аккаунта
по неудачным логинам (`services.lockout`). Хранилище rate-limit (slowapi) живёт
в том же Redis, но управляется библиотекой `limits` по URI (см. `core.rate_limit`).

Клиент ленивый и кэшируется. В тестах модульная переменная `_client`
подменяется на `fakeredis` (см. `tests/conftest.py`), сеть не требуется.
"""

import redis.asyncio as redis_asyncio

from src.app.core.config import get_settings

_client: redis_asyncio.Redis | None = None


def get_redis() -> redis_asyncio.Redis:
    """Вернуть общий асинхронный Redis-клиент (создаётся при первом обращении)."""
    global _client
    if _client is None:
        _client = redis_asyncio.from_url(  # type: ignore[no-untyped-call]
            get_settings().redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _client
