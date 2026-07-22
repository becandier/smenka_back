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


class ShiftFinishReason(enum.StrEnum):
    """Причина завершения смены. NULL у активных/паузных и у всех исторических
    смен, заведённых до фичи work_schedules."""

    manual = "manual"
    auto_schedule = "auto_schedule"


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

    user: Mapped["User"] = relationship(back_populates="shifts")
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
