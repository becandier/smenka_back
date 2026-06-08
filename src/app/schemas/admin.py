from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AdminUserResponse(BaseModel):
    id: str = Field(description="UUID пользователя")
    email: str
    name: str
    phone: str | None = None
    is_verified: bool
    role: str = Field(description="Глобальная роль: super_admin или user")
    created_at: datetime


class AdminUserDetailResponse(AdminUserResponse):
    owned_organizations_count: int = Field(
        description="Сколько активных организаций пользователь владеет"
    )
    member_organizations_count: int = Field(
        description="В скольких организациях состоит участником"
    )
    shifts_count: int = Field(description="Общее число смен пользователя")


class AdminUserListResponse(BaseModel):
    items: list[AdminUserResponse]
    total: int = Field(description="Всего пользователей без учёта пагинации")
    limit: int
    offset: int


class UpdateUserRoleRequest(BaseModel):
    role: Literal["super_admin", "user"] = Field(description="Новая глобальная роль")


class AdminOrganizationResponse(BaseModel):
    id: str = Field(description="UUID организации")
    name: str
    owner_id: str
    owner_email: str | None = Field(default=None, description="Email владельца")
    member_count: int = Field(description="Число участников (без учёта владельца)")
    is_deleted: bool
    created_at: datetime


class AdminOrganizationListResponse(BaseModel):
    items: list[AdminOrganizationResponse]
    total: int = Field(description="Всего организаций без учёта пагинации")
    limit: int
    offset: int


class AdminStatsResponse(BaseModel):
    users_total: int
    users_verified: int
    organizations_total: int
    organizations_active: int
    shifts_active: int
    shifts_today: int
    shifts_week: int
