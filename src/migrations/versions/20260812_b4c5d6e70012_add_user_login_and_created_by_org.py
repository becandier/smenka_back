"""admin_created_accounts: users.login + created_by_org_id, email nullable

Revision ID: b4c5d6e70012
Revises: 9632b96364fd
Create Date: 2026-08-12 12:00:00.000000+00:00

Учётка, заведённая админом организации, может не иметь email вообще —
идентификатор входа тогда только `login`. `created_by_org_id` — ключевое поле
для прав на пароль/логин учётки (см. `services/member_account.py`): сброс
пароля и смена логина разрешены только организации, которая завела учётку.

Backfill не нужен — у всех существующих пользователей email уже заполнен, а
`login`/`created_by_org_id` для них остаются NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e70012"
down_revision: str | None = "9632b96364fd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.add_column("users", sa.Column("login", sa.String(length=32), nullable=True))
    op.add_column(
        "users",
        sa.Column("created_by_org_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        op.f("ix_users_created_by_org_id"),
        "users",
        ["created_by_org_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_users_created_by_org_id_organizations",
        "users",
        "organizations",
        ["created_by_org_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Уникальность login — глобальная по платформе, без учёта регистра;
    # частичный индекс — NULL-логины (обычный саморегистрационный путь) не
    # участвуют в уникальности.
    op.create_index(
        "uq_users_login_lower",
        "users",
        [sa.text("lower(login)")],
        unique=True,
        postgresql_where=sa.text("login IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_users_email_or_login",
        "users",
        "email IS NOT NULL OR login IS NOT NULL",
    )
    # Не unique (email остаётся регистрозависимо уникальным — вне scope) —
    # только ускоряет email-фолбэк входа (`func.lower(User.email) == ...` в
    # `services/auth._find_user_by_ident`), иначе full scan на каждый логин
    # по email, т.к. существующий `ix_users_email` — обычный btree на самой
    # колонке, lower() под него не попадает.
    op.create_index(
        "ix_users_email_lower",
        "users",
        [sa.text("lower(email)")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_users_email_lower", table_name="users")
    op.drop_constraint("ck_users_email_or_login", "users", type_="check")
    op.drop_index("uq_users_login_lower", table_name="users")
    op.drop_constraint(
        "fk_users_created_by_org_id_organizations", "users", type_="foreignkey"
    )
    op.drop_index(op.f("ix_users_created_by_org_id"), table_name="users")
    op.drop_column("users", "created_by_org_id")
    op.drop_column("users", "login")
    # ВНИМАНИЕ: откат сломается (NOT NULL violation), если к этому моменту
    # существуют учётки без email (заведены админом организации, login-only) —
    # такие строки нужно вручную согласовать (удалить/заполнить email) перед
    # downgrade. Корректен только пока таких учёток нет.
    op.alter_column(
        "users",
        "email",
        existing_type=sa.String(length=255),
        nullable=False,
    )
