"""Учётки, заводимые админом организации (admin_created_accounts).

Владелец/админ организации заводит сотрудника целиком со своей стороны: имя,
опциональные логин/email, пароль (генерируется или задаётся явно) — без
самостоятельной регистрации и подтверждения email. Сброс пароля и смена логина
разрешены ТОЛЬКО для учёток, которые завела эта организация
(`users.created_by_org_id == org_id`) — иначе админ мог бы перехватить личный
аккаунт сотрудника, пришедшего по инвайту (см. backend.md, «Кто может менять
пароль»).
"""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.core.security import generate_password, hash_password
from src.app.models.organization import MemberRole, OrganizationMember
from src.app.models.user import RefreshToken, User
from src.app.services import lockout
from src.app.services.auth import get_user_by_email
from src.app.services.common import ensure_admin_or_owner
from src.app.services.organization import (
    OrgError,
    get_member,
    get_organization,
    normalize_display_name,
)
from src.app.services.organization_role import get_role

logger = get_logger(__name__)


async def _ensure_login_free(
    session: AsyncSession,
    login: str,
    *,
    exclude_user_id: uuid.UUID | None = None,
) -> None:
    conditions = [func.lower(User.login) == login.lower()]
    if exclude_user_id is not None:
        conditions.append(User.id != exclude_user_id)
    result = await session.execute(select(User.id).where(*conditions))
    if result.scalar_one_or_none() is not None:
        raise OrgError("LOGIN_TAKEN", "Логин уже занят", 409)


async def _ensure_email_free(session: AsyncSession, email: str) -> None:
    if await get_user_by_email(session, email) is not None:
        raise OrgError("EMAIL_TAKEN", "Email уже занят", 409)


def _ensure_managed_by_org(user: User, org_id: uuid.UUID) -> None:
    """Учётку может менять (пароль/логин) только организация, которая её завела."""
    if user.created_by_org_id != org_id:
        raise OrgError(
            "PASSWORD_RESET_NOT_ALLOWED",
            "Этой учётной записью управляет сам сотрудник",
            403,
        )


async def create_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    actor_is_super_admin: bool,
    name: str,
    login: str | None,
    email: str | None,
    phone: str | None,
    password: str | None,
    role: str,
    role_id: uuid.UUID | None,
    display_name: str | None,
) -> tuple[OrganizationMember, str]:
    """Завести сотрудника целиком со стороны организации.

    Возвращает (member, plain_password) — открытый пароль существует только в
    возврате этой функции и в ответе эндпоинта, нигде не сохраняется и не
    логируется.
    """
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, actor_id)

    role_enum = MemberRole(role)
    # Владелец может назначить admin, обычный admin-участник — только employee
    # (зеркалит правило update_member_role: role=admin разрешена только
    # owner/super_admin).
    is_owner_or_super = org.owner_id == actor_id or actor_is_super_admin
    if role_enum == MemberRole.admin and not is_owner_or_super:
        raise OrgError("FORBIDDEN", "Только владелец может назначать роль admin", 403)

    if login is not None:
        await _ensure_login_free(session, login)
    if email is not None:
        await _ensure_email_free(session, email)

    role_obj = None
    if role_id is not None:
        role_obj = await get_role(session, org_id, role_id)

    plain_password = password or generate_password()

    user = User(
        name=name,
        email=email,
        login=login,
        phone=phone,
        password_hash=hash_password(plain_password),
        # Код верификации слать некуда (email может отсутствовать) — учётка,
        # заведённая админом, всегда считается подтверждённой.
        is_verified=True,
        created_by_org_id=org_id,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Пред-проверки выше закрывают обычный случай; это — защита от гонки
        # двух параллельных запросов на один и тот же login/email.
        detail = str(exc.orig)
        if "uq_users_login_lower" in detail:
            raise OrgError("LOGIN_TAKEN", "Логин уже занят", 409) from exc
        if "ix_users_email" in detail:
            raise OrgError("EMAIL_TAKEN", "Email уже занят", 409) from exc
        raise

    member = OrganizationMember(
        organization_id=org_id,
        user_id=user.id,
        role=role_enum,
        role_id=role_obj.id if role_obj is not None else None,
        display_name=normalize_display_name(display_name),
    )
    session.add(member)
    await session.flush()
    # Прямое присваивание вместо session.refresh(member, ["user", "custom_role"]):
    # оба объекта уже есть в памяти (только что созданы/получены выше) — refresh
    # добавил бы лишний round-trip в БД за тем, что уже известно.
    member.user = user
    member.custom_role = role_obj

    logger.info(
        "member_created_by_org",
        org_id=str(org_id),
        user_id=str(user.id),
        role=role_enum.value,
        has_login=login is not None,
        has_email=email is not None,
    )
    return member, plain_password


async def reset_password(
    session: AsyncSession,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    target_user_id: uuid.UUID,
    password: str | None,
) -> tuple[OrganizationMember, str]:
    """Сбросить пароль сотруднику, учётку которого завела эта организация.

    Возвращает (member, plain_password) — member.user несёт login/email для
    ответа эндпоинта. Отзывает все refresh-токены пользователя и сбрасывает
    счётчик lockout по его идентификаторам (login/email).
    """
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, actor_id)

    member = await get_member(session, org_id, target_user_id)
    user = member.user
    _ensure_managed_by_org(user, org_id)

    plain_password = password or generate_password()
    user.password_hash = hash_password(plain_password)
    await session.flush()

    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
        .values(revoked=True)
    )

    if user.login:
        await lockout.reset(user.login)
    if user.email:
        await lockout.reset(user.email)

    logger.info("member_password_reset", org_id=str(org_id), user_id=str(user.id))
    return member, plain_password


async def apply_login_update(
    session: AsyncSession,
    member: OrganizationMember,
    new_login: str,
) -> OrganizationMember:
    """Сменить логин уже загруженному участнику. Прав НЕ проверяет и НЕ фетчит
    `member` заново — вызывающий код (`PATCH .../members/{user_id}`) авторизует
    и загружает участника один раз на весь partial-запрос (см.
    `organization.apply_display_name_update` — тот же мотив).

    Разрешено только для учётки, которую завела ЭТА организация
    (`_ensure_managed_by_org`) — это правило владения проверяется здесь, а не
    в эндпоинте, т.к. оно специфично для admin_created_accounts, а не общее
    для org-ресурсов.
    """
    user = member.user
    _ensure_managed_by_org(user, member.organization_id)

    await _ensure_login_free(session, new_login, exclude_user_id=user.id)
    user.login = new_login
    try:
        await session.flush()
    except IntegrityError as exc:
        raise OrgError("LOGIN_TAKEN", "Логин уже занят", 409) from exc

    logger.info("member_login_updated", org_id=str(member.organization_id), user_id=str(user.id))
    return member


__all__ = [
    "apply_login_update",
    "create_member",
    "reset_password",
]
