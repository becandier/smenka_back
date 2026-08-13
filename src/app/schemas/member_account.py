"""Схемы admin_created_accounts: сотрудник, заведённый админом организации.

Создание учётки целиком со стороны организации (логин/email опциональны, но
хотя бы один обязателен) и сброс пароля учётке, которую завела эта организация.
"""

from typing import Literal

from pydantic import BaseModel, EmailStr, Field, ValidationInfo, field_validator

from src.app.core.security import (
    validate_login_format,
    validate_password_strength,
    validate_required_text,
)
from src.app.schemas.organization import MemberResponse


class MemberCreateRequest(BaseModel):
    name: str = Field(description="ФИО сотрудника")
    email: EmailStr | None = Field(default=None, description="Email сотрудника (опционально)")
    login: str | None = Field(
        default=None,
        validate_default=True,
        description="Логин для входа: 3–32 символа, латиница/цифры/._-, "
        "регистронезависимая уникальность по платформе. Обязателен, если email не задан.",
    )
    phone: str | None = Field(default=None, description="Телефон")
    password: str | None = Field(
        default=None,
        description="Пароль; не передан — сервер сгенерирует и вернёт один раз в ответе",
    )
    role: Literal["employee", "admin"] = Field(
        default="employee",
        description="Системная роль: employee (по умолчанию) или admin — "
        "роль admin при создании доступна только владельцу организации",
    )
    role_id: str | None = Field(default=None, description="UUID кастомной роли организации")
    display_name: str | None = Field(
        default=None,
        description="Имя сотрудника в этой организации (member_display_name); "
        "нормализация и лимит 100 символов — на сервере",
    )

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str) -> str:
        return validate_required_text(v, noun="Имя")

    @field_validator("login")
    @classmethod
    def _validate_login(cls, v: str | None, info: ValidationInfo) -> str | None:
        login_value = validate_login_format(v) if v and v.strip() else None
        email = info.data.get("email")
        if login_value is None and email is None:
            raise ValueError("Укажите login или email — хотя бы одно из полей")
        return login_value

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str | None) -> str | None:
        return v if v is None else validate_password_strength(v)


class MemberCreateResponse(BaseModel):
    member: MemberResponse
    login: str | None = Field(default=None, description="Логин учётки (null, если не задан)")
    password: str = Field(
        description="Пароль в открытом виде — сгенерирован сервером или задан явно, "
        "показывается только в этом ответе"
    )


class ResetPasswordRequest(BaseModel):
    password: str | None = Field(
        default=None,
        description="Новый пароль; null или отсутствует — сервер сгенерирует",
    )

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str | None) -> str | None:
        return v if v is None else validate_password_strength(v)


class ResetPasswordResponse(BaseModel):
    user_id: str
    login: str | None = Field(default=None, description="Логин учётки (null, если не задан)")
    password: str = Field(description="Новый пароль в открытом виде — показывается один раз")
