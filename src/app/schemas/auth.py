import re

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr = Field(description="Email для регистрации", examples=["user@example.com"])
    password: str = Field(description="Пароль (мин. 8 символов, буквы + цифры)")
    name: str = Field(description="Имя пользователя", examples=["Иван Иванов"])

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            msg = "Пароль должен быть не менее 8 символов"
            raise ValueError(msg)
        if not re.search(r"[a-zA-Zа-яА-ЯёЁ]", v):
            msg = "Пароль должен содержать хотя бы одну букву"
            raise ValueError(msg)
        if not re.search(r"\d", v):
            msg = "Пароль должен содержать хотя бы одну цифру"
            raise ValueError(msg)
        return v


class RegisterResponse(BaseModel):
    user_id: str = Field(description="UUID созданного пользователя")
    message: str = Field(description="Сообщение о статусе регистрации")
    verification_code: str | None = Field(
        default=None, description="Код подтверждения (только в dev-режиме)"
    )


class VerifyRequest(BaseModel):
    email: EmailStr = Field(description="Email для подтверждения")
    code: str = Field(description="4-значный код из письма", examples=["1234"])


class ResendCodeRequest(BaseModel):
    email: EmailStr = Field(description="Email для повторной отправки кода")


class ResendCodeResponse(BaseModel):
    message: str = Field(description="Сообщение о статусе отправки")
    verification_code: str | None = Field(
        default=None, description="Код подтверждения (только в dev-режиме)"
    )


class LoginRequest(BaseModel):
    email: EmailStr = Field(description="Email", examples=["user@example.com"])
    password: str = Field(description="Пароль")


class TokenResponse(BaseModel):
    access_token: str = Field(description="JWT access-токен (время жизни: 30 мин)")
    refresh_token: str = Field(description="JWT refresh-токен (время жизни: 30 дней)")
    token_type: str = Field(default="bearer", description="Тип токена (всегда bearer)")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(description="Текущий refresh-токен для ротации")


class LogoutRequest(BaseModel):
    refresh_token: str = Field(description="Refresh-токен для отзыва")


class MessageResponse(BaseModel):
    message: str = Field(description="Сообщение о результате операции")
