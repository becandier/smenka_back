import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base

if TYPE_CHECKING:
    from src.app.models.organization import Organization


class OrganizationSettings(Base):
    __tablename__ = "organization_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    geo_check_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    require_work_location: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    max_pause_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_pauses_per_shift: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # --- work_schedules ---
    auto_finish_by_schedule: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
    )
    require_schedule: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    late_tolerance_minutes: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )
    overtime_request_days: Mapped[int] = mapped_column(
        Integer,
        default=7,
        server_default="7",
    )
    # --- schedule_window_enforcement ---
    early_start_minutes: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default="0",
    )

    organization: Mapped["Organization"] = relationship(back_populates="settings")
