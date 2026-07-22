"""checklist_reports: индексы под фильтр checklists и реестр checklist-instances

Revision ID: a4b5c6d7e803
Revises: 82e9e9625926
Create Date: 2026-07-22 12:00:00.000000+00:00

Только индексы, данные не трогаются:
- `checklist_instances.template_id` — сейчас FK без индекса, а фильтр по
  шаблону — основной сценарий реестра `GET /organizations/{org_id}/checklist-instances`.
- `checklist_instances (shift_id, status)` — составной, под агрегаты сводки
  `checklists_summary`/фильтр `checklists` в списке смен и фильтр `status`
  реестра (`shift_id` уже покрыт одиночным `ix_checklist_instances_shift_id`,
  этот индекс — дополнительный, под комбинацию с status).
- `shifts.organization_id` уже индексирован (`ix_shifts_organization_id`,
  миграция `fd49e1a252de`) — новый индекс не нужен.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a4b5c6d7e803"
down_revision: str | None = "82e9e9625926"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_checklist_instances_template_id",
        "checklist_instances",
        ["template_id"],
    )
    op.create_index(
        "ix_checklist_instances_shift_id_status",
        "checklist_instances",
        ["shift_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_checklist_instances_shift_id_status",
        table_name="checklist_instances",
    )
    op.drop_index(
        "ix_checklist_instances_template_id",
        table_name="checklist_instances",
    )
