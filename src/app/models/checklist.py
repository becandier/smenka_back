import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base

if TYPE_CHECKING:
    from src.app.models.file import File
    from src.app.models.shift import Shift


class ChecklistType(enum.StrEnum):
    shift_start = "shift_start"
    shift_end = "shift_end"


class OverrideType(enum.StrEnum):
    add = "add"
    remove = "remove"


class ChecklistInstanceStatus(enum.StrEnum):
    pending = "pending"
    completed = "completed"
    incomplete = "incomplete"


class PhotoRequirement(enum.StrEnum):
    """Требование к фото-подтверждению на пункте чек-листа."""

    none = "none"  # фото нельзя прикреплять (UI скрыт) — дефолт
    optional = "optional"  # фото можно прикреплять
    required = "required"  # нужно >=1 фото для «satisfied»


class PhotoSource(enum.StrEnum):
    """Подсказка мобильному UI об источнике фото (сервер не enforce-ит)."""

    camera = "camera"  # только съёмка в приложении (антифрод) — дефолт
    camera_or_gallery = "camera_or_gallery"  # можно выбрать из галереи или снять


class ChecklistTemplate(Base):
    __tablename__ = "checklist_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[ChecklistType] = mapped_column(Enum(ChecklistType))
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    max_per_shift: Mapped[int] = mapped_column(Integer, default=1)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    items: Mapped[list["ChecklistTemplateItem"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="ChecklistTemplateItem.position",
    )


class ChecklistTemplateItem(Base):
    __tablename__ = "checklist_template_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checklist_templates.id", ondelete="CASCADE"),
        index=True,
    )
    text: Mapped[str] = mapped_column(String(500))
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    photo_requirement: Mapped[PhotoRequirement] = mapped_column(
        Enum(PhotoRequirement, native_enum=False, length=32),
        default=PhotoRequirement.none,
        server_default=sa_text("'none'"),
    )
    photo_source: Mapped[PhotoSource] = mapped_column(
        Enum(PhotoSource, native_enum=False, length=32),
        default=PhotoSource.camera,
        server_default=sa_text("'camera'"),
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

    template: Mapped["ChecklistTemplate"] = relationship(back_populates="items")


class ChecklistRoleAssignment(Base):
    __tablename__ = "checklist_role_assignments"
    __table_args__ = (
        UniqueConstraint("template_id", "role_id", name="uq_checklist_role_assignment"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checklist_templates.id", ondelete="CASCADE"),
        index=True,
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_roles.id", ondelete="CASCADE"),
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class ChecklistMemberOverride(Base):
    __tablename__ = "checklist_member_overrides"
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "member_id",
            name="uq_checklist_member_override",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checklist_templates.id", ondelete="CASCADE"),
        index=True,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_members.id", ondelete="CASCADE"),
        index=True,
    )
    override_type: Mapped[OverrideType] = mapped_column(Enum(OverrideType))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class ChecklistInstance(Base):
    __tablename__ = "checklist_instances"

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
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checklist_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[ChecklistType] = mapped_column(Enum(ChecklistType))
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[ChecklistInstanceStatus] = mapped_column(
        Enum(ChecklistInstanceStatus),
        default=ChecklistInstanceStatus.pending,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    shift: Mapped["Shift"] = relationship(back_populates="checklist_instances")
    items: Mapped[list["ChecklistInstanceItem"]] = relationship(
        back_populates="instance",
        cascade="all, delete-orphan",
        order_by="ChecklistInstanceItem.position",
    )


class ChecklistInstanceItem(Base):
    __tablename__ = "checklist_instance_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checklist_instances.id", ondelete="CASCADE"),
        index=True,
    )
    template_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checklist_template_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    text: Mapped[str] = mapped_column(String(500))
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    change_count: Mapped[int] = mapped_column(Integer, default=0)
    # Снимок настроек фото с шаблонного пункта на старте смены (см. backend.md).
    photo_requirement: Mapped[PhotoRequirement] = mapped_column(
        Enum(PhotoRequirement, native_enum=False, length=32),
        default=PhotoRequirement.none,
        server_default=sa_text("'none'"),
    )
    photo_source: Mapped[PhotoSource] = mapped_column(
        Enum(PhotoSource, native_enum=False, length=32),
        default=PhotoSource.camera,
        server_default=sa_text("'camera'"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    instance: Mapped["ChecklistInstance"] = relationship(back_populates="items")
    photos: Mapped[list["ChecklistItemPhoto"]] = relationship(
        back_populates="item",
        cascade="all, delete-orphan",
        order_by="ChecklistItemPhoto.position",
    )


class ChecklistItemPhoto(Base):
    """Фото-подтверждение, привязанное к пункту-экземпляру чек-листа.

    Геометки и `captured_at` — отдельные колонки (хранилище стрипает EXIF);
    видимый штамп вжигает клиент в пиксели. Один файл = одна привязка
    (UNIQUE на file_id). Удаление файла или пункта снимает привязку каскадом.
    """

    __tablename__ = "checklist_item_photos"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    instance_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("checklist_instance_items.id", ondelete="CASCADE"),
        index=True,
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        unique=True,
    )
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    item: Mapped["ChecklistInstanceItem"] = relationship(back_populates="photos")
    file: Mapped["File"] = relationship()
