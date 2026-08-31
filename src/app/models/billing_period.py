"""Справочник периодов продажи подписки и скидки за предоплату (`online_payments`).

Отдельная таблица, а не константы в коде: набор периодов (1/3/6 месяцев) и
размер скидки — маркетинговый параметр, меняется чаще, чем цены планов.
Сидируется миграцией: `(1, 0)`, `(3, 5)`, `(6, 10)`. Годового периода в v1
нет сознательно — лимит магазина ЮKassa 100 000 ₽/мес на оборот (см.
`docs/tasks/online_payments/backend.md`).
"""

from sqlalchemy import Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class BillingPeriod(Base):
    __tablename__ = "billing_periods"

    months: Mapped[int] = mapped_column(Integer, primary_key=True)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
