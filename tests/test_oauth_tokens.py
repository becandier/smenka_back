"""Тесты верификации id-токенов Google/Apple (src/app/services/oauth_tokens.py).

Сеть не используется — _fetch_jwks мокается через monkeypatch, JWKS собирается
вручную из тестовой RSA-пары.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from src.app.services import oauth_tokens
from src.app.services.auth import AuthError

KID = "test-kid"


def _generate_rsa_pem_pair() -> tuple[str, str]:
    """Возвращает (private_pem, public_pem)."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


PRIVATE_PEM, PUBLIC_PEM = _generate_rsa_pem_pair()
OTHER_PRIVATE_PEM, OTHER_PUBLIC_PEM = _generate_rsa_pem_pair()


def _build_jwks(public_pem: str, kid: str) -> dict[str, Any]:
    key_dict = jwk.construct(public_pem, algorithm="RS256").to_dict()
    key_dict["kid"] = kid
    return {"keys": [key_dict]}


FAKE_JWKS = _build_jwks(PUBLIC_PEM, KID)


def _sign_token(
    claims: dict[str, Any],
    *,
    private_pem: str = PRIVATE_PEM,
    kid: str = KID,
) -> str:
    return jwt.encode(claims, key=private_pem, algorithm="RS256", headers={"kid": kid})


def _base_claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": "expected-client-id",
        "sub": "provider-user-123",
        "email": "user@example.com",
        "email_verified": True,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "iat": datetime.now(UTC),
    }
    claims.update(overrides)
    return claims


@pytest.fixture(autouse=True)
def _no_jwks_cache() -> None:
    """Каждый тест видит чистый JWKS-кэш модуля."""
    oauth_tokens._jwks_cache.clear()


@pytest.fixture
def mock_fetch_jwks(monkeypatch: pytest.MonkeyPatch) -> Callable[[dict[str, Any]], AsyncMock]:
    def _apply(jwks: dict[str, Any]) -> AsyncMock:
        mock = AsyncMock(return_value=jwks)
        monkeypatch.setattr(oauth_tokens, "_fetch_jwks", mock)
        return mock

    return _apply


# --- Google ---


async def test_google_valid_token_returns_claims(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_base_claims())

    claims = await oauth_tokens.verify_google_id_token(
        token, expected_client_id="expected-client-id"
    )

    assert claims.sub == "provider-user-123"
    assert claims.email == "user@example.com"
    assert claims.email_verified is True


async def test_google_wrong_audience_raises_invalid_token(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_base_claims(aud="someone-else"))

    with pytest.raises(AuthError) as exc_info:
        await oauth_tokens.verify_google_id_token(token, expected_client_id="expected-client-id")

    assert exc_info.value.code == "INVALID_OAUTH_TOKEN"
    assert exc_info.value.status_code == 400


async def test_google_expired_token_raises_invalid_token(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_base_claims(exp=datetime.now(UTC) - timedelta(minutes=5)))

    with pytest.raises(AuthError) as exc_info:
        await oauth_tokens.verify_google_id_token(token, expected_client_id="expected-client-id")

    assert exc_info.value.code == "INVALID_OAUTH_TOKEN"


async def test_google_email_not_verified_raises_specific_error(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_base_claims(email_verified=False))

    with pytest.raises(AuthError) as exc_info:
        await oauth_tokens.verify_google_id_token(token, expected_client_id="expected-client-id")

    assert exc_info.value.code == "OAUTH_EMAIL_NOT_VERIFIED"
    assert exc_info.value.status_code == 400


async def test_google_invalid_signature_raises_invalid_token(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    """Токен подписан приватным ключом, который НЕ соответствует ключу в JWKS."""
    mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_base_claims(), private_pem=OTHER_PRIVATE_PEM)

    with pytest.raises(AuthError) as exc_info:
        await oauth_tokens.verify_google_id_token(token, expected_client_id="expected-client-id")

    assert exc_info.value.code == "INVALID_OAUTH_TOKEN"


async def test_google_wrong_issuer_raises_invalid_token(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_base_claims(iss="https://evil.example.com"))

    with pytest.raises(AuthError) as exc_info:
        await oauth_tokens.verify_google_id_token(token, expected_client_id="expected-client-id")

    assert exc_info.value.code == "INVALID_OAUTH_TOKEN"


async def test_google_jwks_fetch_failure_raises_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = AsyncMock(side_effect=httpx.HTTPError("boom"))
    monkeypatch.setattr(oauth_tokens, "_fetch_jwks", mock)
    token = _sign_token(_base_claims())

    with pytest.raises(AuthError) as exc_info:
        await oauth_tokens.verify_google_id_token(token, expected_client_id="expected-client-id")

    assert exc_info.value.code == "OAUTH_PROVIDER_UNAVAILABLE"
    assert exc_info.value.status_code == 502


