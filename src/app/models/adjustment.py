import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class PayrollAdjustment(Base):
    """Ручное начисление или удержание сотруднику (manual_time_entry).

    Симметрично `Penalty`, но знак хранится в самой сумме (`amount_minor`):
    `> 0` — доплата, `< 0` — удержание, `!= 0` — инвариант (проверяется и на
    уровне схемы, и здесь — defense in depth). Не привязано к шаблонам (в
    отличие от штрафов) и может существовать без привязки к смене. Отмена —
    soft-delete (`is_deleted = true`).
    """

    __tablename__ = "payroll_adjustments"
    __table_args__ = (
        Index("ix_payroll_adjustments_org_is_deleted", "organization_id", "is_deleted"),
        Index("ix_payroll_adjustments_member_is_deleted", "member_id", "is_deleted"),
        Index("ix_payroll_adjustments_occurred_at", "occurred_at"),
        CheckConstraint("amount_minor != 0", name="ck_payroll_adjustments_amount_nonzero"),
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
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_members.id", ondelete="CASCADE"),
    )
    shift_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("shifts.id", ondelete="SET NULL"),
        nullable=True,
    )
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(
        String(3),
        default="RUB",
        server_default="RUB",
    )
    reason: Mapped[str] = mapped_column(String(200))
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
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
