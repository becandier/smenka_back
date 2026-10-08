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


class PayrollCategoryAmount(BaseModel):
    """Сумма ручных начислений одной категории (payroll_breakdown)."""

    category_id: str | None = Field(
        default=None, description="UUID категории или null — «Без категории»"
    )
    category_name: str | None = Field(
        default=None,
        description="Имя категории (в т.ч. удалённой) или null — «Без категории»",
    )
    category_is_deleted: bool = Field(
        default=False,
        description="Категория удалена (soft-delete); false для «Без категории». При "
        "одинаковом имени живая идёт раньше удалённой",
    )
    amount_minor: int = Field(description="Знаковая сумма начислений категории, в копейках")
    accrual_minor: int = Field(description="Сумма положительных начислений категории (≥ 0)")
    deduction_minor: int = Field(description="Сумма удержаний категории по модулю (≥ 0)")
    count: int = Field(description="Число начислений категории")


_BASE_AMOUNT_DESCRIPTION = (
    "Оплата за отработанное время, в копейках (half-up на смену); "
    "base_amount_minor + overtime_amount_minor == gross_amount_minor"
)
_OVERTIME_AMOUNT_DESCRIPTION = (
    "Оплата согласованной переработки, в копейках: по сменам amount(ставка, время + "
    "переработка) − amount(ставка, время); для per_shift — 0"
)
_ACCRUAL_DESCRIPTION = "Сумма положительных ручных начислений (≥ 0), в копейках"
_DEDUCTION_DESCRIPTION = (
    "Сумма удержаний по модулю (≥ 0), в копейках; "
    "adjustment_accrual_minor − adjustment_deduction_minor == adjustment_amount_minor"
)
_BY_CATEGORY_DESCRIPTION = (
    "Суммы ручных начислений по категориям: сортировка по lower(category_name), "
    "«Без категории» (category_id = null) — последней; пусто, если начислений нет"
)


class PayrollItemResponse(BaseModel):
    user_id: str = Field(description="UUID сотрудника")
    user_name: str = Field(
        description="Настоящее имя сотрудника (User.name) — денежный документ, "
        "основная строка всегда настоящее имя"
    )
    display_name: str | None = Field(
        default=None,
        description="Имя сотрудника в этой организации; null — не задано "
        "(member_display_name; для payroll — подпись, не основная строка)",
    )
    worked_seconds: int = Field(
        description="Отработанное время по завершённым сменам периода (вкл. неоплаченные)",
    )
    overtime_seconds: int = Field(
        default=0,
        description="Согласованная переработка (approved-заявки) в секундах (work_schedules)",
    )
    shifts_count: int = Field(description="Число завершённых смен в периоде")
    gross_amount_minor: int = Field(
        description="Начисление в копейках (half-up, округлено один раз на итог); для "
        "hourly-ставки включает согласованную переработку",
    )
    base_amount_minor: int = Field(default=0, description=_BASE_AMOUNT_DESCRIPTION)
    overtime_amount_minor: int = Field(default=0, description=_OVERTIME_AMOUNT_DESCRIPTION)
    unpaid_seconds: int = Field(
        description="Время смен, для которых не нашлось действующей ставки",
    )
    unpaid_shifts_count: int = Field(description="Число смен без действующей ставки")
    has_missing_rate: bool = Field(
        description="true, если у сотрудника были смены без ставки",
    )
    penalty_amount_minor: int = Field(
        default=0,
        description="Сумма активных штрафов сотрудника за период, в копейках",
    )
    penalties_count: int = Field(default=0, description="Число активных штрафов за период")
    adjustment_amount_minor: int = Field(
        default=0,
        description="Знаковая сумма активных ручных начислений сотрудника за период, в копейках "
        "(manual_time_entry)",
    )
    adjustment_accrual_minor: int = Field(default=0, description=_ACCRUAL_DESCRIPTION)
    adjustment_deduction_minor: int = Field(default=0, description=_DEDUCTION_DESCRIPTION)
    adjustments_count: int = Field(default=0, description="Число активных ручных начислений")
    adjustments_by_category: list[PayrollCategoryAmount] = Field(
        default_factory=list, description=_BY_CATEGORY_DESCRIPTION
    )
    net_amount_minor: int = Field(
        description="К выплате: gross_amount_minor − penalty_amount_minor + "
        "adjustment_amount_minor (может быть < 0)",
    )
    planned_seconds: int = Field(
        default=0,
        description="Плановое время по графику (смены без графика — план = факт, work_schedules)",
    )
    planned_amount_minor: int = Field(
        default=0,
        description="Плановые деньги: planned_seconds × действующая ставка (hourly); "
        "для per_shift равно gross_amount_minor (план = факт)",
    )
    delta_amount_minor: int = Field(
        default=0,
        description="gross_amount_minor − planned_amount_minor; отрицательное = недозаработал",
    )
    late_count: int = Field(default=0, description="Число смен с опозданием (после допуска)")
    late_seconds_total: int = Field(
        default=0, description="Суммарное опоздание в секундах (после допуска)"
    )


