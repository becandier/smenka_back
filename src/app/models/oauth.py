import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base

if TYPE_CHECKING:
    from src.app.models.user import User


class OAuthProvider(enum.StrEnum):
    google = "google"
    apple = "apple"


class OAuthClientType(enum.StrEnum):
    ios = "ios"
    android = "android"
    web = "web"


class OAuthIdentity(Base):
    """Привязка пользователя к внешнему OAuth-провайдеру (Google/Apple).

    Отдельная таблица (а не колонки в users), т.к. у одного пользователя
    может быть до двух привязок (Google + Apple) одновременно.
    """

    __tablename__ = "oauth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_oauth_identity_provider_sub"),
        UniqueConstraint("user_id", "provider", name="uq_oauth_identity_user_provider"),
        Index("ix_oauth_identities_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    provider: Mapped[str] = mapped_column(String(16))
    # sub-claim из id-токена провайдера.
    provider_user_id: Mapped[str] = mapped_column(String(255))
    # email из токена на момент привязки (справочно/аудит, для матчинга повторно не используется).
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    user: Mapped["User"] = relationship(back_populates="oauth_identities")


class OAuthProviderSetting(Base):
    """Платформенная настройка: какие Client ID бэк принимает как валидный
    `aud` для комбинации provider/client_type. Редактируется супер-админом
    из админки (не ENV) — см. docs/tasks/oauth_login/backend.md.
    """

    __tablename__ = "oauth_provider_settings"
    __table_args__ = (
        UniqueConstraint("provider", "client_type", name="uq_oauth_provider_setting"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    provider: Mapped[str] = mapped_column(String(16))
    client_type: Mapped[str] = mapped_column(String(16))
    # Client ID (Google) / Bundle ID или Services ID (Apple) — публичный
    # идентификатор, хранится как есть (не секрет).
    client_id: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
