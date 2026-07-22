"""привязка чек-листов к рабочим точкам (checklist_template_locations)

Revision ID: 82e9e9625926
Revises: e5f6a7b80011
Create Date: 2026-07-21 15:43:42.979943+00:00
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '82e9e9625926'
down_revision: str | None = 'e5f6a7b80011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Пустая таблица связей = нулевое изменение поведения на проде (см.
    # backend.md фичи checklist_work_location) — данные не бэкфиллятся.
    op.create_table(
        'checklist_template_locations',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('template_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('work_location_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['template_id'], ['checklist_templates.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['work_location_id'], ['work_locations.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'template_id', 'work_location_id', name='uq_checklist_template_location',
        ),
    )
    op.create_index(
        'ix_checklist_template_locations_template_id',
        'checklist_template_locations',
        ['template_id'],
    )
    op.create_index(
        'ix_checklist_template_locations_work_location_id',
        'checklist_template_locations',
        ['work_location_id'],
    )


def downgrade() -> None:
    op.drop_index(
        'ix_checklist_template_locations_work_location_id',
        table_name='checklist_template_locations',
    )
    op.drop_index(
        'ix_checklist_template_locations_template_id',
        table_name='checklist_template_locations',
    )
    op.drop_table('checklist_template_locations')
