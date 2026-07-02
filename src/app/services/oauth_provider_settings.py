"""Платформенная настройка OAuth-провайдеров (`oauth_provider_settings`).

Читающая часть (`get_provider_setting`/`require_provider_setting`) переиспользуется
публичным auth-сервисом (`/auth/oauth/*`, `/auth/oauth/config`) — не переименовывать
без согласования.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.oauth import OAuthProviderSetting
from src.app.services.admin import AdminError
from src.app.services.auth import AuthError

# Единственные 5 допустимых комбинаций provider/client_type (см. backend.md).
ALLOWED_COMBOS: frozenset[tuple[str, str]] = frozenset(
    {
        ("google", "web"),
        ("google", "android"),
        ("google", "ios"),
        ("apple", "ios"),
        ("apple", "web"),
    }
)


async def get_provider_setting(
    session: AsyncSession,
    provider: str,
    client_type: str,
) -> OAuthProviderSetting | None:
    """Найти запись настройки по (provider, client_type). None, если не настроена."""
    result = await session.execute(
        select(OAuthProviderSetting).where(
            OAuthProviderSetting.provider == provider,
            OAuthProviderSetting.client_type == client_type,
        )
    )
    return result.scalar_one_or_none()


async def require_provider_setting(
    session: AsyncSession,
    provider: str,
    client_type: str,
) -> OAuthProviderSetting:
    """Как get_provider_setting, но 503 OAUTH_PROVIDER_NOT_CONFIGURED, если нет
    записи или она выключена (enabled=False).
    """
    setting = await get_provider_setting(session, provider, client_type)
    if setting is None or not setting.enabled:
        raise AuthError(
            "OAUTH_PROVIDER_NOT_CONFIGURED",
            f"Провайдер {provider}/{client_type} не настроен или временно недоступен",
            503,
        )
    return setting


async def list_provider_settings(
    session: AsyncSession,
) -> list[OAuthProviderSetting | dict[str, Any]]:
    """Все 5 допустимых комбинаций: реальная запись из БД, либо заглушка-dict
    (client_id=None, enabled=False, updated_by=None, updated_at=None) для
    ненастроенных комбинаций.
    """
    result = await session.execute(select(OAuthProviderSetting))
    existing = {(row.provider, row.client_type): row for row in result.scalars().all()}

    items: list[OAuthProviderSetting | dict[str, Any]] = []
    for provider, client_type in ALLOWED_COMBOS:
        setting = existing.get((provider, client_type))
        if setting is not None:
            items.append(setting)
        else:
            items.append(
                {
                    "provider": provider,
                    "client_type": client_type,
                    "client_id": None,
                    "enabled": False,
                    "updated_by": None,
                    "updated_at": None,
                }
            )
    return items


async def upsert_provider_setting(
    session: AsyncSession,
    provider: str,
    client_type: str,
    client_id: str,
    enabled: bool,
    updated_by_id: uuid.UUID,
) -> OAuthProviderSetting:
    """Upsert записи (provider, client_type). 422 VALIDATION_ERROR для
    недопустимой комбинации.
    """
    if (provider, client_type) not in ALLOWED_COMBOS:
        raise AdminError(
            "VALIDATION_ERROR",
            "недопустимая комбинация provider/client_type",
            422,
        )

    setting = await get_provider_setting(session, provider, client_type)
    now = datetime.now(UTC)
    if setting is not None:
        setting.client_id = client_id
        setting.enabled = enabled
        setting.updated_by = updated_by_id
        setting.updated_at = now
    else:
        setting = OAuthProviderSetting(
            provider=provider,
            client_type=client_type,
            client_id=client_id,
            enabled=enabled,
            updated_by=updated_by_id,
            updated_at=now,
        )
        session.add(setting)

    await session.flush()
    return setting
