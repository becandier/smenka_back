"""Верификация id-токенов Google/Apple (JWKS + RS256).

Только проверка подписи/claims — линковка/регистрация пользователя и
эндпоинты живут в src/app/services/oauth.py и src/app/api/v1/auth.py.
См. ТЗ: docs/tasks/oauth_login/backend.md, раздел «Проверка id-токена».
"""

import time
from dataclasses import dataclass
from typing import Any

import httpx
from jose import jwt
from jose.exceptions import JOSEError

from src.app.services.auth import AuthError

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
APPLE_ISSUERS = frozenset({"https://appleid.apple.com"})

_JWKS_CACHE_TTL_SECONDS = 3600.0

# {jwks_url: (jwks_document, fetched_at_monotonic)}
_jwks_cache: dict[str, tuple[dict[str, Any], float]] = {}


@dataclass(frozen=True)
class OAuthClaims:
    sub: str
    email: str | None
    email_verified: bool | None
    name: str | None = None


async def _fetch_jwks(url: str) -> dict[str, Any]:
    """Сходить за JWKS-документом провайдера. Изолирована для мокания в тестах."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()  # type: ignore[no-any-return]


async def _get_jwks(url: str, *, force_refresh: bool = False) -> dict[str, Any]:
    """JWKS с in-memory кэшем (TTL), чтобы не дёргать сеть на каждый запрос.

    `force_refresh=True` игнорирует ещё не истёкший TTL и всегда идёт в сеть —
    используется, когда искомый `kid` не нашёлся в закэшированном документе
    (провайдер мог только что ротировать ключи, а наш кэш обновился чуть
    раньше ротации).
    """
    cached = _jwks_cache.get(url)
    now = time.monotonic()
    if not force_refresh and cached is not None and now - cached[1] < _JWKS_CACHE_TTL_SECONDS:
        return cached[0]

    try:
        jwks = await _fetch_jwks(url)
    except httpx.HTTPError as exc:
        raise AuthError(
            "OAUTH_PROVIDER_UNAVAILABLE",
            "Не удалось получить ключи провайдера для проверки токена",
            502,
        ) from exc

    _jwks_cache[url] = (jwks, now)
    return jwks


def _find_jwk(jwks: dict[str, Any], kid: str | None) -> dict[str, Any] | None:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key  # type: ignore[no-any-return]
    return None


async def _get_jwk_for_kid(jwks_url: str, kid: str | None) -> dict[str, Any]:
    """Найти ключ по `kid`, форсируя рефетч JWKS один раз, если кэш устарел.

    Без этого легитимные токены, подписанные ключом, добавленным провайдером
    после последнего обновления нашего кэша (ротация ключей), отклонялись бы
    как `INVALID_OAUTH_TOKEN` вплоть до истечения TTL кэша (до часа).
    """
    jwks = await _get_jwks(jwks_url)
    jwk = _find_jwk(jwks, kid)
    if jwk is not None:
        return jwk

    jwks = await _get_jwks(jwks_url, force_refresh=True)
    jwk = _find_jwk(jwks, kid)
    if jwk is not None:
        return jwk

    raise AuthError("INVALID_OAUTH_TOKEN", "Не найден ключ для проверки подписи токена", 400)


async def _decode_id_token(
    id_token: str,
    *,
    jwks_url: str,
    expected_issuers: frozenset[str],
    expected_client_id: str,
) -> dict[str, Any]:
    """Общий хелпер: достать JWKS, найти ключ по kid, проверить подпись/aud/exp/iss."""
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except JOSEError as exc:
        raise AuthError("INVALID_OAUTH_TOKEN", "Невалидный id-токен", 400) from exc

    jwk = await _get_jwk_for_kid(jwks_url, unverified_header.get("kid"))

    try:
        # exp проверяется jose автоматически при decode; iss проверяем сами
        # ниже, т.к. у Google их два допустимых варианта.
        claims: dict[str, Any] = jwt.decode(
            id_token,
            jwk,
            algorithms=["RS256"],
            audience=expected_client_id,
        )
    except JOSEError as exc:
        raise AuthError("INVALID_OAUTH_TOKEN", "Невалидный id-токен", 400) from exc

    if claims.get("iss") not in expected_issuers:
        raise AuthError("INVALID_OAUTH_TOKEN", "Невалидный издатель токена", 400)

    return claims


def _to_claims(payload: dict[str, Any]) -> OAuthClaims:
    sub = payload.get("sub")
    if not sub:
        raise AuthError("INVALID_OAUTH_TOKEN", "В токене отсутствует sub", 400)
    return OAuthClaims(
        sub=sub,
        email=payload.get("email"),
        email_verified=payload.get("email_verified"),
        name=payload.get("name"),
    )


def _reject_unverified_email(claims: OAuthClaims, message: str) -> None:
    if claims.email_verified is False:
        raise AuthError("OAUTH_EMAIL_NOT_VERIFIED", message, 400)


async def verify_google_id_token(id_token: str, expected_client_id: str) -> OAuthClaims:
    """Проверить Google id_token: подпись (JWKS), iss, aud, exp, email_verified."""
    payload = await _decode_id_token(
        id_token,
        jwks_url=GOOGLE_JWKS_URL,
        expected_issuers=GOOGLE_ISSUERS,
        expected_client_id=expected_client_id,
    )
    claims = _to_claims(payload)
    _reject_unverified_email(claims, "Email в Google-аккаунте не подтверждён")
    return claims


async def verify_apple_id_token(id_token: str, expected_client_id: str) -> OAuthClaims:
    """Проверить Apple identity_token: подпись (JWKS), iss, aud, exp.

    Apple не всегда присылает email/email_verified в токене (особенно при
    повторных входах) — отсутствие claim'а не является ошибкой, только явный
    email_verified=False.
    """
    payload = await _decode_id_token(
        id_token,
        jwks_url=APPLE_JWKS_URL,
        expected_issuers=APPLE_ISSUERS,
        expected_client_id=expected_client_id,
    )
    claims = _to_claims(payload)
    _reject_unverified_email(claims, "Email в Apple ID не подтверждён")
    return claims
