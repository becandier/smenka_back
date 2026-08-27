import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class FileCategory(enum.StrEnum):
    """Логическая группировка файлов. Определяет префикс ключа, политику
    (лимит размера, разрешённые типы) и права на загрузку/чтение."""

    checklist_photo = "checklist_photo"
    knowledge_base = "knowledge_base"
    avatar = "avatar"
    # Фото с камеры вместо координат при старте смены (shift_geo_photo_fallback).
    shift_geo_photo = "shift_geo_photo"
    other = "other"


class File(Base):
    """Чистый реестр блобов: метаданные одного объекта в storage.

    Не знает, к какой бизнес-сущности привязан — привязку делает фича-потребитель
    обычным FK на `files.id` (НЕ полиморфизм). См. docs/tasks/file_storage/backend.md."""

    __tablename__ = "files"
    __table_args__ = (
        # Под запрос очистки сирот (is_attached=false AND created_at < cutoff).
        Index("ix_files_attached_created", "is_attached", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Ключ объекта в бакете (с префиксом категории). Уникален.
    storage_key: Mapped[str] = mapped_column(Text, unique=True, index=True)
    # Имя бакета (на будущее под мульти-бакет; сейчас один из настроек).
    bucket: Mapped[str] = mapped_column(String(63))
    category: Mapped[FileCategory] = mapped_column(
        Enum(FileCategory, native_enum=False, length=32),
        index=True,
    )
    # Исходное имя клиента (для Content-Disposition при отдаче).
    original_filename: Mapped[str] = mapped_column(String(255))
    # MIME по реальному содержимому, не по заголовку клиента.
    content_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    # sha256 содержимого (целостность; задел под дедуп).
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Привязан ли файл к бизнес-сущности (см. жизненный цикл и сироты).
    is_attached: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # NULL = персональный/платформенный файл.
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Кто загрузил.
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
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
