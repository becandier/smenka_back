"""Тесты отправки кодов подтверждения через Loops (smtp_email, транспорт Loops).

HTTP-слой всегда мокается (`email_service._send_loops_request`) — живых запросов
к Loops из тестов быть не должно.
"""

from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.services import auth as auth_service
from src.app.services import email as email_service


def _loops_response(status_code: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=body)


@pytest.fixture
def loops_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включить Loops (прод-режим) на время теста: непустой api key + template id."""
    monkeypatch.setattr(email_service.settings, "loops_api_key", "test-key")
    monkeypatch.setattr(email_service.settings, "loops_transactional_id", "tpl_test")
    monkeypatch.setattr(
        email_service.settings, "loops_api_url", "https://app.loops.so/api/v1/transactional"
    )
    monkeypatch.setattr(email_service.settings, "loops_timeout_seconds", 10)


async def test_loops_off_returns_code_and_does_not_send(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loops выключен (дефолт): код в ответе, HTTP-запрос не уходит."""
    send_mock = AsyncMock()
    monkeypatch.setattr(email_service, "_send_loops_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "off@example.com", "password": "Password1", "name": "Off"},
    )

    assert response.status_code == 201
    code = response.json()["data"]["verification_code"]
    assert code is not None
    assert len(code) == 4
    send_mock.assert_not_awaited()


async def test_loops_on_sends_email_and_hides_code(
    client: AsyncClient,
    loops_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loops включён: запрос ушёл с правильным телом, verification_code в ответе = null."""
    send_mock = AsyncMock(return_value=_loops_response(200, {"success": True}))
    monkeypatch.setattr(email_service, "_send_loops_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "on@example.com", "password": "Password1", "name": "On"},
    )

    assert response.status_code == 201
    assert response.json()["data"]["verification_code"] is None

    send_mock.assert_awaited_once()
    payload = send_mock.await_args.args[0]
    assert payload["transactionalId"] == "tpl_test"
    assert payload["email"] == "on@example.com"
    assert payload["dataVariables"]["code"] is not None
    assert len(payload["dataVariables"]["code"]) == 4
    assert payload["dataVariables"]["ttlMinutes"] == 15


async def test_loops_on_success_false_returns_error(
    client: AsyncClient,
    db_session: AsyncSession,
    loops_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 200, но success != true → EMAIL_SEND_FAILED, пользователь уже создан."""
    send_mock = AsyncMock(return_value=_loops_response(200, {"success": False}))
    monkeypatch.setattr(email_service, "_send_loops_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "false@example.com", "password": "Password1", "name": "False"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"

    user = await auth_service.get_user_by_email(db_session, "false@example.com")
    assert user is not None
    assert user.is_verified is False


async def test_loops_on_non_2xx_returns_error(
    client: AsyncClient,
    loops_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 4xx/5xx → EMAIL_SEND_FAILED."""
    send_mock = AsyncMock(
        return_value=_loops_response(400, {"message": "transactionalId not found"})
    )
    monkeypatch.setattr(email_service, "_send_loops_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "bad@example.com", "password": "Password1", "name": "Bad"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"


async def test_loops_on_timeout_returns_error_and_persists_user(
    client: AsyncClient,
    db_session: AsyncSession,
    loops_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Таймаут/сетевая ошибка → EMAIL_SEND_FAILED, но пользователь уже создан в БД."""
    send_mock = AsyncMock(side_effect=httpx.TimeoutException("boom"))
    monkeypatch.setattr(email_service, "_send_loops_request", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "timeout@example.com", "password": "Password1", "name": "Timeout"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"

    user = await auth_service.get_user_by_email(db_session, "timeout@example.com")
    assert user is not None
    assert user.is_verified is False


async def test_resend_code_sends_email_when_loops_on(
    client: AsyncClient,
    loops_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resend-code тоже шлёт письмо через Loops и прячет код в проде."""
    send_mock = AsyncMock(return_value=_loops_response(200, {"success": True}))
    monkeypatch.setattr(email_service, "_send_loops_request", send_mock)

    await client.post(
        "/api/v1/auth/register",
        json={"email": "resend@example.com", "password": "Password1", "name": "Resend"},
    )
    assert send_mock.await_count == 1

    # Сбрасываем cooldown, чтобы resend прошёл (а не упёрся в 429 COOLDOWN).
    monkeypatch.setattr(email_service.settings, "verification_code_cooldown_seconds", 0)

    response = await client.post(
        "/api/v1/auth/resend-code",
        json={"email": "resend@example.com"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["verification_code"] is None
    assert send_mock.await_count == 2


async def test_code_never_logged_when_loops_on(
    client: AsyncClient,
    loops_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ни в успехе, ни при сбое отправки код не попадает в аргументы логгера."""
    log_info = Mock()
    log_error = Mock()
    monkeypatch.setattr(email_service.logger, "info", log_info)
    monkeypatch.setattr(email_service.logger, "error", log_error)

    # Успех.
    send_mock = AsyncMock(return_value=_loops_response(200, {"success": True}))
    monkeypatch.setattr(email_service, "_send_loops_request", send_mock)
    await client.post(
        "/api/v1/auth/register",
        json={"email": "logsafe1@example.com", "password": "Password1", "name": "LogSafe"},
    )

    # Сбой.
    send_mock2 = AsyncMock(return_value=_loops_response(500, {"message": "boom"}))
    monkeypatch.setattr(email_service, "_send_loops_request", send_mock2)
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
