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
    # Inline-стили по бренд-контракту v1.0 (почтовые клиенты не грузят внешний CSS).
    # Токены: paper #FFFFFF, line #E7EBF0, ink #1D2530, muted #6B7785, wash #EAF2FB,
    # blue #4A90D9. Радиусы: card 16, control 12.
    sans = "'Helvetica Neue', Helvetica, Arial, system-ui, sans-serif"
    mono = "ui-monospace, 'SF Mono', Menlo, Consolas, monospace"
    message.add_alternative(
        f"<html>"
        f'<body style="margin:0;padding:24px;background-color:#FFFFFF;'
        f'font-family:{sans};color:#1D2530;">'
        f'<div style="max-width:480px;margin:0 auto;padding:32px;'
        f"background-color:#FFFFFF;border:1px solid #E7EBF0;border-radius:16px;"
        f'font-family:{sans};">'
        f'<h1 style="margin:0 0 16px;font-size:26px;font-weight:600;'
        f'letter-spacing:-0.02em;color:#1D2530;">Smenka</h1>'
        f'<p style="margin:0 0 16px;font-size:17px;color:#6B7785;">'
        f"Ваш код подтверждения:</p>"
        f'<p style="margin:0 0 16px;padding:16px 24px;background-color:#EAF2FB;'
        f"border-radius:12px;text-align:center;font-family:{mono};"
        f'font-size:32px;font-weight:600;letter-spacing:8px;color:#4A90D9;">{code}</p>'
        f'<p style="margin:0 0 16px;font-size:17px;color:#6B7785;">'
        f"Введите его в приложении, чтобы подтвердить email. "
        f"Код действует {ttl_minutes} минут.</p>"
        f'<p style="margin:0;font-size:13px;color:#6B7785;">'
        f"Если вы не запрашивали код — просто проигнорируйте это письмо.</p>"
        f"</div>"
        f"</body></html>",
        subtype="html",
    )
    return message


def _from_address() -> Address:
    """Адрес отправителя с отображаемым именем (`SMTP_FROM_NAME <SMTP_FROM>`)."""
    username, _, domain = settings.smtp_from.partition("@")
    return Address(display_name=settings.smtp_from_name, username=username, domain=domain)
