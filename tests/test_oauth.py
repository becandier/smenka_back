"""Тесты бизнес-логики входа/линковки/регистрации через OAuth и публичных
эндпоинтов /auth/oauth/google, /auth/oauth/apple, /auth/oauth/config.

verify_google_id_token/verify_apple_id_token мокаются напрямую в модуле
src.app.services.oauth (там они импортированы через `from ... import`).
Настоящей сети/JWKS в этих тестах нет — см. tests/test_oauth_tokens.py для
верификации самого id-токена.
"""

import uuid
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.oauth import OAuthIdentity, OAuthProviderSetting
from src.app.models.user import User
from src.app.services import oauth as oauth_service
from src.app.services.auth import AuthError
from src.app.services.oauth_tokens import OAuthClaims


async def _configure_provider(
    db_session: AsyncSession,
    provider: str,
    client_type: str,
    client_id: str = "configured-client-id",
    enabled: bool = True,
) -> None:
    db_session.add(
        OAuthProviderSetting(
            provider=provider,
            client_type=client_type,
            client_id=client_id,
            enabled=enabled,
        )
    )
    await db_session.commit()


@pytest.fixture
def mock_google(monkeypatch: pytest.MonkeyPatch) -> Callable[..., AsyncMock]:
    def _apply(
        claims: OAuthClaims | None = None, *, side_effect: Exception | None = None
    ) -> AsyncMock:
        mock = AsyncMock(side_effect=side_effect, return_value=claims)
        monkeypatch.setattr(oauth_service, "verify_google_id_token", mock)
        return mock

    return _apply


@pytest.fixture
def mock_apple(monkeypatch: pytest.MonkeyPatch) -> Callable[..., AsyncMock]:
    def _apply(
        claims: OAuthClaims | None = None, *, side_effect: Exception | None = None
    ) -> AsyncMock:
        mock = AsyncMock(side_effect=side_effect, return_value=claims)
        monkeypatch.setattr(oauth_service, "verify_apple_id_token", mock)
        return mock

    return _apply


# --- POST /auth/oauth/google ---


async def test_google_links_to_existing_user_by_email(
    client: AsyncClient,
    db_session: AsyncSession,
    verified_user: User,
    mock_google: Callable[..., AsyncMock],
) -> None:
    await _configure_provider(db_session, "google", "web")
    mock_google(OAuthClaims(sub="google-sub-1", email="test@example.com", email_verified=True))

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"] is None
    assert "access_token" in body["data"]
    assert "refresh_token" in body["data"]

    result = await db_session.execute(
        select(OAuthIdentity).where(OAuthIdentity.provider_user_id == "google-sub-1")
    )
    identity = result.scalar_one()
    assert identity.user_id == verified_user.id
    assert identity.provider == "google"

    await db_session.refresh(verified_user)
    assert verified_user.is_verified is True


async def test_google_registers_new_user_when_email_unknown(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_google: Callable[..., AsyncMock],
) -> None:
    await _configure_provider(db_session, "google", "web")
    mock_google(
        OAuthClaims(sub="google-sub-new", email="brandnew@example.com", email_verified=True)
    )

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 200

    user_result = await db_session.execute(
        select(User).where(User.email == "brandnew@example.com")
    )
    user = user_result.scalar_one()
    assert user.password_hash is None
    assert user.is_verified is True

    identity_result = await db_session.execute(
        select(OAuthIdentity).where(OAuthIdentity.user_id == user.id)
    )
    identity = identity_result.scalar_one()
    assert identity.provider == "google"
    assert identity.provider_user_id == "google-sub-new"
    assert user.name == "Пользователь"


async def test_google_registers_new_user_with_name_from_token(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_google: Callable[..., AsyncMock],
) -> None:
    """`name` нового Google-пользователя должно браться из claim токена, а не
    всегда падать на дефолт "Пользователь"."""
    await _configure_provider(db_session, "google", "web")
    mock_google(
        OAuthClaims(
            sub="google-sub-named",
            email="named@example.com",
            email_verified=True,
            name="Иван Иванов",
        )
    )

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 200

    user_result = await db_session.execute(select(User).where(User.email == "named@example.com"))
    user = user_result.scalar_one()
    assert user.name == "Иван Иванов"


