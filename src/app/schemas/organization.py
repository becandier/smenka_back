from datetime import datetime

from pydantic import BaseModel, Field


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="Название организации")


class OrganizationUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="Новое название")


class OrganizationResponse(BaseModel):
    id: str = Field(description="UUID организации")
    name: str = Field(description="Название")
    owner_id: str = Field(description="UUID владельца")
    invite_code: str = Field(description="Инвайт-код для присоединения")
    is_deleted: bool = Field(description="Помечена как удалённая")
    created_at: datetime = Field(description="Дата создания")

    model_config = {"from_attributes": True}


class OrganizationListResponse(BaseModel):
    items: list[OrganizationResponse] = Field(description="Список организаций")


class MemberResponse(BaseModel):
    id: str = Field(description="UUID записи об участии")
    organization_id: str = Field(description="UUID организации")
    user_id: str = Field(description="UUID пользователя")
    user_name: str = Field(description="Имя участника")
    user_email: str = Field(description="Email участника")
    role: str = Field(description="Роль: admin или employee")
    joined_at: datetime = Field(description="Дата присоединения")

    model_config = {"from_attributes": True}


class MemberListResponse(BaseModel):
    items: list[MemberResponse] = Field(description="Список участников")


class JoinResponse(BaseModel):
    organization_id: str = Field(description="UUID организации")
    organization_name: str = Field(description="Название организации")
    role: str = Field(description="Назначенная роль (employee)")


class InviteCodeResponse(BaseModel):
    invite_code: str = Field(description="Новый инвайт-код")


class MemberRoleUpdate(BaseModel):
    role: str = Field(description="Новая роль: admin или employee")
