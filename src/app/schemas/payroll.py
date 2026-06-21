import enum
from datetime import datetime

from pydantic import BaseModel, Field

from src.app.models.member_rate import RateType


class Granularity(enum.StrEnum):
    """Уровень суточной разбивки детального отчёта по зарплате."""

    none = "none"
    day = "day"
    week = "week"
    month = "month"


class ExportFormat(enum.StrEnum):
    """Поддерживаемые форматы экспорта (на старте — только xlsx)."""

    xlsx = "xlsx"


class RateCreate(BaseModel):
    rate_amount_minor: int = Field(
        gt=0,
        description="Ставка в копейках (целое, > 0). Смысл задаёт rate_type",
    )
    rate_type: RateType = Field(
        description="Тип ставки: hourly (₽/час) или per_shift (₽/смена)",
    )
    currency: str = Field(
        default="RUB",
        pattern=r"^[A-Z]{3}$",
        description="Валюта (ISO 4217); на старте всегда RUB",
    )
    effective_from: datetime = Field(
        description="Момент, с которого ставка действует (UTC)",
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description="Необязательный комментарий",
    )


class RateUpdate(BaseModel):
    """Исправление существующей записи истории. Все поля опциональны."""

    rate_amount_minor: int | None = Field(default=None, gt=0)
    rate_type: RateType | None = Field(default=None)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    effective_from: datetime | None = Field(default=None)
    note: str | None = Field(default=None, max_length=500)


class RateResponse(BaseModel):
    id: str = Field(description="UUID записи ставки")
    member_id: str = Field(description="UUID участника (organization_members.id)")
    rate_amount_minor: int = Field(description="Ставка в копейках")
    rate_type: str = Field(description="Тип ставки: hourly или per_shift")
    currency: str = Field(description="Валюта")
    effective_from: datetime = Field(description="Момент начала действия (UTC)")
    note: str | None = Field(default=None, description="Комментарий")
    created_at: datetime = Field(description="Момент создания записи")

    model_config = {"from_attributes": True}


class RateListResponse(BaseModel):
    items: list[RateResponse] = Field(
        description="История ставок, сортировка по effective_from DESC",
    )


class CurrentRateResponse(BaseModel):
    """Действующая ставка: строка истории с максимальным effective_from <= now."""

    rate_amount_minor: int = Field(description="Ставка в копейках")
    rate_type: str = Field(description="Тип ставки: hourly или per_shift")
    currency: str = Field(description="Валюта")
    effective_from: datetime = Field(description="Момент начала действия (UTC)")


class RateDeleteResponse(BaseModel):
    deleted: bool = Field(description="Запись истории удалена")


class PayrollPeriod(BaseModel):
    date_from: datetime | None = Field(
        default=None,
        description="Нижняя граница периода (UTC) или null",
    )
    date_to: datetime | None = Field(
        default=None,
        description="Верхняя граница периода (UTC, включительно) или null",
    )


class PayrollItemResponse(BaseModel):
    user_id: str = Field(description="UUID сотрудника")
    user_name: str = Field(description="Имя сотрудника")
    worked_seconds: int = Field(
        description="Отработанное время по завершённым сменам периода (вкл. неоплаченные)",
    )
    shifts_count: int = Field(description="Число завершённых смен в периоде")
    gross_amount_minor: int = Field(
        description="Начисление в копейках (half-up, округлено один раз на итог)",
    )
    unpaid_seconds: int = Field(
        description="Время смен, для которых не нашлось действующей ставки",
    )
    unpaid_shifts_count: int = Field(description="Число смен без действующей ставки")
    has_missing_rate: bool = Field(
        description="true, если у сотрудника были смены без ставки",
    )


class PayrollTotalsResponse(BaseModel):
    worked_seconds: int = Field(description="Суммарное время по всем сотрудникам")
    shifts_count: int = Field(description="Суммарное число смен")
    gross_amount_minor: int = Field(
        description="Сумма округлённых итогов сотрудников, в копейках",
    )


class PayrollResponse(BaseModel):
    period: PayrollPeriod = Field(description="Применённый период")
    currency: str = Field(description="Валюта отчёта (RUB)")
    items: list[PayrollItemResponse] = Field(
        description="По одному элементу на сотрудника с завершёнными сменами в периоде",
    )
    totals: PayrollTotalsResponse = Field(description="Суммы по всем сотрудникам")


class PayrollBreakdownBucket(BaseModel):
    """Корзина суточной разбивки (day/week/month) — агрегат смен корзины.

    Атомарная единица округления денег — день: `gross_amount_minor` корзин
    week/month и итог сотрудника складываются из уже округлённых дневных сумм.
    """

    bucket_start: str = Field(
        description="ISO-дата начала корзины в tz отчёта (день/понедельник недели/1-е месяца)",
    )
    worked_seconds: int = Field(description="Отработанное время смен корзины (вкл. неоплаченные)")
    shifts_count: int = Field(description="Число завершённых смен в корзине")
    gross_amount_minor: int = Field(
        description="Начисление за корзину в копейках (сумма округлённых дневных значений)",
    )
    unpaid_seconds: int = Field(description="Время смен корзины без действующей ставки")
    has_missing_rate: bool = Field(description="true, если в корзине были смены без ставки")


class PayrollDetailedItem(PayrollItemResponse):
    """Строка сотрудника с суточной разбивкой (granularity != none)."""

    gross_amount_minor: int = Field(
        description="Начисление в копейках (сумма округлённых по дням значений, см. ADR-002)",
    )
    breakdown: list[PayrollBreakdownBucket] = Field(
        description="Корзины с ненулевым числом смен, сортировка по bucket_start ASC",
    )


class PayrollDetailedResponse(BaseModel):
    period: PayrollPeriod = Field(description="Применённый период")
    granularity: str = Field(description="Применённый уровень разбивки (day/week/month)")
    tz: str = Field(description="Применённая таймзона нарезки корзин (IANA)")
    currency: str = Field(description="Валюта отчёта (RUB)")
    items: list[PayrollDetailedItem] = Field(
        description="По одному элементу на сотрудника с разбивкой по корзинам",
    )
    totals: PayrollTotalsResponse = Field(description="Суммы по всем сотрудникам")


class MyEarningsResponse(BaseModel):
    period: PayrollPeriod = Field(description="Применённый период")
    currency: str = Field(description="Валюта (RUB)")
    worked_seconds: int = Field(description="Отработанное время за период")
    shifts_count: int = Field(description="Число завершённых смен за период")
    gross_amount_minor: int = Field(description="Заработок в копейках (half-up один раз)")
    current_rate: CurrentRateResponse | None = Field(
        default=None,
        description="Действующая на сейчас ставка или null",
    )
    has_missing_rate: bool = Field(
        description="true, если в периоде были смены без действующей ставки",
    )