async def test_google_repeat_login_finds_by_sub_without_email(
    client: AsyncClient,
    db_session: AsyncSession,
    verified_user: User,
    mock_google: Callable[..., AsyncMock],
) -> None:
    await _configure_provider(db_session, "google", "web")
    db_session.add(
        OAuthIdentity(
            user_id=verified_user.id,
            provider="google",
            provider_user_id="google-sub-existing",
            email="test@example.com",
        )
    )
    await db_session.commit()

    # claims.email отсутствует — поиск не должен обращаться к email-матчингу.
    mock_google(OAuthClaims(sub="google-sub-existing", email=None, email_verified=None))

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 200

    result = await db_session.execute(
        select(OAuthIdentity).where(OAuthIdentity.provider == "google")
    )
    identities = result.scalars().all()
    assert len(identities) == 1  # не создалась новая привязка


async def test_google_email_not_verified_returns_400(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_google: Callable[..., AsyncMock],
) -> None:
    await _configure_provider(db_session, "google", "web")
    mock_google(side_effect=AuthError("OAUTH_EMAIL_NOT_VERIFIED", "Email не подтверждён", 400))

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "OAUTH_EMAIL_NOT_VERIFIED"


async def test_google_invalid_token_returns_400(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_google: Callable[..., AsyncMock],
) -> None:
    await _configure_provider(db_session, "google", "web")
    mock_google(side_effect=AuthError("INVALID_OAUTH_TOKEN", "Невалидный токен", 400))

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_OAUTH_TOKEN"


