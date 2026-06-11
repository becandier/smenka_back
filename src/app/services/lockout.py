"""Блокировка аккаунта после серии неудачных логинов (Redis, по email).

Счётчик неудач хранится в Redis с TTL = окно блокировки и автоматически
истекает — отдельной таблицы в БД нет (ADR не требуется, см. backend.md).
Блокировка по email (а не по IP), чтобы атака с пула адресов всё равно упёрлась
в лимит на аккаунт; per-IP rate-limit (slowapi) работает параллельно.

Чтобы не создавать enumeration-оракул, счётчик инкрементится одинаково и для
существующего, и для несуществующего email — `login` отдаёт `ACCOUNT_LOCKED`
после N попыток в обоих случаях.
"""

from src.app.core.config import get_settings
from src.app.core.redis import get_redis

settings = get_settings()


def _key(email: str) -> str:
    return f"login_fail:{email.strip().lower()}"


async def is_locked(email: str) -> bool:
    """Заблокирован ли аккаунт сейчас (счётчик достиг порога и ещё не истёк)."""
    value = await get_redis().get(_key(email))
    return value is not None and int(value) >= settings.max_login_failures


async def register_failure(email: str) -> None:
    """Учесть неудачный вход: инкремент счётчика и (ре)установка TTL окна."""
    redis = get_redis()
    key = _key(email)
    count = await redis.incr(key)
    ttl_seconds = settings.account_lockout_minutes * 60
    # TTL ставим на первой неудаче (старт окна) и обновляем при достижении
    # порога (блокировка действует полное окно от последней попытки).
    if count == 1 or count >= settings.max_login_failures:
        await redis.expire(key, ttl_seconds)


async def reset(email: str) -> None:
    """Сбросить счётчик после успешного входа."""
    await get_redis().delete(_key(email))
