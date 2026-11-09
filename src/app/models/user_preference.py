import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class UserPreference(Base):
    """Личная настройка пользователя: непрозрачное JSON-значение по строковому ключу.

    Сервер смысл значения не знает (например, раскладка столбцов таблицы в
    админке — `admin.table.<resource>`). К организации не привязана: у
    пользователя с несколькими организациями настройка общая. Первичный ключ
    `(user_id, key)` — отдельного `id` и дополнительных индексов нет, все
    чтения идут по `user_id` (левый префикс PK).
    """

    __tablename__ = "user_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
