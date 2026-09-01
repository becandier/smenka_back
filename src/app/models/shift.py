import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base

if TYPE_CHECKING:
    from src.app.models.checklist import ChecklistInstance
    from src.app.models.organization import Organization
    from src.app.models.user import User
    from src.app.models.work_location import WorkLocation


class ShiftStatus(enum.StrEnum):
    active = "active"
    paused = "paused"
    finished = "finished"


class GeoFallbackReason(enum.StrEnum):
    """Машинный код гео-ошибки клиента при старте смены по фото вместо координат
    (shift_geo_photo_fallback).

    Значения — контракт с мобилкой (UPPER_SNAKE, как коды ошибок API), поэтому
    имена членов совпадают со значениями: в БД (VARCHAR(40)) хранится ровно тот
    код, который прислал клиент. Набор фиксирован — произвольную строку сюда
    класть нельзя.
    """

    GEO_PERMISSION_DENIED = "GEO_PERMISSION_DENIED"
    GEO_PERMISSION_DENIED_FOREVER = "GEO_PERMISSION_DENIED_FOREVER"
    GEO_SERVICE_DISABLED = "GEO_SERVICE_DISABLED"
    GEO_UNAVAILABLE = "GEO_UNAVAILABLE"
    GEO_UNSUPPORTED = "GEO_UNSUPPORTED"
    GEO_INSECURE_CONTEXT = "GEO_INSECURE_CONTEXT"


class ShiftFinishReason(enum.StrEnum):
    """Причина завершения смены. NULL у активных/паузных и у всех исторических
    смен, заведённых до фичи work_schedules."""

    manual = "manual"
    auto_schedule = "auto_schedule"


class ShiftHistoryScope(enum.StrEnum):
    """Срез истории смен для `GET /shifts` и `GET /shifts/stats` (shift_history_scope).

    Не персистится в БД — только query-параметр запроса. `all` — прежнее
    поведение без изменений (обязательное требование обратной совместимости:
    отсутствие параметра = `all`).
    """

    all = "all"
    personal = "personal"
    organization = "organization"


class Shift(Base):
    __tablename__ = "shifts"
    __table_args__ = (
        # Под выборку Celery-задачи авто-завершения (backend.md, R4): частичный —
        # завершённые смены в предикат не попадают и никогда не сканируются.
        Index(
            "ix_shifts_scheduled_end_at",
            "scheduled_end_at",
            postgresql_where=text("status IN ('active', 'paused')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    work_location_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_locations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    status: Mapped[ShiftStatus] = mapped_column(
        Enum(ShiftStatus),
        default=ShiftStatus.active,
    )
    has_incomplete_required_checklists: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    # --- work_schedules: график и снимок планового окна ---
    work_schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_schedules.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    schedule_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    scheduled_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    scheduled_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finish_reason: Mapped[ShiftFinishReason | None] = mapped_column(
        Enum(ShiftFinishReason),
        nullable=True,
    )
    # --- shift_geo_photo_fallback: старт без геопроверки, по фото с камеры ---
    # Файл категории `shift_geo_photo`. ON DELETE SET NULL: если фото когда-нибудь
    # удалят, признак «стартовала без гео» не теряется — он живёт в reason.
    geo_fallback_photo_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Инвариант: `geo_fallback_reason IS NOT NULL` ⇔ смена стартовала без геопроверки.
    geo_fallback_reason: Mapped[GeoFallbackReason | None] = mapped_column(
        Enum(GeoFallbackReason, native_enum=False, length=40),
        nullable=True,
    )
    # --- manual_time_entry: ручной ввод/правка/удаление смены администратором ---
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    edited_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    edited_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    manual_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # foreign_keys обязателен с manual_time_entry: между shifts и users теперь
    # четыре пути FK (user_id + created_by/edited_by/deleted_by_user_id).
    user: Mapped["User"] = relationship(back_populates="shifts", foreign_keys=[user_id])
    organization: Mapped["Organization | None"] = relationship()
    work_location: Mapped["WorkLocation | None"] = relationship()
    pauses: Mapped[list["Pause"]] = relationship(
        back_populates="shift",
        cascade="all, delete-orphan",
        order_by="Pause.started_at",
    )
    checklist_instances: Mapped[list["ChecklistInstance"]] = relationship(
        back_populates="shift",
        cascade="all, delete-orphan",
    )


class Pause(Base):
    __tablename__ = "pauses"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    shift_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shifts.id", ondelete="CASCADE"),
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    shift: Mapped["Shift"] = relationship(back_populates="pauses")
