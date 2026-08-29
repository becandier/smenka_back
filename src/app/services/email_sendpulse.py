"""SendPulse REST API — доставка кода подтверждения (transactional email).

Аутентификация — статический API-ключ (`settings.sendpulse_api_key`, формат
`sp_apikey_<64 hex>`), выданный в личном кабинете SendPulse. Ключ передаётся
напрямую в заголовке `Authorization: Bearer <key>` при каждом запросе —
обмена на access-токен нет (в отличие от классического OAuth
`client_credentials` у SendPulse, этот ключ уже самодостаточен). Отправка —
`POST /smtp/emails`, HTML в теле запроса закодирован в base64 (обязательное
требование SendPulse).

Схема подтверждена живым запросом с прод-сервера 2026-08-27:
`GET https://api.sendpulse.com/smtp/senders` с этим заголовком → 200.
Неверный ключ → 401. Запрос с IP, не входящего в White List ключа (ограничение
включено в личном кабинете SendPulse) → 403 `{"error":"IP address is not
allowed"}` — реальный и вероятный сбой при смене IP прод-сервера, поэтому
разбирается отдельно понятным сообщением в `send_verification_code`.

Документация: https://sendpulse.com/integrations/api/smtp — формат
`POST /smtp/emails` (`email.html`/`email.text` обязательны, `html` — base64).
"""

import base64
from typing import Any

import httpx

from src.app.core.config import Settings
from src.app.services.email_provider import EmailDeliveryError
from src.app.services.email_template import (
    SUBJECT,
    render_verification_code_html,
    render_verification_code_text,
)

_SEND_URL = "https://api.sendpulse.com/smtp/emails"


class SendPulseEmailProvider:
    """Реализация `email_provider.EmailProvider` для SendPulse."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_verification_code(self, *, to_email: str, code: str, ttl_minutes: int) -> None:
        payload = self._build_payload(to_email, code, ttl_minutes)

        try:
            response = await _send_email_request(self._settings, payload)
        except httpx.HTTPError as exc:
            raise EmailDeliveryError(f"SendPulse HTTP error: {exc!r}") from exc

        if response.status_code == 403:
            # Наиболее вероятная причина именно этого статуса у SendPulse — IP
            # сервера не входит в White List API-ключа (ограничение включено
            # в личном кабинете). Без явного текста диагностика уходит в часы.
            raise EmailDeliveryError(
                "SendPulse отклонил запрос (403): IP-адрес сервера не в белом "
                "списке (White List) API-ключа SendPulse. Проверьте настройки "
                f"ключа в личном кабинете SendPulse. Тело ответа: {response.text}"
            )

        if not _is_success(response):
            raise EmailDeliveryError(
                f"SendPulse send failed: status={response.status_code} body={response.text}"
            )

    def _build_payload(self, to_email: str, code: str, ttl_minutes: int) -> dict[str, Any]:
        html = render_verification_code_html(code, ttl_minutes)
        text = render_verification_code_text(code, ttl_minutes)
        return {
            "email": {
                "html": base64.b64encode(html.encode("utf-8")).decode("ascii"),
                "text": text,
                "subject": SUBJECT,
                "from": {
                    "name": self._settings.sendpulse_from_name,
                    "email": self._settings.sendpulse_from_email,
                },
                "to": [{"email": to_email}],
            }
        }


async def _send_email_request(settings: Settings, payload: dict[str, Any]) -> httpx.Response:
    """Изолирована для мокания в тестах."""
    async with httpx.AsyncClient(timeout=settings.sendpulse_timeout_seconds) as client:
        return await client.post(
            _SEND_URL,
            headers={"Authorization": f"Bearer {settings.sendpulse_api_key}"},
            json=payload,
        )


def _is_success(response: httpx.Response) -> bool:
    """Успех — только HTTP 200 и тело `{"result": true, ...}`."""
    if response.status_code != 200:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    return bool(data.get("result") is True)
