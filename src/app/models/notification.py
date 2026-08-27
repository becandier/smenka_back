import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class NotificationType(enum.StrEnum):
    """Известные типы уведомлений.

    Колонка `notifications.type` — обычный VARCHAR без CHECK-ограничения (см.
    backend.md фичи notifications): новый тип, добавленный будущим
    производителем событий, не требует миграции схемы. Этот enum — только
    типобезопасность вызовов `create_notification` внутри бэка, не источник
    правды для БД.
    """

    test_assigned = "test_assigned"
    shift_manual_changed = "shift_manual_changed"
    payroll_adjustment_changed = "payroll_adjustment_changed"
    subscription_expiring = "subscription_expiring"
    subscription_suspended = "subscription_suspended"


class Notification(Base):
    """Внутриапповое уведомление пользователя (pull-модель, без OS-push).

    Получатель — `User`, а не `OrganizationMember`: работает и для сотрудника
    в организации, и для персонального режима. `organization_id` — контекст
    события (null для платформенных/персональных уведомлений).
    """

    __tablename__ = "notifications"
    __table_args__ = (
        # Лента пользователя: created_at DESC — самые свежие сверху.
        Index("ix_notifications_user_created", "user_id", text("created_at DESC")),
        # Счётчик непрочитанных / фильтр unread=true.
        Index("ix_notifications_user_unread", "user_id", "is_read"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    type: Mapped[str] = mapped_column(String(48))
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
