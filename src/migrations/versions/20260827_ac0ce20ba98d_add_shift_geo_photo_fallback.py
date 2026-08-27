"""shift_geo_photo_fallback: старт смены по фото при недоступной геолокации

Revision ID: ac0ce20ba98d
Revises: 65aa0f57410e
Create Date: 2026-08-27 05:45:27.072222+00:00

Две nullable-колонки на `shifts` (backend.md фичи `shift_geo_photo_fallback`):

- `geo_fallback_photo_file_id` — FK на `files.id` `ON DELETE SET NULL`: снимок с
  камеры, которым сотрудник подтвердил старт вместо координат;
- `geo_fallback_reason` — VARCHAR(40) с машинным кодом гео-ошибки клиента
  (`GEO_PERMISSION_DENIED`, `GEO_SERVICE_DISABLED`, …). Инвариант: «смена
  стартовала без геопроверки» ⇔ `geo_fallback_reason IS NOT NULL`, поэтому
  признак переживает удаление самого фото (FK уходит в NULL, причина остаётся).

Новая категория файла `shift_geo_photo` схему НЕ меняет: `files.category` — не
native PG enum, а обычный VARCHAR(32) без CHECK (`Enum(..., native_enum=False)`,
миграция `d4f5a6b80004`), так что `ALTER TYPE ... ADD VALUE` не нужен. По той же
причине downgrade полностью обратим — удаляются только две колонки.

Обе колонки nullable без server_default: у всех существующих смен они NULL, что
и означает «обычный старт». Старые мобильные билды не ломаются.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ac0ce20ba98d"
down_revision: str | None = "65aa0f57410e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "shifts",
        sa.Column("geo_fallback_photo_file_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "shifts",
        sa.Column(
            "geo_fallback_reason",
            sa.String(length=40),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_shifts_geo_fallback_photo_file_id_files",
        "shifts",
        "files",
        ["geo_fallback_photo_file_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_shifts_geo_fallback_photo_file_id_files",
        "shifts",
        type_="foreignkey",
    )
    op.drop_column("shifts", "geo_fallback_reason")
    op.drop_column("shifts", "geo_fallback_photo_file_id")
