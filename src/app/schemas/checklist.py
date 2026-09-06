import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from src.app.models.checklist import PhotoRequirement, PhotoSource
from src.app.schemas.shift import ShiftWorkLocation


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
    is_deleted: bool = Field(description="Шаблон удалён (мягкое удаление)")
    deleted_at: datetime | None = Field(default=None, description="Момент удаления или null")
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TemplateListResponse(BaseModel):
    items: list[TemplateResponse]


class TemplateDeletedResponse(BaseModel):
    deleted: bool = Field(description="Шаблон удалён (мягкое удаление)")


class TemplateItemCreate(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    is_required: bool = Field(default=False)
    photo_requirement: PhotoRequirement = Field(default=PhotoRequirement.none)
    photo_source: PhotoSource = Field(default=PhotoSource.camera)


class TemplateItemUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    is_required: bool | None = Field(default=None)
    photo_requirement: PhotoRequirement | None = Field(default=None)
    photo_source: PhotoSource | None = Field(default=None)


class TemplateItemResponse(BaseModel):
    id: str
    text: str
    is_required: bool
    position: int
    photo_requirement: str
    photo_source: str

    model_config = {"from_attributes": True}


class TemplateDetailResponse(BaseModel):
    id: str
    name: str
    type: str
    is_required: bool
    is_deleted: bool = Field(description="Шаблон удалён (мягкое удаление)")
    deleted_at: datetime | None = Field(default=None, description="Момент удаления или null")
    created_at: datetime
    updated_at: datetime
    items: list[TemplateItemResponse]
    schedule_ids: list[str] = Field(default_factory=list)


class ItemsReorderRequest(BaseModel):
    item_ids: list[str] = Field(
        min_length=1,
        description="UUIDы пунктов в новом порядке",
    )


class RoleAssignmentRequest(BaseModel):
    role_ids: list[str] = Field(
        description="UUIDы ролей. Передавайте полный список — PUT-семантика (замена)",
    )


class TemplateLocationAssignmentRequest(BaseModel):
    location_ids: list[str] = Field(
        description=(
            "UUIDы рабочих точек. Передавайте полный список — PUT-семантика "
            "(замена). Пустой список снимает все привязки (шаблон снова "
            "действует на всех точках)."
        ),
    )


class TemplateScheduleAssignmentRequest(BaseModel):
    schedule_ids: list[str] = Field(
        description="UUIDы графиков. Передавайте полный список — PUT-семантика (замена)",
    )


class LocationTemplateAssignmentRequest(BaseModel):
    template_ids: list[str] = Field(
        description=(
            "UUIDы шаблонов чек-листов. Передавайте полный список — "
            "PUT-семантика (замена набора шаблонов у точки)."
        ),
    )


class LocationTemplateResponse(BaseModel):
    id: str
    name: str
    type: str
    is_required: bool
    is_deleted: bool = Field(
        description="Удалённые включаются в выдачу — админ видит, что привязка существует",
    )


class LocationTemplatesResponse(BaseModel):
    items: list[LocationTemplateResponse]


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
    location_ids: list[str] = Field(
        description="Точки, к которым привязан шаблон. Пустой список = действует на всех точках",
    )
    schedule_ids: list[str] = Field(
        default_factory=list,
        description="Графики, к которым привязан шаблон",
    )


class EffectiveTemplateResponse(BaseModel):
    id: str
    name: str
    type: str
    is_required: bool
    source: str = Field(description="role | personal_add")
    location_ids: list[str] = Field(
        description="Точки, к которым привязан шаблон. Пустой список = действует на всех точках",
    )


class EffectiveTemplatesResponse(BaseModel):
    items: list[EffectiveTemplateResponse]


class ItemsSummary(BaseModel):
    total: int
    completed: int = Field(description="Пунктов с is_completed=true (без учёта фото)")
    satisfied_count: int = Field(
        description="Пунктов, прошедших критерий satisfied (с учётом обязательных фото)",
    )
    photos_required_missing: int = Field(
        description="Пунктов с photo_requirement=required и без фото (бейдж «нужно фото»)",
    )


class ChecklistInstanceResponse(BaseModel):
    id: str
    name: str
    type: str
    is_required: bool
    status: str = Field(description="pending | completed | incomplete")
    completed_at: datetime | None
    items_summary: ItemsSummary
    created_at: datetime


class ChecklistInstanceListResponse(BaseModel):
    items: list[ChecklistInstanceResponse]
    organization_timezone: str | None = Field(
        default=None,
        description="Текущая IANA-таймзона организации смены; null для персональной смены",
    )
    fill_allowed: bool = Field(
        default=True,
        description="Можно ли сейчас редактировать чек-листы смены "
        "(checklist_grace_period): true для активной смены и для завершённой "
        "в открытом окне дозаполнения, false после его истечения",
    )
    fill_deadline_at: datetime | None = Field(
        default=None,
        description="Момент закрытия окна дозаполнения в UTC; null для активной "
        "смены или уже закрытого окна (checklist_grace_period)",
    )


class PhotoResponse(BaseModel):
    id: str
    file_id: str
    url: str | None = Field(
        default=None,
        description="Свежий presigned GET URL; null при недоступности storage",
    )
    url_expires_at: datetime | None = None
    captured_at: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    position: int


class InstanceItemResponse(BaseModel):
    id: str
    text: str
    is_required: bool
    position: int
    is_completed: bool
    comment: str | None
    completed_at: datetime | None
    change_count: int
    photo_requirement: str
    photo_source: str
    photos_count: int = 0
    photos: list[PhotoResponse] = Field(default_factory=list)


class ChecklistInstanceDetailResponse(BaseModel):
    id: str
    name: str
    type: str
    is_required: bool
    status: str
    completed_at: datetime | None
    created_at: datetime
    max_photos_per_item: int = Field(
        description="Лимит фото на пункт (= CHECKLIST_MAX_PHOTOS_PER_ITEM)",
    )
    items: list[InstanceItemResponse]
    organization_timezone: str | None = Field(
        default=None,
        description="Текущая IANA-таймзона организации смены; null для персональной смены",
    )
    fill_allowed: bool = Field(
        default=True,
        description="Можно ли сейчас редактировать чек-лист смены (checklist_grace_period): "
        "true для активной смены и для завершённой в открытом окне дозаполнения, "
        "false после его истечения",
    )
    fill_deadline_at: datetime | None = Field(
        default=None,
        description="Момент закрытия окна дозаполнения в UTC; null для активной "
        "смены или уже закрытого окна (checklist_grace_period)",
    )


class InstanceItemUpdate(BaseModel):
    is_completed: bool = Field(description="Отмечено/снято")
    comment: str | None = Field(default=None, max_length=2000)


class PhotoBindRequest(BaseModel):
    file_id: uuid.UUID = Field(description="UUID ранее загруженного файла checklist_photo")
    captured_at: datetime | None = Field(
        default=None,
        description="Момент съёмки на клиенте, UTC (суффикс Z)",
    )
    latitude: float | None = Field(default=None)
    longitude: float | None = Field(default=None)


class OrgChecklistInstanceResponse(BaseModel):
    """Строка реестра `GET /organizations/{org_id}/checklist-instances` (checklist_reports)."""

    id: str
    shift_id: str
    template_id: str | None = Field(
        description="null, если шаблон-источник удалён (ON DELETE SET NULL); "
        "name/type экземпляра — снимок, строка остаётся читаемой"
    )
    name: str
    type: str = Field(description="shift_start | shift_end")
    is_required: bool
    status: str = Field(description="pending | completed | incomplete")
    completed_at: datetime | None
    created_at: datetime
    items_summary: ItemsSummary
    photos_count: int = Field(description="Всего фото по всем пунктам экземпляра")
    user_id: str
    user_name: str | None = Field(default=None, description="null, если пользователь удалён")
    display_name: str | None = Field(
        default=None,
        description="Имя сотрудника в этой организации; null — не задано (member_display_name)",
    )
    user_email: str | None = Field(default=None, description="null, если пользователь удалён")
    shift_started_at: datetime
    shift_finished_at: datetime | None
    shift_status: str = Field(description="active | paused | finished")
    work_location: ShiftWorkLocation | None = Field(
        default=None, description="Денормализованная точка смены, null — нет точки"
    )


class OrgChecklistInstanceListResponse(BaseModel):
    items: list[OrgChecklistInstanceResponse]
    total: int
    limit: int
    offset: int
