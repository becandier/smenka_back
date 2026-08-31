"""Чистые расчётные функции биллинга (`online_payments`): скидка за период
продления, доплата за апгрейд Стандарт → Премиум. Без побочных эффектов и
обращений к БД/сети — намеренно вынесены отдельно, чтобы формулы из
`backend.md` («Бизнес-правила») были протестированы напрямую, а не только
через HTTP-эндпоинты.
"""

import math
from dataclasses import dataclass
from datetime import datetime

# «1 месяц» для расчёта months_remaining при апгрейде — backend.md формулирует
# правило как «незавершённый месяц считается целым», но не даёт точную единицу
# для ceil(). 30 дней — простая и предсказуемая единица, симметричная тому, что
# цена плана фиксирована помесячно вне зависимости от календарной длины месяца.
UPGRADE_MONTH_DAYS = 30


@dataclass(frozen=True, slots=True)
class ExtendAmount:
    base_amount_minor: int
    discount_percent: int
    discount_minor: int
    amount_minor: int
    monthly_minor: int


def compute_extend_amount(price_minor: int, months: int, discount_percent: int) -> ExtendAmount:
    """`base = price × months; amount = base − round_down_to_ruble(base × discount% / 100)`.

    Скидка округляется вниз до целого рубля: сначала считается в копейках
    (floor от `base × discount% / 100`), затем сама эта сумма ещё раз
    округляется вниз до ближайшего рубля (100 копеек) — так клиент и сервер
    гарантированно считают одинаково даже при не круглых ценах.
    """
    base_amount_minor = price_minor * months
    raw_discount_minor = base_amount_minor * discount_percent // 100
    discount_minor = (raw_discount_minor // 100) * 100
    amount_minor = base_amount_minor - discount_minor
    monthly_minor = amount_minor // months
    return ExtendAmount(
        base_amount_minor=base_amount_minor,
        discount_percent=discount_percent,
        discount_minor=discount_minor,
        amount_minor=amount_minor,
        monthly_minor=monthly_minor,
    )


def compute_upgrade_months_remaining(current_period_end: datetime, now: datetime) -> int:
    """`ceil((current_period_end − now) / 1 месяц)`, минимум 1.

    Минимум 1 покрывает и `past_due` (период формально уже истёк, `now` уже
    позже `current_period_end`, разница отрицательная) — доплата всё равно
    считается минимум за один месяц.
    """
    delta_seconds = (current_period_end - now).total_seconds()
    if delta_seconds <= 0:
        return 1
    months = math.ceil(delta_seconds / (UPGRADE_MONTH_DAYS * 86400))
    return max(1, months)


def compute_upgrade_amount(
    standard_price_minor: int, premium_price_minor: int, months_remaining: int
) -> int:
    """`(price(premium) − price(standard)) × months_remaining`. Скидка периода
    к доплате не применяется — она уже учтена в исходном платеже."""
    return (premium_price_minor - standard_price_minor) * months_remaining
