"""Бизнес-логика входа/линковки/регистрации через OAuth (Google/Apple).

Проверка id-токенов — `src/app/services/oauth_tokens.py`, платформенная
настройка `oauth_provider_settings` — `src/app/services/oauth_provider_settings.py`.
См. ТЗ: docs/tasks/oauth_login/backend.md, раздел «Бизнес-правила» →
«Поиск/создание пользователя».
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from src.app.core.security import create_access_token
from src.app.models.oauth import OAuthIdentity, OAuthProviderSetting
from src.app.models.user import User, UserRole
from src.app.services.auth import AuthError, _create_refresh_token_db
from src.app.services.oauth_provider_settings import get_provider_setting, require_provider_setting
from src.app.services.oauth_tokens import (
    OAuthClaims,
    verify_apple_id_token,
    verify_google_id_token,
)

_DEFAULT_NAME = "Пользователь"


async def _find_user_by_email_ci(session: AsyncSession, email: str) -> list[User]:
    """Все пользователи с совпадающим (без учёта регистра) email."""
    result = await session.execute(select(User).where(func.lower(User.email) == email.lower()))
    return list(result.scalars().all())


async def _find_identity_with_user(
    session: AsyncSession, provider: str, provider_user_id: str
) -> OAuthIdentity | None:
    """Найти привязку по (provider, sub) вместе с пользователем одним запросом."""
    result = await session.execute(
        select(OAuthIdentity)
        .options(joinedload(OAuthIdentity.user))
        .where(
            OAuthIdentity.provider == provider,
            OAuthIdentity.provider_user_id == provider_user_id,
        )
    )
    return result.scalar_one_or_none()


async def _link_or_register_user(
    session: AsyncSession,
    provider: str,
    claims: OAuthClaims,
    fallback_name: str | None,
) -> User:
    """Три ветки из backend.md: вход по sub / автолинк по email / регистрация."""
    identity = await _find_identity_with_user(session, provider, claims.sub)
    if identity is not None:
        return identity.user

    if claims.email is None:
        # Повторный вход должен был найтись по sub выше. Если email отсутствует
        # и по sub не нашли — это первый вход без email в токене, линковать не по чему.
        raise AuthError(
            "INVALID_OAUTH_TOKEN",
            "email required for first-time link",
            400,
        )

    matches = await _find_user_by_email_ci(session, claims.email)
    if len(matches) > 1:
        raise AuthError(
            "OAUTH_LINK_AMBIGUOUS",
            "Найдено больше одного пользователя с совпадающим email",
            500,
        )

    if len(matches) == 1:
        user = matches[0]
        session.add(
            OAuthIdentity(
                user_id=user.id,
                provider=provider,
                provider_user_id=claims.sub,
                email=claims.email,
            )
        )
        if not user.is_verified:
            user.is_verified = True
        await session.flush()
        return user

    user = User(
        email=claims.email,
        name=fallback_name or _DEFAULT_NAME,
        password_hash=None,
        is_verified=True,
        role=UserRole.user,
    )
    session.add(user)
    await session.flush()
    session.add(
        OAuthIdentity(
            user_id=user.id,
            provider=provider,
            provider_user_id=claims.sub,
            email=claims.email,
        )
    )
    await session.flush()
    return user


async def _issue_token_pair(session: AsyncSession, user: User) -> tuple[str, str]:
    access_token = create_access_token(str(user.id))
    refresh_token = await _create_refresh_token_db(session, user.id)
    return access_token, refresh_token


async def authenticate_google(
    session: AsyncSession, id_token: str, client_type: str
) -> tuple[str, str]:
    """Вход/регистрация через Google. Возвращает (access_token, refresh_token)."""
    setting = await require_provider_setting(session, "google", client_type)
    claims = await verify_google_id_token(id_token, setting.client_id)
    user = await _link_or_register_user(session, "google", claims, fallback_name=claims.name)
    return await _issue_token_pair(session, user)


async def authenticate_apple(
    session: AsyncSession,
    identity_token: str,
    client_type: str,
    email: str | None,
    name: str | None,
) -> tuple[str, str]:
    """Вход/регистрация через Apple. Возвращает (access_token, refresh_token).

    `email`/`name` из тела запроса — client-supplied, ничем криптографически
    не подтверждены (эндпоинт `public`). Для поиска/линковки/регистрации
    пользователя используется ИСКЛЮЧИТЕЛЬНО `claims.email` из проверенного
    id-токена (см. backend.md → «Бизнес-правила» → «Поиск/создание
    пользователя», п.2: матчинг по `token_email`). Тело запроса безопасно
    использовать только для `name` (fallback_name) — это лишь отображаемое
    имя нового аккаунта, оно не участвует в поиске/автолинке существующих
    пользователей, поэтому подделка этого поля не даёт захвата чужого
    аккаунта.

    Если Apple не прислал email в самом id-токене (обычно означает повторную
    авторизацию — но мы не можем криптографически отличить "это правда не
    первый вход" от токена, специально полученного атакующим по второму
    разу), а по `sub` пользователь не найден — регистрация/автолинк
    невозможны без доверенного email: `_link_or_register_user` отклонит
    такой запрос как `INVALID_OAUTH_TOKEN`. Доверять email из тела запроса в
    этом случае значило бы позволить захват чужого аккаунта: атакующий может
    подставить в теле произвольный email существующего пользователя и
    получить автолинк своей Apple-идентичности к чужому аккаунту.
    """
    setting = await require_provider_setting(session, "apple", client_type)
    claims = await verify_apple_id_token(identity_token, setting.client_id)
    user = await _link_or_register_user(session, "apple", claims, fallback_name=name)
    return await _issue_token_pair(session, user)


async def get_oauth_config(
    session: AsyncSession, client_type: str
) -> dict[str, dict[str, object] | None]:
    """Публичный конфиг для GET /auth/oauth/config: {google, apple} | null.

    Для Apple на Android реального client_type-конфига нет (Android у Apple
    нет нативного SDK) — используется общая (apple, web) запись.
    """
    apple_client_type = "web" if client_type == "android" else client_type

    google_setting = await _get_enabled_setting(session, "google", client_type)
    apple_setting = await _get_enabled_setting(session, "apple", apple_client_type)

    return {
        "google": (
            {"client_id": google_setting.client_id, "enabled": True}
            if google_setting is not None
            else None
        ),
        "apple": (
            {"client_id": apple_setting.client_id, "enabled": True}
            if apple_setting is not None
            else None
        ),
    }


async def _get_enabled_setting(
    session: AsyncSession, provider: str, client_type: str
) -> OAuthProviderSetting | None:
    setting = await get_provider_setting(session, provider, client_type)
    if setting is None or not setting.enabled:
        return None
    return setting
