import uuid

from sqlalchemy import Boolean, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base


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
    auto_finish_hours: Mapped[int] = mapped_column(Integer, default=16)
    max_pause_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_pauses_per_shift: Mapped[int | None] = mapped_column(Integer, nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="settings")
