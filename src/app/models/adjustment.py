import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class PayrollAdjustmentCategory(Base):
    """Категория ручных начислений организации (payroll_breakdown).

    Справочник («Премия», «Компенсация», «Удержание за форму»…) для раскладки
    начислений в зарплатном отчёте. Категория **не задаёт знак** — знак
    по-прежнему в `PayrollAdjustment.amount_minor`. Начисление ссылается на
    категорию (не снимок): переименование отражается во всех отчётах. Удаление —
    soft-delete (ADR-003): удалённая категория остаётся у ранее созданных
    начислений и показывается по имени, но новым начислениям не назначается.
    Имя уникально среди живых категорий организации без учёта регистра —
    частичный уникальный индекс, поэтому после удаления имя можно занять снова.
    """

    __tablename__ = "payroll_adjustment_categories"
    __table_args__ = (
        Index(
            "ix_payroll_adjustment_categories_org_is_deleted",
            "organization_id",
            "is_deleted",
        ),
        Index(
            "uq_payroll_adjustment_categories_org_name",
            "organization_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
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
    name: Mapped[str] = mapped_column(String(100))
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    deleted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
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


class PayrollAdjustment(Base):
    """Ручное начисление или удержание сотруднику (manual_time_entry).

    Симметрично `Penalty`, но знак хранится в самой сумме (`amount_minor`):
    `> 0` — доплата, `< 0` — удержание, `!= 0` — инвариант (проверяется и на
    уровне схемы, и здесь — defense in depth). Не привязано к шаблонам (в
    отличие от штрафов) и может существовать без привязки к смене. Отмена —
    soft-delete (`is_deleted = true`). `category_id` — необязательная категория
    (`PayrollAdjustmentCategory`, payroll_breakdown); `NULL` — «Без категории».
    """

    __tablename__ = "payroll_adjustments"
    __table_args__ = (
        Index("ix_payroll_adjustments_org_is_deleted", "organization_id", "is_deleted"),
        Index("ix_payroll_adjustments_member_is_deleted", "member_id", "is_deleted"),
        Index("ix_payroll_adjustments_occurred_at", "occurred_at"),
        Index("ix_payroll_adjustments_category_id", "category_id"),
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
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payroll_adjustment_categories.id", ondelete="SET NULL"),
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
