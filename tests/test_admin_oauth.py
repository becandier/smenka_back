"""Тесты платформенных admin-эндпоинтов настройки OAuth-провайдеров
(GET/PUT /api/v1/admin/oauth-providers/...), см. docs/tasks/oauth_login/backend.md.
"""

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.oauth import OAuthProviderSetting

ALLOWED_COMBOS = {
    ("google", "web"),
    ("google", "android"),
    ("google", "ios"),
    ("apple", "ios"),
    ("apple", "web"),
}


async def test_list_empty_returns_five_unconfigured(
    client: AsyncClient,
    super_admin_headers: dict[str, str],
) -> None:
    response = await client.get("/api/v1/admin/oauth-providers", headers=super_admin_headers)

    assert response.status_code == 200
    body = response.json()
    items = body["data"]["items"]
    assert len(items) == 5
    combos = {(item["provider"], item["client_type"]) for item in items}
    assert combos == ALLOWED_COMBOS
    for item in items:
        assert item["enabled"] is False
        assert item["client_id"] is None
        assert item["updated_by"] is None
        assert item["updated_at"] is None


async def test_put_valid_combo_creates_record(
    client: AsyncClient,
    super_admin_headers: dict[str, str],
    super_admin_user,
    db_session: AsyncSession,
) -> None:
    response = await client.put(
        "/api/v1/admin/oauth-providers/google/web",
        headers=super_admin_headers,
        json={"client_id": "google-web-client-id", "enabled": True},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["provider"] == "google"
    assert data["client_type"] == "web"
    assert data["client_id"] == "google-web-client-id"
    assert data["enabled"] is True
    assert data["updated_by"] == str(super_admin_user.id)
    assert data["updated_at"] is not None

    result = await db_session.execute(
        select(OAuthProviderSetting).where(
            OAuthProviderSetting.provider == "google",
            OAuthProviderSetting.client_type == "web",
        )
    )
    row = result.scalar_one()
    assert row.client_id == "google-web-client-id"
    assert row.enabled is True
    assert row.updated_by == super_admin_user.id


async def test_put_same_combo_twice_updates_not_duplicates(
    client: AsyncClient,
    super_admin_headers: dict[str, str],
    db_session: AsyncSession,
) -> None:
    await client.put(
        "/api/v1/admin/oauth-providers/google/web",
        headers=super_admin_headers,
        json={"client_id": "first-client-id", "enabled": True},
    )
    response = await client.put(
        "/api/v1/admin/oauth-providers/google/web",
        headers=super_admin_headers,
        json={"client_id": "second-client-id", "enabled": False},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["client_id"] == "second-client-id"
    assert data["enabled"] is False

    result = await db_session.execute(
        select(OAuthProviderSetting).where(
            OAuthProviderSetting.provider == "google",
            OAuthProviderSetting.client_type == "web",
        )
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].client_id == "second-client-id"
    assert rows[0].enabled is False


async def test_put_invalid_combo_returns_422(
    client: AsyncClient,
    super_admin_headers: dict[str, str],
) -> None:
    response = await client.put(
        "/api/v1/admin/oauth-providers/apple/android",
        headers=super_admin_headers,
        json={"client_id": "whatever", "enabled": True},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_get_forbidden_for_non_super_admin(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.get("/api/v1/admin/oauth-providers", headers=auth_headers)

    assert response.status_code == 403


async def test_put_forbidden_for_non_super_admin(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    response = await client.put(
        "/api/v1/admin/oauth-providers/google/web",
        headers=auth_headers,
        json={"client_id": "whatever", "enabled": True},
    )

    assert response.status_code == 403


async def test_list_reflects_updates_after_multiple_puts(
    client: AsyncClient,
    super_admin_headers: dict[str, str],
) -> None:
    await client.put(
        "/api/v1/admin/oauth-providers/google/web",
        headers=super_admin_headers,
        json={"client_id": "google-web-id", "enabled": True},
    )
    await client.put(
        "/api/v1/admin/oauth-providers/apple/ios",
        headers=super_admin_headers,
        json={"client_id": "apple-ios-id", "enabled": False},
    )

    response = await client.get("/api/v1/admin/oauth-providers", headers=super_admin_headers)

    assert response.status_code == 200
    raw_items = response.json()["data"]["items"]
    items = {(item["provider"], item["client_type"]): item for item in raw_items}
    assert len(items) == 5

    assert items[("google", "web")]["client_id"] == "google-web-id"
    assert items[("google", "web")]["enabled"] is True

    assert items[("apple", "ios")]["client_id"] == "apple-ios-id"
    assert items[("apple", "ios")]["enabled"] is False

    for combo in ALLOWED_COMBOS - {("google", "web"), ("apple", "ios")}:
        assert items[combo]["client_id"] is None
        assert items[combo]["enabled"] is False
