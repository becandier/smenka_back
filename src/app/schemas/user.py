from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserResponse(BaseModel):
    id: str = Field(description="UUID пользователя")
    email: EmailStr = Field(description="Email")
    phone: str | None = Field(default=None, description="Телефон")
    name: str = Field(description="Имя")
    is_verified: bool = Field(description="Email подтверждён")
    created_at: datetime = Field(description="Дата регистрации")

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, description="Новое имя")
    phone: str | None = Field(default=None, description="Новый телефон", examples=["+79001234567"])
