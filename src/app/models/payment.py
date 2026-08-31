"""Онлайн-платежи за подписку через ЮKassa (`online_payments`).

Финансовый след — записи не удаляются и не редактируются задним числом
(кроме полей, которые фиксируют состояние платежа: `status`/`paid_at`/
`applied_at`/`provider_payload`/`cancellation_reason`). `applied_at` —
защита от повторного применения одного и того же платежа к подписке
(идемпотентность вебхука и поллинга, см. `services/billing.py::apply_payment`).
"""

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class PaymentKind(enum.StrEnum):
    extend = "extend"
    upgrade = "upgrade"


class PaymentStatus(enum.StrEnum):
    """Обычный VARCHAR без CHECK/native enum (как `subscriptions.status`) —
    новое значение (`refunded`, возвраты) не требует миграции схемы."""

    pending = "pending"
    succeeded = "succeeded"
    canceled = "canceled"
    refunded = "refunded"


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_org_created", "organization_id", "created_at"),
        Index("ix_payments_status", "status"),
        Index(
            "uq_payments_provider_payment_id",
            "provider_payment_id",
            unique=True,
            postgresql_where=text("provider_payment_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
    )
    # extend (продление) / upgrade (доплата за смену тарифа внутри периода).
    kind: Mapped[str] = mapped_column(String(16))
    # Тариф, который оплачивается (для upgrade — целевой, всегда premium).
    plan_code: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("plans.code", ondelete="RESTRICT"),
    )
    # extend: число оплаченных месяцев. upgrade: months_remaining, за сколько
    # месяцев доплачена разница.
    months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    base_amount_minor: Mapped[int] = mapped_column(Integer)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="RUB", server_default="RUB")
    status: Mapped[str] = mapped_column(
        String(16),
        default=PaymentStatus.pending.value,
        server_default=PaymentStatus.pending.value,
    )
    provider: Mapped[str] = mapped_column(
        String(16), default="yookassa", server_default="yookassa"
    )
    provider_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotence_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    confirmation_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # NULL = ещё не применён к подписке (в т.ч. навсегда — если организация
    # была удалена к моменту оплаты, см. backend.md «Применение платежа»).
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subscription_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscription_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
