"""Заявки на переработку (backend.md, R6): сотрудник просит зачесть время сверх
планового окна смены, администратор согласует/отклоняет.
"""

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
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base

if TYPE_CHECKING:
    from src.app.models.shift import Shift


class OvertimeRequestStatus(enum.StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ShiftOvertimeRequest(Base):
    """Одна заявка на переработку по смене.

    Инвариант «максимум одна pending/approved заявка на смену» защищён частичным
    уникальным индексом `(shift_id) WHERE status IN ('pending','approved')` — тот
    же приём, что и `KnowledgeNodeAccess` в `models/knowledge.py`. После
    `rejected` сотрудник может подать заново (новая строка).
    """

    __tablename__ = "shift_overtime_requests"
    __table_args__ = (
        Index(
            "uq_shift_overtime_requests_active",
            "shift_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'approved')"),
        ),
    )

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
    minutes: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(String(500))
    status: Mapped[OvertimeRequestStatus] = mapped_column(
        Enum(OvertimeRequestStatus),
        default=OvertimeRequestStatus.pending,
        server_default="pending",
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    review_comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    shift: Mapped["Shift"] = relationship()
