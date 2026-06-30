"""Отправка писем через SMTP (коды подтверждения email — smtp_email).

Режимы (флаг — непустой `SMTP_HOST`):
- **SMTP выключен** (dev/CI/тесты): письма не шлём, код возвращается вызывающему
  для отдачи в ответе/лога. Живой SMTP локалке и тестам не нужен.
- **SMTP включён** (прод): код уходит письмом, в ответе/логах кода нет.

Транспорт — `aiosmtplib` (async, не блокирует event loop). Порт 465 → implicit SSL
(`use_tls`), 587 → STARTTLS (`start_tls`); выбор по `SMTP_USE_SSL`.
"""

from email.headerregistry import Address
from email.message import EmailMessage

import aiosmtplib

from src.app.core.config import get_settings
from src.app.core.logging import get_logger
from src.app.services.auth import AuthError

logger = get_logger(__name__)
settings = get_settings()


async def deliver_verification_code(to_email: str, code: str) -> str | None:
    """Доставить пользователю код подтверждения.

    SMTP выключен → возвращает `code` (его кладут в ответ как раньше).
    SMTP включён → шлёт письмо и возвращает `None` (код в ответе не отдаём).
    Ошибка отправки при включённом SMTP → `AuthError("EMAIL_SEND_FAILED")`
    (пользователь и код уже сохранены — он сможет повторить через `resend-code`).
    """
    if not settings.smtp_enabled:
        return code

    message = _build_code_message(to_email, code)
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username or None,
            password=settings.smtp_password or None,
            # Взаимоисключающие режимы TLS: implicit SSL (465) либо STARTTLS (587).
            use_tls=settings.smtp_use_ssl,
            start_tls=not settings.smtp_use_ssl,
            timeout=settings.smtp_timeout_seconds,
        )
    except (aiosmtplib.SMTPException, OSError) as exc:
        # repr, а не текст письма/код — в логах кода быть не должно.
        logger.error("verification_email_failed", email=to_email, error=repr(exc))
        raise AuthError(
            "EMAIL_SEND_FAILED",
            "Не удалось отправить письмо с кодом. Запросите код повторно.",
            502,
        ) from exc

    logger.info("verification_email_sent", email=to_email)
    return None


def _build_code_message(to_email: str, code: str) -> EmailMessage:
    """Собрать письмо с кодом (text + простой HTML)."""
    ttl_minutes = settings.verification_code_expire_minutes
    message = EmailMessage()
    message["Subject"] = "Код подтверждения Smenka"
    message["From"] = _from_address()
    message["To"] = to_email
    message.set_content(
        f"Ваш код подтверждения Smenka: {code}\n\n"
        f"Введите его в приложении, чтобы подтвердить email.\n"
        f"Код действует {ttl_minutes} минут.\n\n"
        f"Если вы не запрашивали код — просто проигнорируйте это письмо."
    )
    message.add_alternative(
        f"<html><body>"
        f"<p>Ваш код подтверждения <b>Smenka</b>:</p>"
        f'<p style="font-size:28px;font-weight:bold;letter-spacing:4px">{code}</p>'
        f"<p>Введите его в приложении, чтобы подтвердить email. "
        f"Код действует {ttl_minutes} минут.</p>"
        f'<p style="color:#888">Если вы не запрашивали код — '
        f"просто проигнорируйте это письмо.</p>"
        f"</body></html>",
        subtype="html",
    )
    return message


def _from_address() -> Address:
    """Адрес отправителя с отображаемым именем (`SMTP_FROM_NAME <SMTP_FROM>`)."""
    username, _, domain = settings.smtp_from.partition("@")
    return Address(display_name=settings.smtp_from_name, username=username, domain=domain)
