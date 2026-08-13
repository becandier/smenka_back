from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserResponse(BaseModel):
    id: str = Field(description="UUID пользователя")
    email: EmailStr | None = Field(
        default=None,
        description="Email; null — учётка заведена админом организации только с "
        "логином (admin_created_accounts)",
    )
    login: str | None = Field(default=None, description="Логин для входа (admin_created_accounts)")
    phone: str | None = Field(default=None, description="Телефон")
    name: str = Field(description="Имя")
    is_verified: bool = Field(description="Email подтверждён")
    role: str = Field(description="Глобальная роль: super_admin или user")
    created_at: datetime = Field(description="Дата регистрации")

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, description="Новое имя")
    phone: str | None = Field(default=None, description="Новый телефон", examples=["+79001234567"])
