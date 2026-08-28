"""SendPulse REST API — доставка кода подтверждения (transactional email).

Аутентификация — OAuth `client_credentials` (`POST /oauth/access_token`),
access-токен живёт ~час; отправка — `POST /smtp/emails`, HTML в теле запроса
закодирован в base64 (обязательное требование SendPulse). Токен кэшируется в
памяти инстанса (провайдер — синглтон процесса, см. `email.py`) и
обновляется по истечении TTL или при HTTP 401 от отправки — новый токен на
каждое письмо не запрашивается.

Документация (актуальна на момент реализации, живой SendPulse не дёргался):
https://sendpulse.com/integrations/api/smtp — формат `POST /smtp/emails`
(`email.html`/`email.text` обязательны, `html` — base64); OAuth-эндпоинт и
формат ответа токена — https://github.com/sendpulse/sendpulse-rest-api-php
(`POST /oauth/access_token` → `{access_token, token_type, expires_in}`).
"""

import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx

from src.app.core.config import Settings
from src.app.services.email_provider import EmailDeliveryError
from src.app.services.email_template import (
    SUBJECT,
    render_verification_code_html,
    render_verification_code_text,
)

_TOKEN_URL = "https://api.sendpulse.com/oauth/access_token"  # noqa: S105 — эндпоинт, не секрет
_SEND_URL = "https://api.sendpulse.com/smtp/emails"

# Обновлять токен чуть раньше формального истечения — запас на время самого
# HTTP-запроса отправки письма (не хотим словить 401 из-за секундной гонки).
_TOKEN_EXPIRY_MARGIN_SECONDS = 60.0


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float  # time.monotonic()


class SendPulseEmailProvider:
    """Реализация `email_provider.EmailProvider` для SendPulse."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: _CachedToken | None = None

    async def send_verification_code(self, *, to_email: str, code: str, ttl_minutes: int) -> None:
        payload = self._build_payload(to_email, code, ttl_minutes)

        response = await self._send(payload, force_new_token=False)
        if response.status_code == 401:
            # Токен протух или отозван между запросами — обновляем один раз и
            # повторяем, не роняя письмо из-за естественного истечения TTL.
            response = await self._send(payload, force_new_token=True)

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

    async def _send(self, payload: dict[str, Any], *, force_new_token: bool) -> httpx.Response:
        try:
            token = await self._get_token(force_refresh=force_new_token)
            return await _send_email_request(self._settings, token, payload)
        except httpx.HTTPError as exc:
            raise EmailDeliveryError(f"SendPulse HTTP error: {exc!r}") from exc

    async def _get_token(self, *, force_refresh: bool) -> str:
        now = time.monotonic()
        cached = self._token
        if not force_refresh and cached is not None and now < cached.expires_at:
            return cached.access_token

        try:
            data = await _fetch_access_token(self._settings)
        except httpx.HTTPError as exc:
            raise EmailDeliveryError(f"SendPulse token request failed: {exc!r}") from exc

        access_token = data.get("access_token")
        expires_in = data.get("expires_in")
        if not isinstance(access_token, str) or not isinstance(expires_in, int):
            raise EmailDeliveryError(f"SendPulse token response malformed: {data!r}")

        expires_at = now + max(expires_in - _TOKEN_EXPIRY_MARGIN_SECONDS, 0.0)
        self._token = _CachedToken(access_token=access_token, expires_at=expires_at)
        return access_token


async def _fetch_access_token(settings: Settings) -> dict[str, Any]:
    """Изолирована для мокания в тестах."""
    async with httpx.AsyncClient(timeout=settings.sendpulse_timeout_seconds) as client:
        response = await client.post(
            _TOKEN_URL,
            json={
                "grant_type": "client_credentials",
                "client_id": settings.sendpulse_api_id,
                "client_secret": settings.sendpulse_api_secret,
            },
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result


async def _send_email_request(
    settings: Settings, access_token: str, payload: dict[str, Any]
) -> httpx.Response:
    """Изолирована для мокания в тестах."""
    async with httpx.AsyncClient(timeout=settings.sendpulse_timeout_seconds) as client:
        return await client.post(
            _SEND_URL,
            headers={"Authorization": f"Bearer {access_token}"},
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
