"""Rate-limit для auth-эндпоинтов (slowapi).

Ключ лимита — IP клиента (`utils.request.get_client_ip`, учитывает
`X-Forwarded-For` за Caddy). Счётчики хранятся в Redis (распределённый лимит
при нескольких репликах API); в тестах хранилище переключается на `memory://`
через ENV `RATE_LIMIT_STORAGE_URI`. Пороги берутся из `Settings` (ENV).

Сам объект `limiter` подключается к приложению в `main.py`
(`app.state.limiter`), а декоратор `@limiter.limit(...)` навешивается на
эндпоинты в `api/v1/auth.py`.
"""

from slowapi import Limiter

from src.app.core.config import get_settings
from src.app.utils.request import get_client_ip

settings = get_settings()

limiter = Limiter(
    key_func=get_client_ip,
    storage_uri=settings.rate_limit_storage_uri or settings.redis_url,
    enabled=settings.rate_limit_enabled,
)