async def test_google_jwks_cached_between_calls(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    mock = mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_base_claims())

    await oauth_tokens.verify_google_id_token(token, expected_client_id="expected-client-id")
    await oauth_tokens.verify_google_id_token(token, expected_client_id="expected-client-id")

    mock.assert_awaited_once()


async def test_google_forces_jwks_refetch_when_kid_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Регрессия: неизвестный `kid` (кэш ещё не видел ротированный ключ
    провайдера) обязан форсировать повторный fetch JWKS перед отказом —
    иначе легитимные токены отклонялись бы вплоть до истечения TTL кэша."""
    rotated_kid = "rotated-kid"
    rotated_jwks = _build_jwks(OTHER_PUBLIC_PEM, rotated_kid)
    mock = AsyncMock(side_effect=[FAKE_JWKS, rotated_jwks])
    monkeypatch.setattr(oauth_tokens, "_fetch_jwks", mock)

    token = _sign_token(_base_claims(), private_pem=OTHER_PRIVATE_PEM, kid=rotated_kid)

    claims = await oauth_tokens.verify_google_id_token(
        token, expected_client_id="expected-client-id"
    )

    assert claims.sub == "provider-user-123"
    assert mock.await_count == 2


async def test_google_jwks_refetches_even_if_cache_not_yet_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Даже "тёплый" (не протухший по TTL) кэш не должен блокировать токен,
    подписанный ключом, которого в нём ещё нет — TTL не гарантирует, что
    провайдер не ротировал ключи прямо перед этим запросом."""
    oauth_tokens._jwks_cache[oauth_tokens.GOOGLE_JWKS_URL] = (FAKE_JWKS, time.monotonic())

    rotated_kid = "rotated-kid-2"
    rotated_jwks = _build_jwks(OTHER_PUBLIC_PEM, rotated_kid)
    mock = AsyncMock(return_value=rotated_jwks)
    monkeypatch.setattr(oauth_tokens, "_fetch_jwks", mock)

    token = _sign_token(_base_claims(), private_pem=OTHER_PRIVATE_PEM, kid=rotated_kid)

    claims = await oauth_tokens.verify_google_id_token(
        token, expected_client_id="expected-client-id"
    )

    assert claims.sub == "provider-user-123"
    mock.assert_awaited_once()


async def test_google_unknown_kid_after_refetch_raises_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kid, которого нет ни в кэше, ни в свежем JWKS — валидная ошибка
    `INVALID_OAUTH_TOKEN`, а не бесконечные рефетчи (форсируется только раз)."""
    mock = AsyncMock(side_effect=[FAKE_JWKS, FAKE_JWKS])
    monkeypatch.setattr(oauth_tokens, "_fetch_jwks", mock)
    token = _sign_token(_base_claims(), kid="totally-unknown-kid")

    with pytest.raises(AuthError) as exc_info:
        await oauth_tokens.verify_google_id_token(token, expected_client_id="expected-client-id")

    assert exc_info.value.code == "INVALID_OAUTH_TOKEN"
    assert mock.await_count == 2


# --- Apple ---


def _apple_claims(**overrides: Any) -> dict[str, Any]:
    claims: dict[str, Any] = {
        "iss": "https://appleid.apple.com",
        "aud": "com.becandier.smenka",
        "sub": "apple-sub-456",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "iat": datetime.now(UTC),
    }
    claims.update(overrides)
    return claims


async def test_apple_valid_token_returns_claims(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_apple_claims(email="user@icloud.com", email_verified=True))

    claims = await oauth_tokens.verify_apple_id_token(
        token, expected_client_id="com.becandier.smenka"
    )

    assert claims.sub == "apple-sub-456"
    assert claims.email == "user@icloud.com"
    assert claims.email_verified is True


async def test_apple_wrong_audience_raises_invalid_token(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_apple_claims(aud="wrong.bundle.id"))

    with pytest.raises(AuthError) as exc_info:
        await oauth_tokens.verify_apple_id_token(token, expected_client_id="com.becandier.smenka")

    assert exc_info.value.code == "INVALID_OAUTH_TOKEN"


async def test_apple_missing_email_claims_does_not_raise(
    mock_fetch_jwks: Callable[[dict[str, Any]], AsyncMock],
) -> None:
    """Apple не всегда присылает email/email_verified (повторные входы)."""
    mock_fetch_jwks(FAKE_JWKS)
    token = _sign_token(_apple_claims())

    claims = await oauth_tokens.verify_apple_id_token(
        token, expected_client_id="com.becandier.smenka"
    )

    assert claims.sub == "apple-sub-456"
    assert claims.email is None
    assert claims.email_verified is None
