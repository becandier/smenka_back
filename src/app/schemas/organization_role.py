from datetime import datetime

from pydantic import BaseModel, Field


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="Название роли")


class RoleUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="Новое название роли")


class RoleResponse(BaseModel):
    id: str = Field(description="UUID роли")
    name: str = Field(description="Название роли")
    created_at: datetime = Field(description="Дата создания")

    model_config = {"from_attributes": True}


class RoleListResponse(BaseModel):
    items: list[RoleResponse] = Field(description="Список кастомных ролей организации")


class MemberRoleAssignRequest(BaseModel):
    role_id: str | None = Field(
        default=None,
        description="UUID кастомной роли или null для снятия роли",
    )
