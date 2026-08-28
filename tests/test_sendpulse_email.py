"""Тесты отправки кодов подтверждения через SendPulse (smtp_email, транспорт SendPulse).

HTTP-слой всегда мокается (`email_sendpulse._fetch_access_token` /
`email_sendpulse._send_email_request`) — живых запросов к SendPulse из тестов
быть не должно.
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


def _token_payload(
    access_token: str = "test-access-token",  # noqa: S107 — тестовый фикстурный токен
    expires_in: int = 3600,
) -> dict[str, Any]:
    return {"access_token": access_token, "token_type": "Bearer", "expires_in": expires_in}


def _send_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=body)


@pytest.fixture
def sendpulse_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включить SendPulse (прод-режим) на время теста: непустые api_id/secret/from."""
    monkeypatch.setattr(email_service.settings, "email_provider", "sendpulse")
    monkeypatch.setattr(email_service.settings, "sendpulse_api_id", "test-id")
    monkeypatch.setattr(email_service.settings, "sendpulse_api_secret", "test-secret")
    monkeypatch.setattr(email_service.settings, "sendpulse_from_email", "noreply@smenka.space")
    monkeypatch.setattr(email_service.settings, "sendpulse_from_name", "Smenka")
    monkeypatch.setattr(email_service.settings, "sendpulse_timeout_seconds", 10)

    assert email_service._provider is not None, "провайдер должен резолвиться в SendPulse"
    # Сбрасываем кэш токена — провайдер живёт весь тестовый процесс синглтоном,
    # предыдущий тест мог оставить в нём токен/просроченный кэш.
    email_service._provider._token = None  # type: ignore[attr-defined]


def _decode_html(payload: dict[str, Any]) -> str:
    encoded = payload["email"]["html"]
    return base64.b64decode(encoded).decode("utf-8")


async def test_sendpulse_off_returns_code_and_does_not_send(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SendPulse выключен (дефолт: пустые креды): код в ответе, HTTP-запрос не уходит."""
    send_mock = AsyncMock()
    fetch_mock = AsyncMock()
    monkeypatch.setattr(email_sendpulse, "_send_email_request", send_mock)
    monkeypatch.setattr(email_sendpulse, "_fetch_access_token", fetch_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "off@example.com", "password": "Password1", "name": "Off"},
    )

    assert response.status_code == 201
    code = response.json()["data"]["verification_code"]
    assert code is not None
    assert len(code) == 4
    send_mock.assert_not_awaited()
    fetch_mock.assert_not_awaited()


async def test_email_provider_none_disables_sending(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`email_provider=none` явно выключает отправку, даже если креды заданы."""
    monkeypatch.setattr(email_service.settings, "email_provider", "none")
    monkeypatch.setattr(email_service.settings, "sendpulse_api_id", "test-id")
    monkeypatch.setattr(email_service.settings, "sendpulse_api_secret", "test-secret")
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
    fetch_mock = AsyncMock(return_value=_token_payload())
    send_mock = AsyncMock(return_value=_send_response(200, {"result": True, "id": "abc"}))
    monkeypatch.setattr(email_sendpulse, "_fetch_access_token", fetch_mock)
    monkeypatch.setattr(email_sendpulse, "_send_email_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "on@example.com", "password": "Password1", "name": "On"},
    )

    assert response.status_code == 201
    assert response.json()["data"]["verification_code"] is None

    fetch_mock.assert_awaited_once()
    send_mock.assert_awaited_once()

    args = send_mock.await_args.args
    # _send_email_request(settings, access_token, payload)
    assert args[1] == "test-access-token"
    payload = args[2]
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


async def test_sendpulse_on_result_false_returns_error(
    client: AsyncClient,
    db_session: AsyncSession,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 200, но result != true → EMAIL_SEND_FAILED, пользователь уже создан."""
    monkeypatch.setattr(
        email_sendpulse, "_fetch_access_token", AsyncMock(return_value=_token_payload())
    )
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
        email_sendpulse, "_fetch_access_token", AsyncMock(return_value=_token_payload())
    )
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


async def test_sendpulse_token_request_failure_returns_error_and_persists_user(
    client: AsyncClient,
    db_session: AsyncSession,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой получения OAuth-токена → EMAIL_SEND_FAILED, но пользователь уже создан в БД."""
    monkeypatch.setattr(
        email_sendpulse,
        "_fetch_access_token",
        AsyncMock(side_effect=httpx.TimeoutException("boom")),
    )
    send_mock = AsyncMock()
    monkeypatch.setattr(email_sendpulse, "_send_email_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "timeout@example.com", "password": "Password1", "name": "Timeout"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"
    send_mock.assert_not_awaited()

    user = await auth_service.get_user_by_email(db_session, "timeout@example.com")
    assert user is not None
    assert user.is_verified is False


async def test_sendpulse_token_reused_between_sends(
    client: AsyncClient,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Токен запрашивается один раз и переиспользуется между отправками (register + resend)."""
    fetch_mock = AsyncMock(return_value=_token_payload())
    send_mock = AsyncMock(return_value=_send_response(200, {"result": True, "id": "abc"}))
    monkeypatch.setattr(email_sendpulse, "_fetch_access_token", fetch_mock)
    monkeypatch.setattr(email_sendpulse, "_send_email_request", send_mock)

    await client.post(
        "/api/v1/auth/register",
        json={"email": "resend@example.com", "password": "Password1", "name": "Resend"},
    )
    assert fetch_mock.await_count == 1
    assert send_mock.await_count == 1

    # Сбрасываем cooldown, чтобы resend прошёл (а не упёрся в 429 COOLDOWN).
    monkeypatch.setattr(email_service.settings, "verification_code_cooldown_seconds", 0)

    response = await client.post(
        "/api/v1/auth/resend-code",
        json={"email": "resend@example.com"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["verification_code"] is None
    # Второе письмо ушло, но токен запрошен по-прежнему один раз — переиспользован из кэша.
    assert send_mock.await_count == 2
    assert fetch_mock.await_count == 1
    # Оба запроса на отправку использовали один и тот же кэшированный токен.
    first_token = send_mock.await_args_list[0].args[1]
    second_token = send_mock.await_args_list[1].args[1]
    assert first_token == second_token == "test-access-token"


async def test_sendpulse_token_refreshed_after_401(
    client: AsyncClient,
    sendpulse_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Отправка вернула 401 (токен протух/отозван) → провайдер обновляет токен и повторяет."""
    fetch_mock = AsyncMock(
        side_effect=[
            _token_payload(access_token="stale-token"),
            _token_payload(access_token="fresh-token"),
        ]
    )
    send_mock = AsyncMock(
        side_effect=[
            _send_response(401, {"message": "invalid token"}),
            _send_response(200, {"result": True, "id": "abc"}),
        ]
    )
    monkeypatch.setattr(email_sendpulse, "_fetch_access_token", fetch_mock)
    monkeypatch.setattr(email_sendpulse, "_send_email_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "refresh@example.com", "password": "Password1", "name": "Refresh"},
    )

    assert response.status_code == 201
    assert response.json()["data"]["verification_code"] is None

    assert fetch_mock.await_count == 2
    assert send_mock.await_count == 2
    first_token = send_mock.await_args_list[0].args[1]
    second_token = send_mock.await_args_list[1].args[1]
    assert first_token == "stale-token"
    assert second_token == "fresh-token"


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
        email_sendpulse, "_fetch_access_token", AsyncMock(return_value=_token_payload())
    )
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
