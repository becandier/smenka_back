"""Тесты отправки кодов подтверждения по SMTP (smtp_email).

Транспорт всегда мокается — живых сетевых вызовов нет.
"""

from unittest.mock import AsyncMock

import aiosmtplib
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.services import auth as auth_service
from src.app.services import email as email_service


@pytest.fixture
def smtp_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включить SMTP (прод-режим) на время теста: непустой host + 465/SSL."""
    monkeypatch.setattr(email_service.settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(email_service.settings, "smtp_port", 465)
    monkeypatch.setattr(email_service.settings, "smtp_use_ssl", True)
    monkeypatch.setattr(email_service.settings, "smtp_username", "smenka@test")
    monkeypatch.setattr(email_service.settings, "smtp_password", "secret")
    monkeypatch.setattr(email_service.settings, "smtp_from", "smenka@test")
    monkeypatch.setattr(email_service.settings, "smtp_from_name", "Smenka")


async def test_smtp_off_returns_code_and_does_not_send(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMTP выключен (дефолт): код в ответе, SMTP-клиент не вызывается."""
    send_mock = AsyncMock()
    monkeypatch.setattr(email_service.aiosmtplib, "send", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "off@example.com", "password": "Password1", "name": "Off"},
    )

    assert response.status_code == 201
    code = response.json()["data"]["verification_code"]
    assert code is not None
    assert len(code) == 4
    send_mock.assert_not_awaited()


async def test_smtp_on_sends_email_and_hides_code(
    client: AsyncClient,
    smtp_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMTP включён: письмо отправлено, verification_code в ответе = null."""
    send_mock = AsyncMock()
    monkeypatch.setattr(email_service.aiosmtplib, "send", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "on@example.com", "password": "Password1", "name": "On"},
    )

    assert response.status_code == 201
    assert response.json()["data"]["verification_code"] is None

    send_mock.assert_awaited_once()
    message = send_mock.await_args.args[0]
    assert message["To"] == "on@example.com"
    assert message["Subject"] == "Код подтверждения Smenka"
    assert "smenka@test" in str(message["From"])
    assert "Smenka" in str(message["From"])

    kwargs = send_mock.await_args.kwargs
    assert kwargs["hostname"] == "smtp.test"
    assert kwargs["port"] == 465
    assert kwargs["use_tls"] is True
    assert kwargs["start_tls"] is False


async def test_smtp_on_starttls_for_port_587(
    client: AsyncClient,
    smtp_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SMTP_USE_SSL=false → STARTTLS (587): use_tls=False, start_tls=True."""
    monkeypatch.setattr(email_service.settings, "smtp_port", 587)
    monkeypatch.setattr(email_service.settings, "smtp_use_ssl", False)
    send_mock = AsyncMock()
    monkeypatch.setattr(email_service.aiosmtplib, "send", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "tls@example.com", "password": "Password1", "name": "Tls"},
    )

    assert response.status_code == 201
    kwargs = send_mock.await_args.kwargs
    assert kwargs["use_tls"] is False
    assert kwargs["start_tls"] is True


async def test_smtp_send_failure_returns_error_and_persists_user(
    client: AsyncClient,
    db_session: AsyncSession,
    smtp_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сбой отправки → EMAIL_SEND_FAILED, но пользователь уже создан в БД."""
    send_mock = AsyncMock(side_effect=aiosmtplib.SMTPException("boom"))
    monkeypatch.setattr(email_service.aiosmtplib, "send", send_mock)

    response = await client.post(
        "/api/v1/auth/register",
        json={"email": "fail@example.com", "password": "Password1", "name": "Fail"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "EMAIL_SEND_FAILED"

    user = await auth_service.get_user_by_email(db_session, "fail@example.com")
    assert user is not None
    assert user.is_verified is False


async def test_resend_code_sends_email_when_smtp_on(
    client: AsyncClient,
    smtp_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resend-code тоже шлёт письмо и прячет код в проде."""
    send_mock = AsyncMock()
    monkeypatch.setattr(email_service.aiosmtplib, "send", send_mock)

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
