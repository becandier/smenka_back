"""checklist_photo_retention: срок хранения фото чек-листов

Revision ID: 56fddf4416fa
Revises: 9a0b1c2d3e4f
Create Date: 2026-09-13 00:00:00.000000+00:00

`files.purged_at` (nullable TIMESTAMPTZ, без server_default) — момент удаления
ОБЪЕКТА из S3 по сроку хранения (см.
docs/tasks/checklist_photo_retention/backend.md). `NULL` — объект в хранилище
на месте. Строка `files` (storage_key, size_bytes, checksum_sha256) переживает
удаление объекта — она след для истории чек-листа. Инвариант: `purged_at IS
NOT NULL` ⇒ объекта по `storage_key` больше нет, presigned-ссылка на него не
выдаётся никогда.

Частичный индекс `ix_files_retention_candidates` по `(category, created_at)` с
условием `purged_at IS NULL AND is_attached = true` — под выборку кандидатов
Celery-задачи `purge_expired_checklist_photos` (раз в сутки, 02:00 UTC).
Таблица `files` на проде маленькая (сотни строк, ~11 МБ/сутки прироста) и
растёт медленно — обычный (не `CONCURRENTLY`) `CREATE INDEX` не создаёт
заметной паузы записи (в отличие от `d43e672a012a` для горячей
`checklist_instances`).

Бэкфилл не нужен: колонка nullable без server_default — у всех существующих
строк `purged_at` остаётся NULL (= «объект на месте»), что и есть корректное
значение для уже загруженных файлов.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "56fddf4416fa"
down_revision: str | None = "9a0b1c2d3e4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "files",
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_files_retention_candidates",
        "files",
        ["category", "created_at"],
        unique=False,
        postgresql_where=sa.text("purged_at IS NULL AND is_attached = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_files_retention_candidates", table_name="files")
    op.drop_column("files", "purged_at")
