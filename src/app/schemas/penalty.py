import uuid
from datetime import datetime

from pydantic import BaseModel, Field

CURRENCY_PATTERN = r"^[A-Z]{3}$"


# --- Шаблоны штрафов ---------------------------------------------------------
class PenaltyTemplateCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=200, description="Причина/название штрафа")
    amount_minor: int = Field(gt=0, description="Сумма в копейках (> 0)")
    currency: str = Field(
        default="RUB",
        pattern=CURRENCY_PATTERN,
        description="Валюта (ISO 4217); на старте всегда RUB",
    )


class PenaltyTemplateUpdate(BaseModel):
    """Исправление шаблона. Все поля опциональны."""

    reason: str | None = Field(default=None, min_length=1, max_length=200)
    amount_minor: int | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)


class PenaltyTemplateResponse(BaseModel):
    id: str = Field(description="UUID шаблона")
    reason: str = Field(description="Причина/название")
    amount_minor: int = Field(description="Сумма в копейках")
    currency: str = Field(description="Валюта")
    is_deleted: bool = Field(description="Шаблон удалён (мягкое удаление)")
    deleted_at: datetime | None = Field(default=None, description="Момент удаления или null")
    created_at: datetime = Field(description="Момент создания")
    updated_at: datetime = Field(description="Момент последнего изменения")


class PenaltyTemplateListResponse(BaseModel):
    items: list[PenaltyTemplateResponse] = Field(
        description="Активные шаблоны организации, сортировка по created_at DESC",
    )


# --- Штрафы ------------------------------------------------------------------
class PenaltyCreate(BaseModel):
    member_id: uuid.UUID = Field(description="Кому штраф (organization_members.id)")
    template_id: uuid.UUID | None = Field(
        default=None,
        description="Происхождение из шаблона (опционально)",
    )
    reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Причина; из шаблона при template_id или явное переопределение",
    )
    amount_minor: int | None = Field(
        default=None,
        gt=0,
        description="Сумма в копейках (> 0); из шаблона при template_id или переопределение",
    )
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN, description="Валюта")
    shift_id: uuid.UUID | None = Field(
        default=None,
        description="Привязка к смене этого сотрудника (опционально)",
    )
    occurred_at: datetime | None = Field(
        default=None,
        description="Дата/момент штрафа (UTC). Обязателен, если shift_id не задан",
    )
    comment: str | None = Field(default=None, max_length=500, description="Доп. комментарий")


class PenaltyUpdate(BaseModel):
    """Исправление штрафа. Все поля опциональны. member_id не меняется."""

    reason: str | None = Field(default=None, min_length=1, max_length=200)
    amount_minor: int | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)
    shift_id: uuid.UUID | None = Field(
        default=None,
        description="Переустановить на другую смену сотрудника или обнулить (null)",
    )
    occurred_at: datetime | None = Field(default=None)
    comment: str | None = Field(default=None, max_length=500)


class PenaltyResponse(BaseModel):
    id: str = Field(description="UUID штрафа")
    member_id: str = Field(description="UUID участника (organization_members.id)")
    user_id: str = Field(description="UUID сотрудника")
    user_name: str = Field(description="Настоящее имя сотрудника (User.name)")
    display_name: str | None = Field(
        default=None,
        description="Имя сотрудника в этой организации; null — не задано (member_display_name)",
    )
    template_id: str | None = Field(default=None, description="UUID шаблона или null")
    reason: str = Field(description="Причина (снимок)")
    amount_minor: int = Field(description="Сумма в копейках (снимок)")
    currency: str = Field(description="Валюта")
    shift_id: str | None = Field(default=None, description="UUID смены или null")
    occurred_at: datetime = Field(description="Дата/момент штрафа (UTC)")
    comment: str | None = Field(default=None, description="Доп. комментарий или null")
    created_by_user_id: str = Field(description="UUID назначившего (admin/owner)")
    is_deleted: bool = Field(description="Штраф снят (мягкое удаление)")
    deleted_at: datetime | None = Field(default=None, description="Момент снятия или null")
    created_at: datetime = Field(description="Момент создания")
    updated_at: datetime = Field(description="Момент последнего изменения")


class PenaltyListResponse(BaseModel):
    items: list[PenaltyResponse] = Field(description="Активные штрафы, occurred_at DESC")
    total: int = Field(description="Число активных штрафов под фильтром (без пагинации)")
    limit: int = Field(description="Размер страницы")
    offset: int = Field(description="Смещение")


class MyPenaltyResponse(BaseModel):
    id: str = Field(description="UUID штрафа")
    reason: str = Field(description="Причина")
    amount_minor: int = Field(description="Сумма в копейках")
    currency: str = Field(description="Валюта")
    shift_id: str | None = Field(default=None, description="UUID смены или null")
    occurred_at: datetime = Field(description="Дата/момент штрафа (UTC)")
    comment: str | None = Field(default=None, description="Доп. комментарий или null")
    created_at: datetime = Field(description="Момент создания")


class MyPenaltyListResponse(BaseModel):
    items: list[MyPenaltyResponse] = Field(
        description="Свои активные штрафы, сортировка по occurred_at DESC",
    )
    total: int = Field(description="Число своих активных штрафов под фильтром")
    limit: int = Field(description="Размер страницы")
    offset: int = Field(description="Смещение")


class PenaltyDeletedResponse(BaseModel):
    deleted: bool = Field(description="Штраф снят (soft-delete)")


class PenaltyTemplateDeletedResponse(BaseModel):
    deleted: bool = Field(description="Шаблон удалён (soft-delete)")
