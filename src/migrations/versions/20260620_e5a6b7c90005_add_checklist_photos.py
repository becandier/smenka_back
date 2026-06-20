"""add checklist photos (фото-подтверждения пунктов чек-листов)

Revision ID: e5a6b7c90005
Revises: d4f5a6b80004
Create Date: 2026-06-20 22:30:00.000000+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a6b7c90005"
down_revision: str | None = "d4f5a6b80004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # +2 поля на шаблонные пункты (настройка) и снимок на экземпляры пунктов.
    op.add_column(
        "checklist_template_items",
        sa.Column(
            "photo_requirement",
            sa.String(length=32),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
    )
    op.add_column(
        "checklist_template_items",
        sa.Column(
            "photo_source",
            sa.String(length=32),
            server_default=sa.text("'camera'"),
            nullable=False,
        ),
    )
    op.add_column(
        "checklist_instance_items",
        sa.Column(
            "photo_requirement",
            sa.String(length=32),
            server_default=sa.text("'none'"),
            nullable=False,
        ),
    )
    op.add_column(
        "checklist_instance_items",
        sa.Column(
            "photo_source",
            sa.String(length=32),
            server_default=sa.text("'camera'"),
            nullable=False,
        ),
    )

    op.create_table(
        "checklist_item_photos",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("instance_item_id", sa.UUID(), nullable=False),
        sa.Column("file_id", sa.UUID(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column(
            "position",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instance_item_id"],
            ["checklist_instance_items.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_checklist_item_photos_instance_item_id",
        "checklist_item_photos",
        ["instance_item_id"],
    )
    # Один файл = одна привязка.
    op.create_index(
        "ix_checklist_item_photos_file_id",
        "checklist_item_photos",
        ["file_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_checklist_item_photos_file_id",
        table_name="checklist_item_photos",
    )
    op.drop_index(
        "ix_checklist_item_photos_instance_item_id",
        table_name="checklist_item_photos",
    )
    op.drop_table("checklist_item_photos")
    op.drop_column("checklist_instance_items", "photo_source")
    op.drop_column("checklist_instance_items", "photo_requirement")
    op.drop_column("checklist_template_items", "photo_source")
    op.drop_column("checklist_template_items", "photo_requirement")
