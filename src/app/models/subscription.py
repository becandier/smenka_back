"""Подписка организации на тариф + append-only журнал изменений (`tariffs`).

`subscriptions.status` хранит ТОЛЬКО ручные состояния (`trialing`/`active`/
`canceled`) — просрочка (`past_due`/`suspended`) вычисляется чистой функцией
от дат в `services/entitlements.py::compute_effective_status`, а не хранится
здесь: так хранимое и вычисляемое состояние не могут разъехаться между
прогонами Celery. Колонка — обычный VARCHAR без CHECK/native enum, по тому же
мотиву, что и `notifications.type` (см. `models/notification.py`).

`SubscriptionEvent` — неизменяемый журнал (создаётся, никогда не редактируется
и не удаляется): кто, когда и на каком основании продлил/изменил подписку.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class SubscriptionStatus(enum.StrEnum):
    """Ручные (хранимые) состояния подписки. НЕ путать с эффективным статусом
    (`services/entitlements.py::EffectiveStatus`), который добавляет `past_due`
    и `suspended` как производные от дат."""

    trialing = "trialing"
    active = "active"
    canceled = "canceled"


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_subscription_org"),
        Index("ix_subscriptions_current_period_end", "current_period_end"),
        Index("ix_subscriptions_trial_ends_at", "trial_ends_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # UNIQUE — одна подписка на организацию.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
    )
    plan_code: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("plans.code", ondelete="RESTRICT"),
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default=SubscriptionStatus.trialing.value,
        server_default=SubscriptionStatus.trialing.value,
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Антидубль уведомлений `subscription_expiring`: за сколько дней уже
    # предупреждали. Значение `0` — зарезервированный сентинел «уже отправлено
    # разовое subscription_suspended, дальнейшие expiring-предупреждения не
    # нужны, пока эффективный статус не восстановится» (см. `tasks/subscriptions.py`
    # — в ТЗ отдельной колонки под идемпотентность suspended-уведомления нет,
    # переиспользуем это поле, т.к. 0 никогда не выдаётся как реальный порог
    # 7/3/1).
    last_expiry_notice_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class SubscriptionEventType(enum.StrEnum):
    created = "created"
    extended = "extended"
    plan_changed = "plan_changed"
    status_changed = "status_changed"
    auto_suspended = "auto_suspended"
    # online_payments: применение успешного онлайн-платежа к подписке
    # (extend/upgrade через ЮKassa — вебхук или поллинг статуса).
    paid_online = "paid_online"
    # online_payments: возврат по онлайн-платежу (`refund.succeeded`
    # вебхук ЮKassa). Не `status_changed` — сама подписка не трогается,
    # только платёж помечается `status=refunded`; см. backend.md
    # «Возвраты» — решение отключать ли организацию принимает super_admin
    # руками через обычный PATCH .../subscription.
    payment_refunded = "payment_refunded"


class SubscriptionEvent(Base):
    """Append-only журнал: записи никогда не редактируются и не удаляются."""

    __tablename__ = "subscription_events"
    __table_args__ = (
        Index("ix_subscription_events_org_created", "organization_id", "created_at"),
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
    type: Mapped[str] = mapped_column(String(24))
    from_plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_plan_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    from_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    period_end_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_end_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # NULL = системное событие (авто-приостановка).
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # online_payments: какой онлайн-платёж породил это событие. NULL у
    # ручных продлений/правок супер-админа.
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # use_alter=True — payments.subscription_event_id ссылается на
        # subscription_events.id, а эта колонка ссылается обратно на
        # payments.id: без use_alter это циклическая зависимость, которую
        # `Base.metadata.create_all`/`drop_all` (тесты) не может
        # топологически отсортировать (тот же паттерн, что
        # `users.created_by_org_id` ↔ `organizations.owner_id`, см.
        # `models/user.py`). Alembic саму миграцию не затрагивает — там FK
        # создаётся отдельным `op.create_foreign_key` уже после обеих таблиц.
        ForeignKey(
            "payments.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_subscription_events_payment_id",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
