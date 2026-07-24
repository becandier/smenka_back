"""add employee_tests (тестирование сотрудников: шаблон → снимок попытки)

Revision ID: 9632b96364fd
Revises: 6686fd74797c
Create Date: 2026-07-24 10:05:00.000000+00:00

Порядок — по backend.md фичи employee_tests. Зависит от `notifications`
(назначение теста создаёт уведомление `test_assigned`), поэтому идёт следом:
1. `test_templates` (org-scoped).
2. `test_questions` → `test_question_options`.
3. `test_assignments` (upsert по UNIQUE(template_id, member_id), денорм статуса).
4. `test_attempts` (снимок попытки, свой `pass_threshold_percent` — ADR-003).
5. `test_attempt_questions` → `test_attempt_options` (снимок вопросов/вариантов).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9632b96364fd"
down_revision: str | None = "6686fd74797c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. test_templates
    op.create_table(
        "test_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "pass_threshold_percent", sa.Integer(), server_default=sa.text("70"), nullable=False
        ),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "reveal_answers", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "shuffle_questions", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_test_templates_organization_id", "test_templates", ["organization_id"]
    )
    op.create_index("ix_test_templates_is_archived", "test_templates", ["is_archived"])

    # 2. test_questions
    op.create_table(
        "test_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("points", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["template_id"], ["test_templates.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_test_questions_template_id", "test_questions", ["template_id"])

    # test_question_options
    op.create_table(
        "test_question_options",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.Column("is_correct", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(["question_id"], ["test_questions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_test_question_options_question_id", "test_question_options", ["question_id"]
    )

    # 3. test_assignments
    op.create_table(
        "test_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'assigned'"), nullable=False
        ),
        sa.Column("attempts_used", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("best_percent", sa.Integer(), nullable=True),
        sa.Column("passed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["template_id"], ["test_templates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["member_id"], ["organization_members.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["assigned_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_id", "member_id", name="uq_test_assignment"),
    )
    op.create_index("ix_test_assignments_template_id", "test_assignments", ["template_id"])
    op.create_index("ix_test_assignments_member_id", "test_assignments", ["member_id"])

    # 4. test_attempts
    op.create_table(
        "test_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default=sa.text("'in_progress'"),
            nullable=False,
        ),
        sa.Column("score", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_score", sa.Integer(), nullable=False),
        sa.Column("percent", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("passed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # Снимок template.pass_threshold_percent на момент старта попытки — без
        # server_default: значение всегда явно проставляет приложение (ADR-003).
        sa.Column("pass_threshold_percent", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["assignment_id"], ["test_assignments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assignment_id", "attempt_number", name="uq_test_attempt_number"
        ),
    )
    op.create_index("ix_test_attempts_assignment_id", "test_attempts", ["assignment_id"])

    # 5. test_attempt_questions
    op.create_table(
        "test_attempt_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_question_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(["attempt_id"], ["test_attempts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["template_question_id"], ["test_questions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_test_attempt_questions_attempt_id", "test_attempt_questions", ["attempt_id"]
    )

    # test_attempt_options
    op.create_table(
        "test_attempt_options",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_question_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_option_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("text", sa.String(length=500), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("is_selected", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["attempt_question_id"], ["test_attempt_questions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["template_option_id"], ["test_question_options.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_test_attempt_options_attempt_question_id",
        "test_attempt_options",
        ["attempt_question_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_test_attempt_options_attempt_question_id", table_name="test_attempt_options"
    )
    op.drop_table("test_attempt_options")

    op.drop_index("ix_test_attempt_questions_attempt_id", table_name="test_attempt_questions")
    op.drop_table("test_attempt_questions")

    op.drop_index("ix_test_attempts_assignment_id", table_name="test_attempts")
    op.drop_table("test_attempts")

    op.drop_index("ix_test_assignments_member_id", table_name="test_assignments")
    op.drop_index("ix_test_assignments_template_id", table_name="test_assignments")
    op.drop_table("test_assignments")

    op.drop_index("ix_test_question_options_question_id", table_name="test_question_options")
    op.drop_table("test_question_options")

    op.drop_index("ix_test_questions_template_id", table_name="test_questions")
    op.drop_table("test_questions")

    op.drop_index("ix_test_templates_is_archived", table_name="test_templates")
    op.drop_index("ix_test_templates_organization_id", table_name="test_templates")
    op.drop_table("test_templates")
