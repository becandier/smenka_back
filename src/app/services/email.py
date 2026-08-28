"""Доставка кодов подтверждения email — граница между вызывающим кодом и провайдером.

Третий провайдер подряд (Яндекс-SMTP → Loops → SendPulse) — оба предыдущих
отвалились не по нашей вине (антиспам, блокировка IP), поэтому транспорт здесь
явно **сменный**: `EmailProvider` — протокол одного метода, `_PROVIDERS` —
реестр реализаций по значению `settings.email_provider`. Следующий переезд —
это новый файл `email_<provider>.py` + одна строка в `_PROVIDERS` ниже;
`deliver_verification_code` и её вызывающие (`api/v1/auth.py`) не меняются.

Режимы (флаг — `settings.email_enabled`, зависит от провайдера и его кредов):
- **выключен** (dev/CI/тесты): письма не шлём, код возвращается вызывающему
  для отдачи в ответе/логах. Живой провайдер локалке и тестам не нужен.
- **включён** (прод): код уходит письмом через провайдера, в ответе/логах кода нет.
"""

from collections.abc import Callable

from src.app.core.config import Settings, get_settings
from src.app.core.logging import get_logger
from src.app.services.auth import AuthError
from src.app.services.email_provider import EmailDeliveryError, EmailProvider
from src.app.services.email_sendpulse import SendPulseEmailProvider

logger = get_logger(__name__)
settings = get_settings()

_EMAIL_SEND_FAILED_MESSAGE = "Не удалось отправить письмо с кодом. Запросите код повторно."

# Реестр провайдеров по `settings.email_provider`. Новый провайдер = новый
# файл `email_<provider>.py` с классом, реализующим `EmailProvider`, + строка тут.
# Значение — конструктор (класс), а не сам протокол: у EmailProvider нет
# декларированного __init__, поэтому фабрики типизированы как Callable.
_PROVIDERS: dict[str, Callable[[Settings], EmailProvider]] = {
    "sendpulse": SendPulseEmailProvider,
}


def _build_provider(current_settings: Settings) -> EmailProvider | None:
    """`None` — известного провайдера нет (`email_provider="none"` или опечатка в
    значении); в этом случае `email_enabled` уже `False`, отправка не вызывается."""
    provider_factory = _PROVIDERS.get(current_settings.email_provider)
    if provider_factory is None:
        return None
    return provider_factory(current_settings)


# Провайдер — синглтон процесса: держит кэш OAuth-токена в памяти между
# вызовами (см. email_sendpulse.SendPulseEmailProvider), пересоздавать на
# каждое письмо нельзя — это заново запросит токен у провайдера.
_provider = _build_provider(settings)


async def deliver_verification_code(to_email: str, code: str) -> str | None:
    """Доставить пользователю код подтверждения.

    Отправка выключена → возвращает `code` (его кладут в ответ как раньше).
    Отправка включена → шлёт письмо через активного провайдера и возвращает
    `None` (код в ответе не отдаём). Ошибка отправки при включённой отправке →
    `AuthError("EMAIL_SEND_FAILED")` (пользователь и код уже сохранены —
    он сможет повторить через `resend-code`).
    """
    if not settings.email_enabled:
        return code

    if _provider is None:
        # Защитный случай: email_enabled=True обязан подразумевать known-провайдер
        # (см. Settings.email_enabled) — но не полагаемся на это молча.
        logger.error(
            "verification_email_failed", email=to_email, error="no email provider configured"
        )
        raise AuthError("EMAIL_SEND_FAILED", _EMAIL_SEND_FAILED_MESSAGE, 502)

    try:
        await _provider.send_verification_code(
            to_email=to_email,
            code=code,
            ttl_minutes=settings.verification_code_expire_minutes,
        )
    except EmailDeliveryError as exc:
        # repr, а не тело письма/код — в логах кода быть не должно.
        logger.error("verification_email_failed", email=to_email, error=repr(exc))
        raise AuthError("EMAIL_SEND_FAILED", _EMAIL_SEND_FAILED_MESSAGE, 502) from exc

    logger.info("verification_email_sent", email=to_email)
    return None
