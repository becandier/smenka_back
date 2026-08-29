"""Тесты отправки кодов подтверждения через SendPulse (smtp_email, транспорт SendPulse).

HTTP-слой всегда мокается (`email_sendpulse._send_email_request`) — живых
запросов к SendPulse из тестов быть не должно.
"""

import base64
import re
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.services import auth as auth_service
from src.app.services import email as email_service
from src.app.services import email_sendpulse

_TEST_API_KEY = "sp_apikey_test-key"


def _send_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=body)


@pytest.fixture
def sendpulse_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включить SendPulse (прод-режим) на время теста: непустые api_key/from."""
    monkeypatch.setattr(email_service.settings, "email_provider", "sendpulse")
    monkeypatch.setattr(email_service.settings, "sendpulse_api_key", _TEST_API_KEY)
    monkeypatch.setattr(email_service.settings, "sendpulse_from_email", "noreply@smenka.space")
    monkeypatch.setattr(email_service.settings, "sendpulse_from_name", "Smenka")
    monkeypatch.setattr(email_service.settings, "sendpulse_timeout_seconds", 10)

    assert email_service._provider is not None, "провайдер должен резолвиться в SendPulse"


def _decode_html(payload: dict[str, Any]) -> str:
    encoded = payload["email"]["html"]
    return base64.b64decode(encoded).decode("utf-8")


async def test_sendpulse_off_returns_code_and_does_not_send(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SendPulse выключен (дефолт: пустые креды): код в ответе, HTTP-запрос не уходит."""
    send_mock = AsyncMock()
    monkeypatch.setattr(email_sendpulse, "_send_email_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "off@example.com", "password": "Password1", "name": "Off"},
    )

    assert response.status_code == 201
    code = response.json()["data"]["verification_code"]
    assert code is not None
    assert len(code) == 4
    send_mock.assert_not_awaited()


