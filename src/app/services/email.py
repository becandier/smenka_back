"""Отправка кодов подтверждения через Loops (transactional API — smtp_email).

Режимы (флаг — непустой `LOOPS_API_KEY`):
- **Loops выключен** (dev/CI/тесты): письма не шлём, код возвращается вызывающему
  для отдачи в ответе/логах. Живой Loops локалке и тестам не нужен.
- **Loops включён** (прод): код уходит письмом через HTTP API, в ответе/логах кода нет.

Транспорт — `httpx.AsyncClient` (async, не блокирует event loop). Шаблон
«Verification code» (`LOOPS_TRANSACTIONAL_ID`) — правки вёрстки идут из
`docs/email-templates/verification-code/index.mjml`, не отсюда.
"""

from typing import Any

import httpx

from src.app.core.config import get_settings
from src.app.core.logging import get_logger
from src.app.services.auth import AuthError

logger = get_logger(__name__)
settings = get_settings()

_EMAIL_SEND_FAILED_MESSAGE = "Не удалось отправить письмо с кодом. Запросите код повторно."


async def deliver_verification_code(to_email: str, code: str) -> str | None:
    """Доставить пользователю код подтверждения.

    Loops выключен → возвращает `code` (его кладут в ответ как раньше).
    Loops включён → шлёт письмо через HTTP API Loops и возвращает `None`
    (код в ответе не отдаём). Ошибка отправки при включённом Loops →
    `AuthError("EMAIL_SEND_FAILED")` (пользователь и код уже сохранены —
    он сможет повторить через `resend-code`).
    """
    if not settings.email_enabled:
        return code

    payload: dict[str, Any] = {
        "transactionalId": settings.loops_transactional_id,
        "email": to_email,
        "dataVariables": {
            "code": code,
            "ttlMinutes": settings.verification_code_expire_minutes,
        },
    }

    try:
        response = await _send_loops_request(payload)
    except httpx.HTTPError as exc:
        # repr, а не тело письма/код — в логах кода быть не должно.
        logger.error("verification_email_failed", email=to_email, error=repr(exc))
        raise AuthError("EMAIL_SEND_FAILED", _EMAIL_SEND_FAILED_MESSAGE, 502) from exc

    if not _is_success(response):
        # Тело ответа Loops содержит осмысленную диагностику (например,
        # «transactionalId not found» / «missing data variable») — без ключа и кода.
        logger.error(
            "verification_email_failed",
            email=to_email,
            status_code=response.status_code,
            body=response.text,
        )
        raise AuthError("EMAIL_SEND_FAILED", _EMAIL_SEND_FAILED_MESSAGE, 502)

    logger.info("verification_email_sent", email=to_email)
    return None


async def _send_loops_request(payload: dict[str, Any]) -> httpx.Response:
    """Изолирована для мокания в тестах (см. `oauth_tokens._fetch_jwks`)."""
    async with httpx.AsyncClient(timeout=settings.loops_timeout_seconds) as client:
        return await client.post(
            settings.loops_api_url,
            headers={"Authorization": f"Bearer {settings.loops_api_key}"},
            json=payload,
        )


def _is_success(response: httpx.Response) -> bool:
    """Успех — только HTTP 200 и тело `{"success": true}`."""
    if response.status_code != 200:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    return data.get("success") is True
