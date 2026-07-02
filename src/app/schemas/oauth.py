from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# --- Публичные схемы (POST /auth/oauth/*, GET /auth/oauth/config) ---


class OAuthGoogleRequest(BaseModel):
    id_token: str = Field(description="id_token, полученный от Google Identity Services/SDK")
    client_type: Literal["ios", "android", "web"] = Field(
        description="Платформа клиента — определяет, с каким Client ID сверяется aud токена"
    )


class OAuthAppleRequest(BaseModel):
    identity_token: str = Field(description="identity_token, полученный от Sign in with Apple")
    client_type: Literal["ios", "web"] = Field(
        description="Платформа клиента — ios: нативный флоу (aud=Bundle ID), "
        "web: используется и админкой, и Android (aud=Services ID)"
    )
    email: str | None = Field(
        default=None,
        description=(
            "Email из первой авторизации Apple (клиент присылает его только один раз). "
            "Не проверен криптографически (эндпоинт public) — бэк НЕ использует это поле "
            "для поиска/автолинка/регистрации пользователя, только claims.email из "
            "проверенного identity_token. Принимается для совместимости с клиентом, "
            "который может его прислать, но игнорируется сервисным слоем."
        ),
    )
    name: str | None = Field(
        default=None,
        description=(
            "Имя из первой авторизации Apple (Apple присылает его только один раз). "
            "Используется только как отображаемое имя нового аккаунта (fallback), "
            "не участвует в поиске/автолинке — подделка этого поля безопасна."
        ),
    )


class OAuthConfigProviderResponse(BaseModel):
    client_id: str = Field(description="Client ID/Bundle ID/Services ID для этого провайдера")
    enabled: bool = Field(description="Провайдер включён для входа на этом client_type")


class OAuthConfigResponse(BaseModel):
    google: OAuthConfigProviderResponse | None = Field(
        default=None, description="null, если для запрошенного client_type Google не настроен"
    )
    apple: OAuthConfigProviderResponse | None = Field(
        default=None, description="null, если для запрошенного client_type Apple не настроен"
    )


# --- Admin-схемы (GET/PUT /admin/oauth-providers/...) ---


class OAuthProviderSettingResponse(BaseModel):
    provider: str = Field(description="google | apple")
    client_type: str = Field(description="web | ios | android")
    client_id: str | None = Field(default=None, description="null, если комбинация не настроена")
    enabled: bool
    updated_by: str | None = Field(
        default=None, description="UUID super_admin, последним менявшего запись"
    )
    updated_at: datetime | None = None


class OAuthProviderSettingListResponse(BaseModel):
    items: list[OAuthProviderSettingResponse]


class UpsertOAuthProviderRequest(BaseModel):
    client_id: str = Field(description="Client ID (Google) / Bundle ID или Services ID (Apple)")
    enabled: bool = Field(description="Выключить = временно недоступен для входа")
