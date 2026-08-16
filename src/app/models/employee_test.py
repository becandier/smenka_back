"""Тестирование сотрудников: шаблон → вопросы/варианты → назначение →
попытка-снимок с результатом (та же схема «шаблон → снимок», что у чек-листов,
см. `models/checklist.py`). Все сущности organization-scoped; персональный
режим (`organization_id=null`) тестов не касается.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
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


class TestQuestionType(enum.StrEnum):
    single_choice = "single_choice"
    multiple_choice = "multiple_choice"


class TestAssignmentStatus(enum.StrEnum):
    assigned = "assigned"
    in_progress = "in_progress"
    passed = "passed"
    failed = "failed"


class TestAttemptStatus(enum.StrEnum):
    in_progress = "in_progress"
    submitted = "submitted"


class TestTemplate(Base):
    __tablename__ = "test_templates"

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
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    pass_threshold_percent: Mapped[int] = mapped_column(
        Integer, default=70, server_default=sa_text("70")
    )
    max_attempts: Mapped[int] = mapped_column(Integer, default=1, server_default=sa_text("1"))
    reveal_answers: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=sa_text("true")
    )
    shuffle_questions: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false")
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
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

    questions: Mapped[list["TestQuestion"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="TestQuestion.position",
    )


class TestQuestion(Base):
    __tablename__ = "test_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("test_templates.id", ondelete="CASCADE"),
        index=True,
    )
    text: Mapped[str] = mapped_column(Text)
    type: Mapped[TestQuestionType] = mapped_column(
        Enum(TestQuestionType, native_enum=False, length=32),
    )
    points: Mapped[int] = mapped_column(Integer, default=1, server_default=sa_text("1"))
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    template: Mapped["TestTemplate"] = relationship(back_populates="questions")
    options: Mapped[list["TestQuestionOption"]] = relationship(
        back_populates="question",
        cascade="all, delete-orphan",
        order_by="TestQuestionOption.position",
    )


class TestQuestionOption(Base):
    __tablename__ = "test_question_options"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("test_questions.id", ondelete="CASCADE"),
        index=True,
    )
    text: Mapped[str] = mapped_column(String(500))
    is_correct: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false")
    )
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))

    question: Mapped["TestQuestion"] = relationship(back_populates="options")


class TestAssignment(Base):
    """Назначение шаблона конкретному сотруднику. Денормализованный
    статус/результат — чтобы реестр результатов в админке был одним запросом."""

    __tablename__ = "test_assignments"
    __table_args__ = (UniqueConstraint("template_id", "member_id", name="uq_test_assignment"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("test_templates.id", ondelete="CASCADE"),
        index=True,
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_members.id", ondelete="CASCADE"),
        index=True,
    )
    assigned_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[TestAssignmentStatus] = mapped_column(
        Enum(TestAssignmentStatus, native_enum=False, length=16),
        default=TestAssignmentStatus.assigned,
        server_default=sa_text("'assigned'"),
    )
    attempts_used: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    best_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"))
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    template: Mapped["TestTemplate"] = relationship()
    attempts: Mapped[list["TestAttempt"]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        order_by="TestAttempt.attempt_number",
    )


class TestAttempt(Base):
    """Попытка — снимок прохождения. Хранит собственный `pass_threshold_percent`
    (снимок с шаблона на момент старта — см. ADR-003): правка порога в шаблоне
    после сдачи не должна задним числом менять passed/failed уже сданной попытки."""

    __tablename__ = "test_attempts"
    __table_args__ = (
        UniqueConstraint("assignment_id", "attempt_number", name="uq_test_attempt_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    assignment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("test_assignments.id", ondelete="CASCADE"),
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[TestAttemptStatus] = mapped_column(
        Enum(TestAttemptStatus, native_enum=False, length=16),
        default=TestAttemptStatus.in_progress,
        server_default=sa_text("'in_progress'"),
    )
    score: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    max_score: Mapped[int] = mapped_column(Integer)
    percent: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))
    passed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_text("false"))
    pass_threshold_percent: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    assignment: Mapped["TestAssignment"] = relationship(back_populates="attempts")
    questions: Mapped[list["TestAttemptQuestion"]] = relationship(
        back_populates="attempt",
        cascade="all, delete-orphan",
        order_by="TestAttemptQuestion.position",
    )


class TestAttemptQuestion(Base):
    """Снимок вопроса на момент старта попытки. `template_question_id` —
    только для трассировки (SET NULL) — результат не зависит от живого шаблона."""

    __tablename__ = "test_attempt_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("test_attempts.id", ondelete="CASCADE"),
        index=True,
    )
    template_question_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("test_questions.id", ondelete="SET NULL"),
        nullable=True,
    )
    text: Mapped[str] = mapped_column(Text)
    type: Mapped[TestQuestionType] = mapped_column(
        Enum(TestQuestionType, native_enum=False, length=32),
    )
    points: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))

    attempt: Mapped["TestAttempt"] = relationship(back_populates="questions")
    options: Mapped[list["TestAttemptOption"]] = relationship(
        back_populates="attempt_question",
        cascade="all, delete-orphan",
        order_by="TestAttemptOption.position",
    )


class TestAttemptOption(Base):
    """Снимок варианта + выбор сотрудника. `is_correct` скрыт от сотрудника
    до сдачи (на уровне схемы ответа, не колонки)."""

    __tablename__ = "test_attempt_options"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    attempt_question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("test_attempt_questions.id", ondelete="CASCADE"),
        index=True,
    )
    template_option_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("test_question_options.id", ondelete="SET NULL"),
        nullable=True,
    )
    text: Mapped[str] = mapped_column(String(500))
    is_correct: Mapped[bool] = mapped_column(Boolean)
    is_selected: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_text("false")
    )
    position: Mapped[int] = mapped_column(Integer, default=0, server_default=sa_text("0"))

    attempt_question: Mapped["TestAttemptQuestion"] = relationship(back_populates="options")
