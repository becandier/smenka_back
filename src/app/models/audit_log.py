import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class AuditAction(enum.StrEnum):
    """Стабильные машинные коды аудируемых действий (см. backend.md)."""

    org_update = "org.update"
    org_delete = "org.delete"
    org_invite_rotate = "org.invite_rotate"
    member_join = "member.join"
    member_remove = "member.remove"
    member_role_update = "member.role_update"
    member_display_name_update = "member.display_name_update"
    member_create = "member.create"
    member_password_reset = "member.password_reset"  # noqa: S105 — код действия, не пароль
    # Не из явного списка ТЗ admin_created_accounts (там перечислены только
    # member.create/member.password_reset) — добавлено по аналогии с
    # member.role_update/member.display_name_update, чтобы смена логина через
    # PATCH .../members/{id} тоже попадала в аудит организации, а не терялась
    # под чужим по смыслу action'ом.
    member_login_update = "member.login_update"
    settings_update = "settings.update"
    location_create = "location.create"
    location_update = "location.update"
    location_delete = "location.delete"
    shift_finish = "shift.finish"
    shift_auto_finish = "shift.auto_finish"
    shift_schedule_change = "shift.schedule_change"
    pause_auto_finish = "pause.auto_finish"
    overtime_request = "overtime.request"
    overtime_review = "overtime.review"


class AuditResource(enum.StrEnum):
    """Тип объекта, которого касается действие."""

    organization = "organization"
    member = "member"
    settings = "settings"
    location = "location"
    shift = "shift"
    pause = "pause"
    overtime = "overtime"


class AuditLog(Base):
    """Append-only журнал чувствительных действий owner/admin/super_admin и
    системных авто-действий Celery (`actor_user_id = null`). Не редактируется и
    не удаляется через API."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # null — платформенное/персональное действие; CASCADE — журнал удаляется
    # вместе с организацией.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    # null — системное действие (Celery); SET NULL — при удалении пользователя
    # запись аудита сохраняется.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("ix_audit_logs_org_created", "organization_id", text("created_at DESC")),
        Index("ix_audit_logs_actor_user_id", "actor_user_id"),
        Index("ix_audit_logs_action", "action"),
    )