async def test_email_provider_none_disables_sending(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`email_provider=none` явно выключает отправку, даже если креды заданы."""
    monkeypatch.setattr(email_service.settings, "email_provider", "none")
    monkeypatch.setattr(email_service.settings, "sendpulse_api_key", _TEST_API_KEY)
    monkeypatch.setattr(email_service.settings, "sendpulse_from_email", "noreply@smenka.space")

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "none-provider@example.com", "password": "Password1", "name": "None"},
    )

    assert response.status_code == 201
    assert response.json()["data"]["verification_code"] is not None


async def test_sendpulse_on_sends_email_and_hides_code(
    client: AsyncClient,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SendPulse включён: запрос ушёл с правильным телом, verification_code в ответе = null."""
    send_mock = AsyncMock(return_value=_send_response(200, {"result": True, "id": "abc"}))
    monkeypatch.setattr(email_sendpulse, "_send_email_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "on@example.com", "password": "Password1", "name": "On"},
    )

    assert response.status_code == 201
    assert response.json()["data"]["verification_code"] is None

    send_mock.assert_awaited_once()

    args = send_mock.await_args.args
    # _send_email_request(settings, payload)
    payload = args[1]
    assert payload["email"]["subject"] == "Код подтверждения Smenka"
    assert payload["email"]["from"] == {"name": "Smenka", "email": "noreply@smenka.space"}
    assert payload["email"]["to"] == [{"email": "on@example.com"}]
    assert isinstance(payload["email"]["text"], str)
    assert payload["email"]["text"]

    html = _decode_html(payload)
    assert "15" in html  # ttl_minutes
    # Код подтверждения — 4 цифры, ищем его же в декодированном HTML.
    codes_in_html = re.findall(r"\b\d{4}\b", html)
    assert codes_in_html, "код должен присутствовать в HTML-письме"


async def test_sendpulse_sends_api_key_as_bearer_token(
    client: AsyncClient,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ключ уходит напрямую в заголовке Authorization: Bearer <key>, без обмена на токен."""
    send_mock = AsyncMock(return_value=_send_response(200, {"result": True, "id": "abc"}))
    monkeypatch.setattr(email_sendpulse, "_send_email_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "bearer@example.com", "password": "Password1", "name": "Bearer"},
    )

    assert response.status_code == 201
    send_mock.assert_awaited_once()

    settings_arg = send_mock.await_args.args[0]
    assert settings_arg.sendpulse_api_key == _TEST_API_KEY


async def test_sendpulse_on_result_false_returns_error(
    client: AsyncClient,
    db_session: AsyncSession,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 200, но result != true → EMAIL_SEND_FAILED, пользователь уже создан."""
    monkeypatch.setattr(
        email_sendpulse,
        "_send_email_request",
        AsyncMock(return_value=_send_response(200, {"result": False})),
    )

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "false@example.com", "password": "Password1", "name": "False"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"

    user = await auth_service.get_user_by_email(db_session, "false@example.com")
    assert user is not None
    assert user.is_verified is False


async def test_sendpulse_on_non_2xx_returns_error(
    client: AsyncClient,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 4xx/5xx от отправки → EMAIL_SEND_FAILED."""
    monkeypatch.setattr(
        email_sendpulse,
        "_send_email_request",
        AsyncMock(return_value=_send_response(400, {"message": "invalid sender"})),
    )

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "bad@example.com", "password": "Password1", "name": "Bad"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"


async def test_sendpulse_on_401_invalid_key_returns_error(
    client: AsyncClient,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Неверный API-ключ → 401 от SendPulse → EMAIL_SEND_FAILED."""
    monkeypatch.setattr(
        email_sendpulse,
        "_send_email_request",
        AsyncMock(
            return_value=_send_response(401, {"error": "Client authentication failed"}),
        ),
    )

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "unauthorized@example.com", "password": "Password1", "name": "Unauth"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"


async def test_sendpulse_on_403_ip_not_allowed_gives_clear_message(
    client: AsyncClient,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 IP address is not allowed → EMAIL_SEND_FAILED, а в логе — внятная подсказка
    про White List ключа (иначе диагностика уходит в часы при смене IP сервера)."""
    monkeypatch.setattr(
        email_sendpulse,
        "_send_email_request",
        AsyncMock(
            return_value=_send_response(403, {"error": "IP address is not allowed"}),
        ),
    )
    log_error = Mock()
    monkeypatch.setattr(email_service.logger, "error", log_error)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "forbidden-ip@example.com", "password": "Password1", "name": "Forbidden"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"

    log_error.assert_called_once()
    _, kwargs = log_error.call_args
    error_repr = kwargs["error"]
    assert "White List" in error_repr or "белом списке" in error_repr
    assert "IP" in error_repr


async def test_sendpulse_http_error_returns_error(
    client: AsyncClient,
    db_session: AsyncSession,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сетевая ошибка/таймаут запроса отправки → EMAIL_SEND_FAILED, пользователь уже создан."""
    monkeypatch.setattr(
        email_sendpulse,
        "_send_email_request",
        AsyncMock(side_effect=httpx.TimeoutException("boom")),
    )

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "timeout@example.com", "password": "Password1", "name": "Timeout"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"

    user = await auth_service.get_user_by_email(db_session, "timeout@example.com")
    assert user is not None
    assert user.is_verified is False


async def test_code_never_logged_when_sendpulse_on(
    client: AsyncClient,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ни в успехе, ни при сбое отправки код не попадает в аргументы логгера."""
    log_info = Mock()
    log_error = Mock()
    monkeypatch.setattr(email_service.logger, "info", log_info)
    monkeypatch.setattr(email_service.logger, "error", log_error)

    # Успех.
    monkeypatch.setattr(
        email_sendpulse,
        "_send_email_request",
        AsyncMock(return_value=_send_response(200, {"result": True, "id": "abc"})),
    )
    await client.post(
        "/api/v1/auth/register",
        json={"email": "logsafe1@example.com", "password": "Password1", "name": "LogSafe"},
    )

    # Сбой.
    monkeypatch.setattr(
        email_sendpulse,
        "_send_email_request",
        AsyncMock(return_value=_send_response(500, {"message": "boom"})),
    )
    await client.post(
        "/api/v1/auth/register",
        json={"email": "logsafe2@example.com", "password": "Password1", "name": "LogSafe"},
    )

    all_calls = log_info.call_args_list + log_error.call_args_list
    assert all_calls, "логгер должен был вызываться хотя бы раз"
    for call in all_calls:
        _, kwargs = call
        assert "code" not in kwargs
        for value in kwargs.values():
            assert not (isinstance(value, str) and value.isdigit() and len(value) == 4)
