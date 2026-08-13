from pydantic import BaseModel, EmailStr, Field, ValidationInfo, field_validator

from src.app.core.security import validate_password_strength


class RegisterRequest(BaseModel):
    email: EmailStr = Field(description="Email для регистрации", examples=["user@example.com"])
    password: str = Field(description="Пароль (мин. 8 символов, буквы + цифры)")
    name: str = Field(description="Имя пользователя", examples=["Иван Иванов"])

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return validate_password_strength(v)


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
    """Вход по логину или email (admin_created_accounts).

    Обратная совместимость: старые мобильные билды шлют только `email` — это
    продолжает работать без изменений. Новые клиенты шлют `login`, куда
    пользователь может ввести и логин, и email (без проверки формата — просто
    строка, которую ищем сначала среди login, потом среди email). Ровно одно
    из полей должно быть заполнено.
    """

    email: EmailStr | None = Field(
        default=None,
        description="Email (устаревшее поле, сохранено для обратной совместимости)",
        examples=["user@example.com"],
    )
    login: str | None = Field(
        default=None,
        validate_default=True,
        description="Логин или email — новые клиенты шлют идентификатор сюда",
        examples=["ivanov"],
    )
    password: str = Field(description="Пароль")

    @field_validator("login")
    @classmethod
    def _check_identifier(cls, v: str | None, info: ValidationInfo) -> str | None:
        trimmed = (v or "").strip() or None
        email = info.data.get("email")
        if bool(trimmed) == bool(email):
            raise ValueError("Укажите login или email — ровно одно из полей")
        return trimmed

    @property
    def identifier(self) -> str:
        """`login` или `email` — какое бы поле ни было заполнено. Всегда `str`:
        `_check_identifier` уже гарантирует ровно одно из двух полей."""
        ident = self.login or self.email
        if ident is None:  # pragma: no cover — недостижимо, см. _check_identifier
            raise ValueError("login или email обязателен")
        return ident


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
