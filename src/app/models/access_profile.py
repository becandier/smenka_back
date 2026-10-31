import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class OrganizationAccessProfile(Base):
    """Профиль доступа организации (access_profiles, ADR-006).

    Именованный набор прав по разделам веб-кабинета: `permissions` —
    `{"<section>": "view"|"manage"}`, разделы и уровни — из
    `core/access_catalog.py` (валидация на уровне Pydantic, CHECK в БД нет).
    Выдаётся участнику с системной ролью `employee`
    (`OrganizationMember.access_profile_id`). Удаление — soft-delete (ADR-003),
    при удалении участники отвязываются явно в той же транзакции. Имя уникально
    среди живых профилей организации без учёта регистра.
    """

    __tablename__ = "organization_access_profiles"
    __table_args__ = (
        Index(
            "ix_organization_access_profiles_org_is_deleted",
            "organization_id",
            "is_deleted",
        ),
        Index(
            "uq_organization_access_profiles_org_name",
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
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONB)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
