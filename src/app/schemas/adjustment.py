import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

CURRENCY_PATTERN = r"^[A-Z]{3}$"

CategoryName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]


def _validate_nonzero(value: int) -> int:
    if value == 0:
        raise ValueError("amount_minor не может быть равен 0")
    return value


class AdjustmentCreate(BaseModel):
    member_id: uuid.UUID = Field(description="Кому начисление (organization_members.id)")
    amount_minor: int = Field(
        description="Знаковая сумма в копейках: > 0 — доплата, < 0 — удержание (!= 0)",
    )
    currency: str = Field(
        default="RUB",
        pattern=CURRENCY_PATTERN,
        description="Валюта (ISO 4217); на старте всегда RUB",
    )
    reason: str = Field(min_length=1, max_length=200, description="Основание начисления")
    occurred_at: datetime | None = Field(
        default=None,
        description="Дата, к которой относится начисление (UTC). Обязателен, если shift_id "
        "не задан; при переданном shift_id по умолчанию = started_at смены",
    )
    shift_id: uuid.UUID | None = Field(
        default=None,
        description="Необязательная привязка к смене этого сотрудника",
    )
    comment: str | None = Field(default=None, max_length=500, description="Свободный комментарий")
    category_id: uuid.UUID | None = Field(
        default=None,
        description="Категория начисления (payroll_adjustment_categories.id) или null — "
        "«Без категории»; категория должна быть неудалённой и принадлежать организации",
    )

    _amount_nonzero = field_validator("amount_minor")(_validate_nonzero)


class AdjustmentUpdate(BaseModel):
    """Исправление начисления. Все поля опциональны. member_id не меняется."""

    amount_minor: int | None = Field(default=None)
    reason: str | None = Field(default=None, min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=500)
    occurred_at: datetime | None = Field(default=None)
    shift_id: uuid.UUID | None = Field(
        default=None,
        description="Переустановить на другую смену сотрудника или обнулить (null)",
    )
    category_id: uuid.UUID | None = Field(
        default=None,
        description="Назначить другую (неудалённую) категорию или сбросить в null "
        "(«Без категории»); поле не передано — категория не меняется",
    )

    @field_validator("amount_minor")
    @classmethod
    def _amount_nonzero(cls, value: int | None) -> int | None:
        return _validate_nonzero(value) if value is not None else value


class AdjustmentResponse(BaseModel):
    id: str = Field(description="UUID начисления")
    organization_id: str = Field(description="UUID организации")
    member_id: str = Field(description="UUID участника (organization_members.id)")
    user_id: str = Field(description="UUID сотрудника")
    user_name: str = Field(description="Настоящее имя сотрудника (User.name)")
    display_name: str | None = Field(
        default=None,
        description="Имя сотрудника в этой организации; null — не задано (member_display_name)",
    )
    shift_id: str | None = Field(default=None, description="UUID смены или null")
    category_id: str | None = Field(
        default=None, description="UUID категории начисления или null («Без категории»)"
    )
    category_name: str | None = Field(
        default=None,
        description="Имя категории (в т.ч. удалённой) или null («Без категории»)",
    )
    amount_minor: int = Field(description="Знаковая сумма в копейках")
    currency: str = Field(description="Валюта")
    reason: str = Field(description="Основание начисления")
    comment: str | None = Field(default=None, description="Свободный комментарий или null")
    occurred_at: datetime = Field(description="Дата/момент начисления (UTC)")
    created_by_user_id: str = Field(description="UUID назначившего (owner/admin)")
    created_by_name: str = Field(description="Имя назначившего")
    is_deleted: bool = Field(description="Начисление отменено (мягкое удаление)")
    deleted_at: datetime | None = Field(default=None, description="Момент отмены или null")
    created_at: datetime = Field(description="Момент создания")


class AdjustmentListResponse(BaseModel):
    items: list[AdjustmentResponse] = Field(
        description="Активные начисления организации, occurred_at DESC",
    )
    total: int = Field(description="Число активных начислений под фильтром (без пагинации)")
    limit: int = Field(description="Размер страницы")
    offset: int = Field(description="Смещение")


class MyAdjustmentResponse(BaseModel):
    id: str = Field(description="UUID начисления")
    amount_minor: int = Field(description="Знаковая сумма в копейках")
    currency: str = Field(description="Валюта")
    reason: str = Field(description="Основание начисления")
    comment: str | None = Field(default=None, description="Свободный комментарий или null")
    occurred_at: datetime = Field(description="Дата/момент начисления (UTC)")
    shift_id: str | None = Field(default=None, description="UUID смены или null")
    category_id: str | None = Field(
        default=None, description="UUID категории начисления или null («Без категории»)"
    )
    category_name: str | None = Field(
        default=None,
        description="Имя категории (в т.ч. удалённой) или null («Без категории»)",
    )
    created_at: datetime = Field(description="Момент создания")


class MyAdjustmentListResponse(BaseModel):
    items: list[MyAdjustmentResponse] = Field(
        description="Свои активные начисления, сортировка по occurred_at DESC",
    )
    total: int = Field(description="Число своих активных начислений под фильтром")
    limit: int = Field(description="Размер страницы")
    offset: int = Field(description="Смещение")


class AdjustmentDeletedResponse(BaseModel):
    deleted: bool = Field(description="Начисление отменено (soft-delete)")


class AdjustmentCategoryCreate(BaseModel):
    name: CategoryName = Field(
        description="Название категории (trim, 1..100); уникально среди живых категорий "
        "организации без учёта регистра",
    )


class AdjustmentCategoryUpdate(BaseModel):
    """Переименование категории."""

    name: CategoryName = Field(description="Новое название (trim, 1..100)")


class AdjustmentCategoryResponse(BaseModel):
    id: str = Field(description="UUID категории")
    organization_id: str = Field(description="UUID организации")
    name: str = Field(description="Название категории")
    is_deleted: bool = Field(description="Категория удалена (soft-delete, ADR-003)")
    deleted_at: datetime | None = Field(default=None, description="Момент удаления или null")
    created_at: datetime = Field(description="Момент создания")
    adjustments_count: int = Field(
        description="Число неотменённых начислений с этой категорией (подсказка при удалении)",
    )


class AdjustmentCategoryListResponse(BaseModel):
    items: list[AdjustmentCategoryResponse] = Field(
        description="Категории организации, сортировка по lower(name), без пагинации",
    )


class AdjustmentCategoryDeletedResponse(BaseModel):
    deleted: bool = Field(description="Категория удалена (soft-delete)")
