import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base

if TYPE_CHECKING:
    from src.app.models.organization import OrganizationMember


class RateType(enum.StrEnum):
    hourly = "hourly"
    per_shift = "per_shift"


class OrganizationMemberRate(Base):
    """Строка истории ставок участника.

    Одна строка = одна ставка, действующая начиная с `effective_from`.
    Прошлые ставки не перезаписываются — при изменении добавляется новая строка.
    Действующая ставка на момент T — строка с максимальным `effective_from <= T`.
    """

    __tablename__ = "organization_member_rates"
    __table_args__ = (
        UniqueConstraint(
            "member_id", "effective_from", name="uq_member_rate_effective_from",
        ),
        Index(
            "ix_member_rates_member_id_effective_from_desc",
            "member_id",
            text("effective_from DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_members.id", ondelete="CASCADE"),
        index=True,
    )
    rate_amount_minor: Mapped[int] = mapped_column(Integer)
    rate_type: Mapped[RateType] = mapped_column(Enum(RateType))
    currency: Mapped[str] = mapped_column(
        String(3),
        default="RUB",
        server_default="RUB",
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    member: Mapped["OrganizationMember"] = relationship(back_populates="rates")
