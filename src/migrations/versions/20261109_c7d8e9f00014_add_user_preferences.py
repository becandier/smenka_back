"""table_column_settings: личные настройки пользователя

Revision ID: c7d8e9f00014
Revises: a2fdde8505d1
Create Date: 2026-11-09 00:00:00.000000+00:00

Новая таблица `user_preferences` — непрозрачные JSON-значения по строковому
ключу, принадлежащие пользователю. PK `(user_id, key)`; отдельного `id` и
дополнительных индексов нет — все чтения идут по `user_id` (левый префикс PK).
FK на `users.id` с `ON DELETE CASCADE`. Только аддитивно, seed не нужен.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7d8e9f00014"
down_revision: str | None = "a2fdde8505d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("user_preferences")
