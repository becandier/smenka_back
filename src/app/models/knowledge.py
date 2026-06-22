"""База знаний организации: дерево разделов/страниц, ACL на узлах, реестр файлов.

Только организационный режим — каждый узел принадлежит конкретной org. Контент
страницы (`kind=page`) — массив блоков в JSONB (см. `schemas.knowledge` → BLOCK
SCHEMA). Доступ employee к узлу считается обходом дерева по `parent_id` с приоритетом
категорий правил (персональное > ролевое > all_members > deny). Файлы привязаны к
странице через `knowledge_node_files` (паттерн «FK от потребителя», как
`checklist_item_photos`): пока файл числится в реестре — `files.is_attached=true`.
"""

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.app.core.database import Base


class KnowledgeNodeKind(enum.StrEnum):
    section = "section"  # контейнер-папка; content всегда NULL
    page = "page"  # страница с блочным контентом; content — массив блоков


class KnowledgeSubjectType(enum.StrEnum):
    role = "role"  # правило на кастомную роль org
    member = "member"  # правило на конкретного сотрудника (user_id в рамках org)


class KnowledgeAccessEffect(enum.StrEnum):
    allow = "allow"
    deny = "deny"


class KnowledgeNode(Base):
    """Узел дерева базы знаний: раздел (`section`) или страница (`page`).

    Бесконечная вложенность — любой узел может иметь детей. `content` хранит блоки
    только для страницы; для раздела всегда `NULL`. `all_members` — быстрый тумблер
    «видно всем сотрудникам организации» (самый слабый положительный сигнал ACL).
    """

    __tablename__ = "knowledge_nodes"
    __table_args__ = (
        Index("ix_knowledge_nodes_organization_id", "organization_id"),
        Index("ix_knowledge_nodes_parent_id", "parent_id"),
        Index(
            "ix_knowledge_nodes_org_parent_position",
            "organization_id",
            "parent_id",
            "position",
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
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        nullable=True,
    )
    kind: Mapped[KnowledgeNodeKind] = mapped_column(
        Enum(KnowledgeNodeKind, native_enum=False, length=16),
    )
    title: Mapped[str] = mapped_column(String(255))
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    all_members: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
    )
    content: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)
    schema_version: Mapped[int] = mapped_column(
        SmallInteger,
        default=1,
        server_default="1",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
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


class KnowledgeNodeAccess(Base):
    """ACL-правило на узле: разрешить/запретить доступ роли либо сотруднику.

    Целостность субъекта гарантирует CHECK-констрейнт; одно правило на субъект на
    узел — частичные UNIQUE. Каскад по `node_id`/`role_id`/`member_user_id` снимает
    правило автоматически при удалении узла/роли/пользователя.
    """

    __tablename__ = "knowledge_node_access"
    __table_args__ = (
        CheckConstraint(
            "(subject_type = 'role' AND role_id IS NOT NULL AND member_user_id IS NULL) "
            "OR (subject_type = 'member' AND member_user_id IS NOT NULL AND role_id IS NULL)",
            name="ck_knowledge_access_subject",
        ),
        Index("ix_knowledge_node_access_node_id", "node_id"),
        Index(
            "uq_knowledge_access_role",
            "node_id",
            "role_id",
            unique=True,
            postgresql_where=text("subject_type = 'role'"),
        ),
        Index(
            "uq_knowledge_access_member",
            "node_id",
            "member_user_id",
            unique=True,
            postgresql_where=text("subject_type = 'member'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
    )
    subject_type: Mapped[KnowledgeSubjectType] = mapped_column(
        Enum(KnowledgeSubjectType, native_enum=False, length=16),
    )
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_roles.id", ondelete="CASCADE"),
        nullable=True,
    )
    member_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    effect: Mapped[KnowledgeAccessEffect] = mapped_column(
        Enum(KnowledgeAccessEffect, native_enum=False, length=8),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class KnowledgeNodeFile(Base):
    """Реестр привязок файла к странице (`image`/`file`-блоки её `content`).

    Пока строка существует — файл `is_attached=true` и не подбирается
    `cleanup_orphan_files`. `UNIQUE(file_id)` гарантирует «один файл = одна
    страница» (важно для безусловного удаления файлов при удалении страницы).
    """

    __tablename__ = "knowledge_node_files"
    __table_args__ = (
        Index("ix_knowledge_node_files_node_id", "node_id"),
        UniqueConstraint("file_id", name="uq_knowledge_node_files_file_id"),
    )

    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_nodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("files.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
