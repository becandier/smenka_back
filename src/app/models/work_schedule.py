"""Графики работы (work_schedules): интервалы времени внутри суток.

Назначение сотрудникам — полная калька модели чек-листов (`checklist.py`):
роль / рабочая точка / персональное исключение. Единственное сознательное
отличие (backend.md, R1): график без единой привязки (ни роли, ни точки)
действует на ВСЕХ сотрудников организации — у чек-листов такой шаблон не
выдаётся никому.
"""

import enum
import uuid
from datetime import UTC, datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Time,
    UniqueConstraint,
)
from sqlalchemy import (
    String as SaString,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base

if TYPE_CHECKING:
    from src.app.models.organization import Organization


class ScheduleOverrideType(enum.StrEnum):
    """Тип личного исключения графика (независимый от `checklist.OverrideType`,
    хотя значения совпадают — домены разные, PG enum-тип свой)."""

    add = "add"
    remove = "remove"


class WorkSchedule(Base):
    __tablename__ = "work_schedules"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(SaString(100))
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    is_paused: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
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

    organization: Mapped["Organization"] = relationship()

    @property
    def crosses_midnight(self) -> bool:
        """Ночной график, переходящий через полночь (`end_time < start_time`)."""
        return self.end_time < self.start_time

    @property
    def duration_minutes(self) -> int:
        start_minutes = self.start_time.hour * 60 + self.start_time.minute
        end_minutes = self.end_time.hour * 60 + self.end_time.minute
        if end_minutes > start_minutes:
            return end_minutes - start_minutes
        return 24 * 60 - (start_minutes - end_minutes)


class WorkScheduleRole(Base):
    """Назначение графика кастомной роли организации."""

    __tablename__ = "work_schedule_roles"
    __table_args__ = (UniqueConstraint("schedule_id", "role_id", name="uq_work_schedule_role"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_schedules.id", ondelete="CASCADE"),
        index=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_roles.id", ondelete="CASCADE"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class WorkScheduleLocation(Base):
    """Привязка графика к рабочей точке (many-to-many)."""

    __tablename__ = "work_schedule_locations"
    __table_args__ = (
        UniqueConstraint("schedule_id", "work_location_id", name="uq_work_schedule_location"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_schedules.id", ondelete="CASCADE"),
        index=True,
    )
    work_location_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_locations.id", ondelete="CASCADE"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class WorkScheduleMemberOverride(Base):
    """Персональное исключение (add/remove) графика для сотрудника."""

    __tablename__ = "work_schedule_member_overrides"
    __table_args__ = (
        UniqueConstraint("schedule_id", "member_id", name="uq_work_schedule_member_override"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("work_schedules.id", ondelete="CASCADE"),
        index=True,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_members.id", ondelete="CASCADE"),
        index=True,
    )
    override_type: Mapped[ScheduleOverrideType] = mapped_column(
        Enum(ScheduleOverrideType),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
