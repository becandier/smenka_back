"""Платформенные операции super_admin: пользователи, обзор организаций, статистика.

Все функции рассчитаны на вызов из эндпоинтов под `SuperAdminDep` —
проверка роли выполняется на уровне зависимости роутера.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.organization import Organization, OrganizationMember
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User, UserRole


class AdminError(Exception):
    """Ошибка платформенных admin-операций. Маппится в {data,error} в main.py."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


_USER_SORT_COLUMNS: dict[str, Any] = {
    "created_at": User.created_at,
    "email": User.email,
}
_ORG_SORT_COLUMNS: dict[str, Any] = {
    "created_at": Organization.created_at,
    "name": Organization.name,
}


def _order_by(
    columns: dict[str, Any],
    sort: str,
    order: str,
    default_key: str,
) -> Any:
    column = columns.get(sort, columns[default_key])
    return column.asc() if order.lower() == "asc" else column.desc()


async def list_users(
    session: AsyncSession,
    *,
    search: str | None = None,
    role: str | None = None,
    is_verified: bool | None = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> tuple[list[User], int]:
    """Список пользователей с фильтрами/поиском/пагинацией. Returns (users, total)."""
    conditions: list[ColumnElement[bool]] = []
    if search:
        pattern = f"%{search}%"
        conditions.append(User.email.ilike(pattern) | User.name.ilike(pattern))
    if role is not None:
        try:
            conditions.append(User.role == UserRole(role))
        except ValueError:
            conditions.append(false())
    if is_verified is not None:
        conditions.append(User.is_verified.is_(is_verified))

    count_query = select(func.count()).select_from(User)
    if conditions:
        count_query = count_query.where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    query = (
        select(User)
        .order_by(_order_by(_USER_SORT_COLUMNS, sort, order, "created_at"))
        .limit(limit)
        .offset(offset)
    )
    if conditions:
        query = query.where(*conditions)
    users = list((await session.execute(query)).scalars().all())
    return users, total


async def get_user_detail(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> tuple[User, int, int, int]:
    """Пользователь + агрегаты (owned orgs, memberships, shifts). 404 USER_NOT_FOUND."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AdminError("USER_NOT_FOUND", "Пользователь не найден", 404)

    owned = (
        await session.execute(
            select(func.count())
            .select_from(Organization)
            .where(
                Organization.owner_id == user_id,
                Organization.is_deleted.is_(False),
            )
        )
    ).scalar_one()
    member = (
        await session.execute(
            select(func.count())
            .select_from(OrganizationMember)
            .where(OrganizationMember.user_id == user_id)
        )
    ).scalar_one()
    shifts = (
        await session.execute(
            select(func.count()).select_from(Shift).where(Shift.user_id == user_id)
        )
    ).scalar_one()
    return user, owned, member, shifts


async def update_user_role(
    session: AsyncSession,
    user_id: uuid.UUID,
    new_role: str,
    requester_id: uuid.UUID,
) -> User:
    """Сменить глобальную роль. Нельзя снять super_admin с самого себя."""
    if user_id == requester_id and new_role != UserRole.super_admin.value:
        raise AdminError(
            "CANNOT_DEMOTE_SELF",
            "Нельзя снять роль super_admin с самого себя",
            400,
        )
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise AdminError("USER_NOT_FOUND", "Пользователь не найден", 404)

    user.role = UserRole(new_role)
    await session.flush()
    return user


async def list_organizations(
    session: AsyncSession,
    *,
    search: str | None = None,
    is_deleted: bool | None = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "created_at",
    order: str = "desc",
) -> tuple[list[tuple[Organization, str, int]], int]:
    """Обзор организаций: (org, owner_email, member_count). Returns (rows, total)."""
    conditions: list[ColumnElement[bool]] = []
    if search:
        conditions.append(Organization.name.ilike(f"%{search}%"))
    if is_deleted is not None:
        conditions.append(Organization.is_deleted.is_(is_deleted))

    count_query = select(func.count()).select_from(Organization)
    if conditions:
        count_query = count_query.where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    query = (
        select(
            Organization,
            User.email,
            func.count(OrganizationMember.id),
        )
        .join(User, Organization.owner_id == User.id)
        .outerjoin(
            OrganizationMember,
            OrganizationMember.organization_id == Organization.id,
        )
        .group_by(Organization.id, User.id)
        .order_by(_order_by(_ORG_SORT_COLUMNS, sort, order, "created_at"))
        .limit(limit)
        .offset(offset)
    )
    if conditions:
        query = query.where(*conditions)

    rows = (await session.execute(query)).all()
    return [(row[0], row[1], row[2]) for row in rows], total


async def get_stats(session: AsyncSession) -> dict[str, int]:
    """Сводка платформы для дашборда super_admin."""
    users_row = (
        await session.execute(
            select(
                func.count(User.id),
                func.count(User.id).filter(User.is_verified.is_(True)),
            )
        )
    ).one()
    orgs_row = (
        await session.execute(
            select(
                func.count(Organization.id),
                func.count(Organization.id).filter(Organization.is_deleted.is_(False)),
            )
        )
    ).one()

    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=now.weekday())

    shifts_active = (
        await session.execute(
            select(func.count(Shift.id)).where(Shift.status == ShiftStatus.active)
        )
    ).scalar_one()
    shifts_today = (
        await session.execute(select(func.count(Shift.id)).where(Shift.started_at >= today_start))
    ).scalar_one()
    shifts_week = (
        await session.execute(select(func.count(Shift.id)).where(Shift.started_at >= week_start))
    ).scalar_one()

    return {
        "users_total": users_row[0],
        "users_verified": users_row[1],
        "organizations_total": orgs_row[0],
        "organizations_active": orgs_row[1],
        "shifts_active": shifts_active,
        "shifts_today": shifts_today,
        "shifts_week": shifts_week,
    }
