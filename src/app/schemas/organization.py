from datetime import datetime

from pydantic import BaseModel, Field

from src.app.schemas.organization_role import RoleResponse


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
    geo_check_enabled: bool = Field(description="Геопроверка при начале смены")
    created_at: datetime = Field(description="Дата создания")
    my_role: str | None = Field(
        default=None,
        description="Роль текущего пользователя: owner, admin или employee. null только в "
        "/organizations/all для super_admin, не состоящего в организации.",
    )
    my_custom_role: RoleResponse | None = Field(
        default=None,
        description="Кастомная роль текущего пользователя (у owner всегда null)",
    )

    model_config = {"from_attributes": True}


class OrganizationListResponse(BaseModel):
    items: list[OrganizationResponse] = Field(description="Список организаций")


class MemberResponse(BaseModel):
    id: str = Field(description="UUID записи об участии")
    organization_id: str = Field(description="UUID организации")
    user_id: str = Field(description="UUID пользователя")
    user_name: str = Field(description="Имя участника")
    user_email: str = Field(description="Email участника")
    role: str = Field(description="Системная роль: admin или employee")
    custom_role: RoleResponse | None = Field(
        default=None,
        description="Кастомная роль организации (или null)",
    )
    joined_at: datetime = Field(description="Дата присоединения")

    model_config = {"from_attributes": True}


class MemberListResponse(BaseModel):
    items: list[MemberResponse] = Field(description="Список участников")


class JoinResponse(BaseModel):
    organization_id: str = Field(description="UUID организации")
    organization_name: str = Field(description="Название организации")
    role: str = Field(description="Назначенная роль (employee)")
    geo_check_enabled: bool = Field(description="Геопроверка при начале смены")


class InviteCodeResponse(BaseModel):
    invite_code: str = Field(description="Новый инвайт-код")


class MemberRoleUpdate(BaseModel):
    role: str = Field(description="Новая роль: admin или employee")
