"""Схемы профилей доступа (access_profiles, ADR-006)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from src.app.core.access_catalog import validate_permissions
from src.app.core.security import validate_required_text

_PERMISSIONS_DESCRIPTION = (
    "Права по разделам кабинета: {section: view|manage}. Разделы и допустимые "
    "уровни — из GET /access-permissions; неизвестный раздел, недопустимый "
    "уровень или пустой объект → 422 VALIDATION_ERROR"
)


def _normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) > 500:
        raise ValueError("Описание не может превышать 500 символов")
    return stripped


class AccessSectionResponse(BaseModel):
    code: str = Field(description="Код раздела")
    group: str = Field(description="Группа разделов (для группировки в конструкторе)")
    levels: list[str] = Field(description="Допустимые уровни: view и/или manage")


class AccessCatalogResponse(BaseModel):
    sections: list[AccessSectionResponse] = Field(
        description="Разделы кабинета в порядке построения конструктора"
    )


class AccessProfileCreate(BaseModel):
    name: str = Field(description="Название профиля, 1–100 символов")
    description: str | None = Field(default=None, description="Описание, до 500 символов")
    permissions: dict[str, str] = Field(description=_PERMISSIONS_DESCRIPTION)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str) -> str:
        return validate_required_text(v, noun="Название", max_length=100)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, v: str | None) -> str | None:
        return _normalize_description(v)

    @field_validator("permissions")
    @classmethod
    def _validate_permissions(cls, v: dict[str, str]) -> dict[str, str]:
        return validate_permissions(v)


class AccessProfileUpdate(BaseModel):
    """Partial-обновление: переданные ключи правятся; `permissions` заменяется целиком."""

    name: str | None = Field(default=None, description="Новое название, 1–100 символов")
    description: str | None = Field(
        default=None, description="Описание; null или пустая строка — очистить"
    )
    permissions: dict[str, str] | None = Field(
        default=None, description=_PERMISSIONS_DESCRIPTION + " (заменяется целиком)"
    )

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return validate_required_text(v, noun="Название", max_length=100)

    @field_validator("description")
    @classmethod
    def _validate_description(cls, v: str | None) -> str | None:
        return _normalize_description(v)

    @field_validator("permissions")
    @classmethod
    def _validate_permissions(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return validate_permissions(v)


class AccessProfileResponse(BaseModel):
    id: str = Field(description="UUID профиля")
    organization_id: str = Field(description="UUID организации")
    name: str = Field(description="Название")
    description: str | None = Field(default=None, description="Описание")
    permissions: dict[str, str] = Field(description="Права по разделам {section: level}")
    members_count: int = Field(description="Число участников с этим профилем")
    created_at: datetime = Field(description="Дата создания")
    updated_at: datetime = Field(description="Дата изменения")
    is_deleted: bool = Field(description="Профиль удалён (soft-delete)")
    deleted_at: datetime | None = Field(default=None, description="Момент удаления")


class AccessProfileListResponse(BaseModel):
    items: list[AccessProfileResponse] = Field(description="Профили доступа, по названию")


class AccessProfileRef(BaseModel):
    id: str = Field(description="UUID профиля доступа")
    name: str = Field(description="Название профиля доступа")


class MemberAccessProfileUpdate(BaseModel):
    access_profile_id: uuid.UUID | None = Field(
        description="UUID профиля доступа или null — снять профиль"
    )
