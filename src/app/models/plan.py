"""Справочник тарифов (`tariffs`): Стандарт / Премиум.

Редактирование из админки в v1 не делаем — цена/лимиты/фичи меняются только
миграцией (см. `backend.md` фичи `tariffs`, §«Редактирование планов»). Флаги
`feature_*` — источник правды по составу тарифа, единственный на них
потребитель — `PlanFeature` в `services/entitlements.py` (соответствие 1:1
проверяется тестом).
"""

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class Plan(Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", server_default="RUB")
    # NULL = без лимита.
    max_employees: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_locations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feature_fines: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    feature_test_import: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
