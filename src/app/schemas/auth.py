import re

from pydantic import BaseModel, EmailStr, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str

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
    user_id: str
    message: str
    verification_code: str | None = None  # Only in dev — remove later


class VerifyRequest(BaseModel):
    email: EmailStr
    code: str


class ResendCodeRequest(BaseModel):
    email: EmailStr


class ResendCodeResponse(BaseModel):
    message: str
    verification_code: str | None = None  # Only in dev — remove later


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class MessageResponse(BaseModel):
    message: str
