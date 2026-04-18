from datetime import datetime

from pydantic import BaseModel, Field


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="Название шаблона")
    type: str = Field(
        description="Тип привязки к смене: shift_start или shift_end",
    )
    is_required: bool = Field(
        default=False,
        description="Обязательный шаблон (влияет на incomplete-статус при завершении смены)",
    )


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    type: str | None = Field(
        default=None,
        description="shift_start или shift_end",
    )
    is_required: bool | None = Field(default=None)


class TemplateResponse(BaseModel):
    id: str
    name: str
    type: str
    is_required: bool
    items_count: int = Field(description="Количество пунктов в шаблоне")
    is_archived: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TemplateListResponse(BaseModel):
    items: list[TemplateResponse]


class TemplateItemCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    is_required: bool = Field(default=False)


class TemplateItemUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    is_required: bool | None = Field(default=None)


class TemplateItemResponse(BaseModel):
    id: str
    text: str
    is_required: bool
    position: int

    model_config = {"from_attributes": True}


class TemplateDetailResponse(BaseModel):
    id: str
    name: str
    type: str
    is_required: bool
    is_archived: bool
    created_at: datetime
    updated_at: datetime
    items: list[TemplateItemResponse]


class ItemsReorderRequest(BaseModel):
    item_ids: list[str] = Field(
        min_length=1,
        description="UUIDы пунктов в новом порядке",
    )


class RoleAssignmentRequest(BaseModel):
    role_ids: list[str] = Field(
        description="UUIDы ролей. Передавайте полный список — PUT-семантика (замена)",
    )


class OverrideItem(BaseModel):
    template_id: str = Field(description="UUID шаблона")
    type: str = Field(description="add — добавить поверх роли, remove — исключить")


class MemberOverrideRequest(BaseModel):
    overrides: list[OverrideItem] = Field(
        description="Полный список личных переопределений для сотрудника (PUT-семантика)",
    )


class MemberInfo(BaseModel):
    user_id: str
    user_name: str
    user_email: str


class AssignmentResponse(BaseModel):
    template_id: str
    role_ids: list[str] = Field(description="Роли, которым назначен шаблон")
    personal_add: list[MemberInfo] = Field(description="Сотрудники с личным add")
    personal_remove: list[MemberInfo] = Field(description="Сотрудники с личным remove")


class EffectiveTemplateResponse(BaseModel):
    id: str
    name: str
    type: str
    is_required: bool
    source: str = Field(description="role | personal_add")


class EffectiveTemplatesResponse(BaseModel):
    items: list[EffectiveTemplateResponse]