class PayrollTotalsResponse(BaseModel):
    worked_seconds: int = Field(description="Суммарное время по всем сотрудникам")
    overtime_seconds: int = Field(
        default=0, description="Суммарная согласованная переработка в секундах"
    )
    shifts_count: int = Field(description="Суммарное число смен")
    gross_amount_minor: int = Field(
        description="Сумма округлённых итогов сотрудников, в копейках",
    )
    base_amount_minor: int = Field(default=0, description=_BASE_AMOUNT_DESCRIPTION)
    overtime_amount_minor: int = Field(default=0, description=_OVERTIME_AMOUNT_DESCRIPTION)
    penalty_amount_minor: int = Field(
        default=0,
        description="Сумма штрафов по всем сотрудникам, в копейках",
    )
    penalties_count: int = Field(default=0, description="Суммарное число активных штрафов")
    adjustment_amount_minor: int = Field(
        default=0, description="Сумма знаковых ручных начислений по всем сотрудникам, в копейках"
    )
    adjustment_accrual_minor: int = Field(default=0, description=_ACCRUAL_DESCRIPTION)
    adjustment_deduction_minor: int = Field(default=0, description=_DEDUCTION_DESCRIPTION)
    adjustments_count: int = Field(
        default=0, description="Суммарное число активных ручных начислений"
    )
    adjustments_by_category: list[PayrollCategoryAmount] = Field(
        default_factory=list,
        description="Агрегат adjustments_by_category по всем items (та же форма и сортировка)",
    )
    net_amount_minor: int = Field(
        description="Сумма «к выплате» по всем сотрудникам (может быть < 0)",
    )
    planned_seconds: int = Field(default=0, description="Суммарное плановое время")
    planned_amount_minor: int = Field(default=0, description="Суммарные плановые деньги")
    delta_amount_minor: int = Field(default=0, description="Сумма дельт (может быть < 0)")
    late_count: int = Field(default=0, description="Суммарное число опозданий")
    late_seconds_total: int = Field(default=0, description="Суммарное опоздание в секундах")


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
    overtime_seconds: int = Field(default=0, description="Согласованная переработка корзины")
    shifts_count: int = Field(description="Число завершённых смен в корзине")
    gross_amount_minor: int = Field(
        description="Начисление за корзину в копейках (сумма округлённых дневных значений)",
    )
    base_amount_minor: int = Field(default=0, description=_BASE_AMOUNT_DESCRIPTION)
    overtime_amount_minor: int = Field(default=0, description=_OVERTIME_AMOUNT_DESCRIPTION)
    unpaid_seconds: int = Field(description="Время смен корзины без действующей ставки")
    has_missing_rate: bool = Field(description="true, если в корзине были смены без ставки")
    planned_seconds: int = Field(default=0, description="Плановое время корзины")
    planned_amount_minor: int = Field(default=0, description="Плановые деньги корзины")
    delta_amount_minor: int = Field(
        default=0, description="gross_amount_minor − planned_amount_minor корзины"
    )
    late_count: int = Field(default=0, description="Число опозданий в корзине")
    late_seconds_total: int = Field(default=0, description="Суммарное опоздание корзины, сек")


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
    overtime_seconds: int = Field(
        default=0, description="Согласованная переработка за период, секунды"
    )
    shifts_count: int = Field(description="Число завершённых смен за период")
    gross_amount_minor: int = Field(description="Заработок в копейках (half-up один раз)")
    penalty_amount_minor: int = Field(
        default=0,
        description="Сумма своих активных штрафов за период, в копейках",
    )
    penalties_count: int = Field(default=0, description="Число своих активных штрафов за период")
    adjustment_amount_minor: int = Field(
        default=0, description="Сумма своих знаковых ручных начислений за период, в копейках"
    )
    adjustments_count: int = Field(
        default=0, description="Число своих активных ручных начислений за период"
    )
    net_amount_minor: int = Field(
        description="К выплате: gross_amount_minor − penalty_amount_minor + "
        "adjustment_amount_minor (может быть < 0)",
    )
    current_rate: CurrentRateResponse | None = Field(
        default=None,
        description="Действующая на сейчас ставка или null",
    )
    has_missing_rate: bool = Field(
        description="true, если в периоде были смены без действующей ставки",
    )
    planned_seconds: int = Field(
        default=0, description="Плановое время за период (work_schedules)"
    )
    planned_amount_minor: int = Field(default=0, description="Плановый заработок за период")
    delta_amount_minor: int = Field(
        default=0, description="gross_amount_minor − planned_amount_minor (< 0 — недозаработал)"
    )
    late_count: int = Field(default=0, description="Число опозданий за период")
    late_seconds_total: int = Field(default=0, description="Суммарное опоздание за период, сек")