async def test_google_case_insensitive_email_matching_links_same_user(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_google: Callable[..., AsyncMock],
) -> None:
    user = User(
        id=uuid.uuid4(),
        email="Ivan@Gmail.com",
        password_hash=None,
        name="Ivan",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()

    await _configure_provider(db_session, "google", "web")
    mock_google(OAuthClaims(sub="google-sub-ivan", email="ivan@gmail.com", email_verified=True))

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 200

    user_result = await db_session.execute(select(User).where(User.email == "Ivan@Gmail.com"))
    users = user_result.scalars().all()
    assert len(users) == 1  # дубль не создан

    identity_result = await db_session.execute(
        select(OAuthIdentity).where(OAuthIdentity.provider_user_id == "google-sub-ivan")
    )
    identity = identity_result.scalar_one()
    assert identity.user_id == user.id


async def test_google_link_ambiguous_when_multiple_case_duplicate_emails(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_google: Callable[..., AsyncMock],
) -> None:
    """Легаси-кейс из «Технического долга» backend.md: если до чистки
    case-дублей в БД остались два пользователя с email, различающимся только
    регистром, автолинк не должен наугад выбрать одного из них — 500
    `OAUTH_LINK_AMBIGUOUS`.
    """
    db_session.add_all(
        [
            User(
                id=uuid.uuid4(),
                email="Dup@Example.com",
                password_hash=None,
                name="Dup One",
                is_verified=True,
            ),
            User(
                id=uuid.uuid4(),
                email="dup@example.com",
                password_hash=None,
                name="Dup Two",
                is_verified=True,
            ),
        ]
    )
    await db_session.commit()

    await _configure_provider(db_session, "google", "web")
    mock_google(OAuthClaims(sub="google-sub-dup", email="DUP@Example.com", email_verified=True))

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "OAUTH_LINK_AMBIGUOUS"

    identity_result = await db_session.execute(
        select(OAuthIdentity).where(OAuthIdentity.provider_user_id == "google-sub-dup")
    )
    assert identity_result.scalar_one_or_none() is None


async def test_google_provider_not_configured_returns_503_without_verifying_token(
    client: AsyncClient,
    mock_google: Callable[..., AsyncMock],
) -> None:
    mock = mock_google(OAuthClaims(sub="whatever", email="x@example.com", email_verified=True))

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "OAUTH_PROVIDER_NOT_CONFIGURED"
    mock.assert_not_awaited()


async def test_google_provider_disabled_returns_503(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_google: Callable[..., AsyncMock],
) -> None:
    await _configure_provider(db_session, "google", "web", enabled=False)
    mock = mock_google(OAuthClaims(sub="whatever", email="x@example.com", email_verified=True))

    response = await client.post(
        "/api/v1/auth/oauth/google",
        json={"id_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "OAUTH_PROVIDER_NOT_CONFIGURED"
    mock.assert_not_awaited()


# --- POST /auth/oauth/apple ---


async def test_apple_registers_new_user_with_token_email_and_body_name(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_apple: Callable[..., AsyncMock],
) -> None:
    """Первая авторизация: Apple кладёт email в сам id-токен (claims.email).

    Тело запроса дублирует то же значение (как это в реальности делает
    Apple SDK на клиенте) плюс `name`, которого в JWT нет никогда — email из
    тела не используется для поиска/регистрации (см. `authenticate_apple`),
    используется только `claims.email` (из проверенного токена) для
    идентификации и `name` из тела — только как отображаемое имя нового
    аккаунта.
    """
    await _configure_provider(db_session, "apple", "ios")
    mock_apple(
        OAuthClaims(sub="apple-sub-new", email="appleuser@example.com", email_verified=True)
    )

    response = await client.post(
        "/api/v1/auth/oauth/apple",
        json={
            "identity_token": "fake-token",
            "client_type": "ios",
            "email": "appleuser@example.com",
            "name": "Apple User",
        },
    )

    assert response.status_code == 200

    result = await db_session.execute(select(User).where(User.email == "appleuser@example.com"))
    user = result.scalar_one()
    assert user.name == "Apple User"
    assert user.password_hash is None
    assert user.is_verified is True


async def test_apple_body_email_cannot_link_to_existing_user_when_token_has_no_email(
    client: AsyncClient,
    db_session: AsyncSession,
    verified_user: User,
    mock_apple: Callable[..., AsyncMock],
) -> None:
    """Регрессия на account takeover: id-токен без claims.email (как у
    Apple при неполной/повторной авторизации) не должен позволять привязать
    чужую Apple-идентичность к существующему пользователю через
    client-supplied `email` в теле запроса — это непроверенное значение,
    подделываемое любым вызывающим (эндпоинт public).
    """
    await _configure_provider(db_session, "apple", "ios")
    mock_apple(OAuthClaims(sub="attacker-apple-sub", email=None, email_verified=None))

    response = await client.post(
        "/api/v1/auth/oauth/apple",
        json={
            "identity_token": "fake-token",
            "client_type": "ios",
            "email": verified_user.email,
            "name": "Attacker",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_OAUTH_TOKEN"

    identity_result = await db_session.execute(
        select(OAuthIdentity).where(OAuthIdentity.provider_user_id == "attacker-apple-sub")
    )
    assert identity_result.scalar_one_or_none() is None

    await db_session.refresh(verified_user)
    identities_result = await db_session.execute(
        select(OAuthIdentity).where(OAuthIdentity.user_id == verified_user.id)
    )
    assert identities_result.scalars().all() == []


async def test_apple_repeat_login_ignores_body_email_when_found_by_sub(
    client: AsyncClient,
    db_session: AsyncSession,
    verified_user: User,
    mock_apple: Callable[..., AsyncMock],
) -> None:
    await _configure_provider(db_session, "apple", "web")
    db_session.add(
        OAuthIdentity(
            user_id=verified_user.id,
            provider="apple",
            provider_user_id="apple-sub-existing",
            email="test@example.com",
        )
    )
    await db_session.commit()

    mock_apple(OAuthClaims(sub="apple-sub-existing", email=None, email_verified=None))

    response = await client.post(
        "/api/v1/auth/oauth/apple",
        json={"identity_token": "fake-token", "client_type": "web"},
    )

    assert response.status_code == 200
    result = await db_session.execute(
        select(OAuthIdentity).where(OAuthIdentity.provider == "apple")
    )
    identities = result.scalars().all()
    assert len(identities) == 1


async def test_apple_missing_email_and_body_email_returns_invalid_token(
    client: AsyncClient,
    db_session: AsyncSession,
    mock_apple: Callable[..., AsyncMock],
) -> None:
    await _configure_provider(db_session, "apple", "ios")
    mock_apple(OAuthClaims(sub="apple-sub-orphan", email=None, email_verified=None))

    response = await client.post(
        "/api/v1/auth/oauth/apple",
        json={"identity_token": "fake-token", "client_type": "ios"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_OAUTH_TOKEN"


# --- GET /auth/oauth/config ---


async def test_oauth_config_unconfigured_returns_nulls(client: AsyncClient) -> None:
    response = await client.get("/api/v1/auth/oauth/config", params={"client_type": "web"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == {"google": None, "apple": None}


async def test_oauth_config_after_google_configured(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _configure_provider(db_session, "google", "web", client_id="google-web-id")

    response = await client.get("/api/v1/auth/oauth/config", params={"client_type": "web"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["google"] == {"client_id": "google-web-id", "enabled": True}
    assert data["apple"] is None


async def test_oauth_config_apple_android_reads_apple_web_combo(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _configure_provider(db_session, "apple", "web", client_id="apple-web-id")

    response = await client.get("/api/v1/auth/oauth/config", params={"client_type": "android"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["apple"] == {"client_id": "apple-web-id", "enabled": True}


async def test_oauth_config_disabled_provider_returns_null(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _configure_provider(db_session, "google", "ios", enabled=False)

    response = await client.get("/api/v1/auth/oauth/config", params={"client_type": "ios"})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["google"] is None
