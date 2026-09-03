import uuid
from datetime import datetime as dt_datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, Request

from src.app.api.deps import CurrentUserDep, SessionDep, SuperAdminDep
from src.app.api.v1.checklist_instances import _org_instance_row_to_response
from src.app.api.v1.shifts import _shift_to_response
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.models.organization_settings import DEFAULT_CHECKLIST_GRACE_MINUTES
from src.app.models.user import UserRole
from src.app.schemas.audit import AuditLogEntry, AuditLogListResponse
from src.app.schemas.base import ApiResponse
from src.app.schemas.checklist import OrgChecklistInstanceListResponse
from src.app.schemas.member_account import (
    MemberCreateRequest,
    MemberCreateResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
)
from src.app.schemas.organization import (
    InviteCodeResponse,
    JoinResponse,
    MemberListResponse,
    MemberResponse,
    MemberRoleUpdate,
    MemberUpdateRequest,
    OrganizationCreate,
    OrganizationListResponse,
    OrganizationResponse,
    OrganizationUpdate,
)
from src.app.schemas.organization_settings import (
    OrganizationSettingsResponse,
    OrganizationSettingsUpdate,
)
from src.app.schemas.organization_stats import OrgStatsResponse
from src.app.schemas.shift import ShiftListResponse, ShiftResponse
from src.app.services import audit as audit_service
from src.app.services import checklist_instance as checklist_instance_service
from src.app.services import entitlements
from src.app.services import member_account as member_account_service
from src.app.services import organization as org_service
from src.app.services import organization_settings as settings_service
from src.app.services import overtime as overtime_service
from src.app.services import shift as shift_service
from src.app.services import subscription as subscription_service
from src.app.services.overtime import DEFAULT_OVERTIME_REQUEST_DAYS
from src.app.utils.request import get_client_ip

if TYPE_CHECKING:
    from src.app.models.audit_log import AuditLog
    from src.app.models.organization import Organization, OrganizationMember
    from src.app.models.organization_settings import OrganizationSettings

router = APIRouter(prefix="/organizations", tags=["organizations"])


def _org_to_response(
    org: "Organization",
    my_role: str | None = None,
    my_custom_role: Any = None,
    subscription: dict[str, Any] | None = None,
) -> dict[str, Any]:
    geo_check = org.settings.geo_check_enabled if org.settings else False
    require_work_location = org.settings.require_work_location if org.settings else False
    overtime_request_days = (
        org.settings.overtime_request_days if org.settings else DEFAULT_OVERTIME_REQUEST_DAYS
    )
    # checklist_grace_period: денормализовано по тому же прецеденту, что и
    # overtime_request_days выше — employee не имеет доступа к /settings, но
    # должен знать длительность окна (диалог завершения смены).
    checklist_grace_minutes = (
        org.settings.checklist_grace_minutes if org.settings else DEFAULT_CHECKLIST_GRACE_MINUTES
    )
    custom_role_payload = None
    if my_custom_role is not None:
        custom_role_payload = {
            "id": str(my_custom_role.id),
            "name": my_custom_role.name,
            "created_at": my_custom_role.created_at.isoformat(),
        }
    return OrganizationResponse(
        id=str(org.id),
        name=org.name,
        owner_id=str(org.owner_id),
        invite_code=org.invite_code,
        is_deleted=org.is_deleted,
        deleted_at=org.deleted_at,
        geo_check_enabled=geo_check,
        require_work_location=require_work_location,
        timezone=org.timezone,
        overtime_request_days=overtime_request_days,
        checklist_grace_minutes=checklist_grace_minutes,
        created_at=org.created_at,
        my_role=my_role,
        my_custom_role=custom_role_payload,
        subscription=subscription,
    ).model_dump(mode="json")


def _member_to_response(
    member: "OrganizationMember",
    current_rate: Any = None,
) -> dict[str, Any]:
    from src.app.api.v1.payroll import current_rate_payload

    custom_role = None
    if member.custom_role is not None:
        custom_role = {
            "id": str(member.custom_role.id),
            "name": member.custom_role.name,
            "created_at": member.custom_role.created_at.isoformat(),
        }
    return MemberResponse(
        id=str(member.id),
        organization_id=str(member.organization_id),
        user_id=str(member.user_id),
        user_name=member.user.name,
        # user_email остаётся строкой в существующем контракте (обратная
        # совместимость мобильных билдов) — "" вместо null для admin-created
        # учёток без почты (admin_created_accounts).
        user_email=member.user.email_display,
        user_login=member.user.login,
        password_managed=member.user.created_by_org_id == member.organization_id,
        display_name=member.display_name,
        role=member.role.value,
        custom_role=custom_role,
        joined_at=member.joined_at,
        current_rate=current_rate_payload(current_rate),
    ).model_dump(mode="json")


@router.post(
    "",
    status_code=201,
    summary="Создать организацию",
    description=(
        "Создаёт организацию. Только для super_admin. Текущий пользователь "
        "становится владельцем (Owner). Автоматически создаётся инвайт-код и "
        "настройки по умолчанию."
    ),
)
async def create_organization(
    body: OrganizationCreate,
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    org = await org_service.create_organization(session, body.name, user.id)
    await session.commit()
    return ApiResponse.success(_org_to_response(org, "owner", None))


@router.get(
    "",
    summary="Мои организации",
    description=(
        "Список всех организаций, где текущий пользователь — владелец или "
        "участник. В ответе поля my_role и my_custom_role отражают роль "
        "текущего пользователя в каждой организации."
    ),
)
async def list_organizations(
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    orgs = await org_service.get_user_organizations(session, user.id)
    roles = await org_service.batch_get_my_roles(session, orgs, user.id)
    return ApiResponse.success(
        OrganizationListResponse(
            items=[_org_to_response(o, *roles.get(o.id, (None, None))) for o in orgs],
        ).model_dump(mode="json")
    )


@router.get(
    "/all",
    summary="Все организации (super_admin)",
    description=(
        "Список ВСЕХ организаций системы. Только для super_admin. "
        "my_role/my_custom_role заполнены, если super_admin сам является "
        "владельцем или участником конкретной организации, иначе null."
    ),
)
async def list_all_organizations(
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    orgs = await org_service.get_all_organizations(session)
    roles = await org_service.batch_get_my_roles(session, orgs, user.id)
    return ApiResponse.success(
        OrganizationListResponse(
            items=[_org_to_response(o, *roles.get(o.id, (None, None))) for o in orgs],
        ).model_dump(mode="json")
    )


@router.get(
    "/{org_id}",
    summary="Получить организацию",
    description=(
        "Информация об организации по ID. Поля my_role/my_custom_role "
        "отражают роль текущего пользователя в этой организации."
    ),
)
async def get_organization(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    org = await org_service.get_organization(session, org_id)
    roles = await org_service.batch_get_my_roles(session, [org], user.id)
    my_role, my_custom_role = roles.get(org.id, (None, None))

    # tariffs: additive-поле subscription — только owner/admin/super_admin,
    # для employee всегда null (backend.md, п.3 API-контрактов). Организация
    # без строки subscriptions (в проде невозможно — автосоздание + data-
    # миграция; в тестах бывает — прямое создание Organization через ORM в
    # обход сервиса) не должна ронять весь эндпоинт: additive-поле просто
    # остаётся null, тем же fail-open духом, что и require_active_subscription.
    subscription_payload: dict[str, Any] | None = None
    if my_role in ("owner", "admin") or user.role == UserRole.super_admin:
        try:
            subscription_payload = await subscription_service.build_subscription_payload(
                session, org_id
            )
        except entitlements.SubscriptionError:
            subscription_payload = None

    return ApiResponse.success(
        _org_to_response(org, my_role, my_custom_role, subscription_payload)
    )


@router.patch(
    "/{org_id}",
    summary="Обновить организацию",
    description=(
        "Обновляет название организации. Доступно владельцу (Owner), участнику с "
        "ролью admin и super_admin. В ответе my_role/my_custom_role отражают "
        "фактическую роль вызывающего."
    ),
)
async def update_organization(
    org_id: uuid.UUID,
    body: OrganizationUpdate,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    org = await org_service.update_organization(
        session, org_id, user.id, name=body.name, timezone=body.timezone
    )
    summary: dict[str, Any] = {}
    if body.name is not None:
        summary["name"] = body.name
    if body.timezone is not None:
        summary["timezone"] = body.timezone
    await audit_service.record(
        session,
        action=AuditAction.org_update,
        resource_type=AuditResource.organization,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=org_id,
        summary=summary,
        ip_address=get_client_ip(request),
    )
    await session.commit()
    roles = await org_service.batch_get_my_roles(session, [org], user.id)
    my_role, my_custom_role = roles.get(org.id, (None, None))
    return ApiResponse.success(_org_to_response(org, my_role, my_custom_role))


@router.delete(
    "/{org_id}",
    status_code=200,
    summary="Удалить организацию",
    description=("Мягкое удаление организации (soft delete). Только для владельца (Owner)."),
)
async def delete_organization(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    await org_service.delete_organization(session, org_id, user.id)
    await audit_service.record(
        session,
        action=AuditAction.org_delete,
        resource_type=AuditResource.organization,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=org_id,
        summary={"soft_delete": True},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success({"message": "Организация удалена"})


@router.post(
    "/{org_id}/rotate-invite",
    status_code=200,
    summary="Ротация инвайт-кода",
    description=(
        "Генерирует новый инвайт-код. Старый перестаёт работать. "
        "Для владельца и админа организации."
    ),
)
async def rotate_invite_code(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    new_code = await org_service.rotate_invite_code(session, org_id, user.id)
    # Сам инвайт-код не пишем в summary — это access-credential.
    await audit_service.record(
        session,
        action=AuditAction.org_invite_rotate,
        resource_type=AuditResource.organization,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=org_id,
        summary={"rotated": True},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(InviteCodeResponse(invite_code=new_code).model_dump())


@router.post(
    "/join/{invite_code}",
    status_code=201,
    summary="Присоединиться по инвайт-коду",
    description=(
        "Присоединяет текущего пользователя к организации с ролью Employee. "
        "Владелец не может присоединиться к своей организации."
    ),
)
async def join_organization(
    invite_code: str,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    org, member = await org_service.join_by_invite(session, invite_code, user.id)
    await audit_service.record(
        session,
        action=AuditAction.member_join,
        resource_type=AuditResource.member,
        organization_id=org.id,
        actor_user_id=user.id,
        resource_id=member.id,
        summary={"role": member.role.value},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    geo_check = org.settings.geo_check_enabled if org.settings else False
    require_work_location = org.settings.require_work_location if org.settings else False
    return ApiResponse.success(
        JoinResponse(
            organization_id=str(org.id),
            organization_name=org.name,
            role=member.role.value,
            geo_check_enabled=geo_check,
            require_work_location=require_work_location,
        ).model_dump()
    )


@router.post(
    "/{org_id}/members",
    status_code=201,
    summary="Завести сотрудника (admin_created_accounts)",
    description=(
        "Заводит сотрудника целиком со стороны организации: имя, опциональные "
        "login/email (хотя бы одно обязательно), пароль (не передан — сервер "
        "генерирует). Учётка сразу is_verified — письмо не отправляется. "
        "Пароль возвращается один раз в ответе и нигде больше не сохраняется. "
        "Доступно владельцу (Owner) и admin-участнику, оба могут назначить "
        "любую роль, включая admin."
    ),
)
async def create_member(
    org_id: uuid.UUID,
    body: MemberCreateRequest,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    role_uuid = uuid.UUID(body.role_id) if body.role_id else None
    member, plain_password = await member_account_service.create_member(
        session,
        org_id,
        user.id,
        name=body.name,
        login=body.login,
        email=body.email,
        phone=body.phone,
        password=body.password,
        role=body.role,
        role_id=role_uuid,
        display_name=body.display_name,
    )
    await audit_service.record(
        session,
        action=AuditAction.member_create,
        resource_type=AuditResource.member,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=member.id,
        # Открытый пароль в аудит не пишем — только факт наличия login/email.
        summary={
            "role": member.role.value,
            "login": member.user.login,
            "has_email": member.user.email is not None,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(
        MemberCreateResponse(
            member=MemberResponse(**_member_to_response(member)),
            login=member.user.login,
            password=plain_password,
        ).model_dump()
    )


@router.get(
    "/{org_id}/members",
    summary="Список участников",
    description=(
        "Список всех участников организации с их ролями. Доступно владельцу и "
        "участникам. Поле current_rate (действующая ставка) заполняется только "
        "для владельца и админов; для остальных участников оно всегда null."
    ),
)
async def list_members(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    from src.app.services import payroll as payroll_service
    from src.app.services.common import AccessError, ensure_admin_or_owner

    members = await org_service.get_members(session, org_id, user.id)

    # Ставки видят только owner/admin/super_admin (ТЗ payroll);
    # для employee current_rate всегда null.
    current_rates: dict[uuid.UUID, Any] = {}
    org = await org_service.get_organization(session, org_id)
    try:
        await ensure_admin_or_owner(session, org, user.id)
    except AccessError:
        pass
    else:
        current_rates = await payroll_service.get_current_rates(
            session,
            [m.id for m in members],
        )

    return ApiResponse.success(
        MemberListResponse(
            items=[_member_to_response(m, current_rates.get(m.id)) for m in members],
        ).model_dump(mode="json")
    )


@router.delete(
    "/{org_id}/members/{member_user_id}",
    summary="Удалить участника",
    description=(
        "Удаляет участника из организации. Владелец и админ могут удалять "
        "других. Любой участник может покинуть организацию сам (передав свой "
        "user_id)."
    ),
)
async def remove_member(
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    member_id = await org_service.remove_member(session, org_id, member_user_id, user.id)
    await audit_service.record(
        session,
        action=AuditAction.member_remove,
        resource_type=AuditResource.member,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=member_id,
        summary={
            "removed_user_id": str(member_user_id),
            "self_leave": member_user_id == user.id,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success({"message": "Участник удалён"})


@router.post(
    "/{org_id}/members/{member_user_id}/reset-password",
    summary="Сбросить пароль сотруднику (admin_created_accounts)",
    description=(
        "Сбрасывает пароль сотруднику, учётку которого завела ЭТА организация "
        "(users.created_by_org_id == org_id) — иначе 403 "
        "PASSWORD_RESET_NOT_ALLOWED (сотрудник с личной учёткой, пришедший по "
        "инвайту, под это правило не попадает). password не передан/null — "
        "сервер генерирует. Отзывает все refresh-токены пользователя. Доступно "
        "владельцу (Owner) и admin-участнику."
    ),
)
async def reset_member_password(
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    body: ResetPasswordRequest,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    member, plain_password = await member_account_service.reset_password(
        session,
        org_id,
        user.id,
        member_user_id,
        body.password,
    )
    await audit_service.record(
        session,
        action=AuditAction.member_password_reset,
        resource_type=AuditResource.member,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=member.id,
        # Открытый пароль в аудит не пишем.
        summary={"login": member.user.login},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(
        ResetPasswordResponse(
            user_id=str(member.user_id),
            login=member.user.login,
            password=plain_password,
        ).model_dump()
    )


@router.patch(
    "/{org_id}/members/{member_user_id}/role",
    summary="Изменить роль участника",
    description=(
        "Назначает или снимает роль admin у участника. Доступно владельцу "
        "(Owner), admin-участнику организации и super_admin. Нельзя изменить "
        "собственную роль (кроме super_admin) — 403 FORBIDDEN."
    ),
)
async def update_member_role(
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    body: MemberRoleUpdate,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    member = await org_service.update_member_role(
        session,
        org_id,
        member_user_id,
        body.role,
        user.id,
        is_super_admin=user.role == UserRole.super_admin,
    )
    await audit_service.record(
        session,
        action=AuditAction.member_role_update,
        resource_type=AuditResource.member,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=member.id,
        summary={"new_role": body.role, "user_id": str(member_user_id)},
        ip_address=get_client_ip(request),
    )
    await session.commit()

    # Эндпоинт доступен только owner/admin/super_admin — ставку показываем всегда
    from src.app.services import payroll as payroll_service

    current_rates = await payroll_service.get_current_rates(session, [member.id])
    return ApiResponse.success(_member_to_response(member, current_rates.get(member.id)))


@router.patch(
    "/{org_id}/members/{member_user_id}",
    summary="Обновить участника (имя в организации, логин)",
    description=(
        "Partial-обновление: правятся только переданные ключи. `display_name` — "
        "имя, которым участник отображается только в этой организации "
        "(настоящее User.name не меняется); null/пустая строка сбрасывают на "
        "него, ключ отсутствует — не менять. `login` — логин для входа "
        "(admin_created_accounts): меняется ТОЛЬКО для учёток, заведённых ЭТОЙ "
        "организацией (users.created_by_org_id == org_id), иначе 403 "
        "PASSWORD_RESET_NOT_ALLOWED; занят другим пользователем → 409 "
        "LOGIN_TAKEN; ключ отсутствует/null — не менять. Доступно владельцу "
        "(Owner), admin-участнику и super_admin; сотрудник (в т.ч. себе) "
        "получает 403 — это управленческий атрибут."
    ),
)
async def update_member(
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    body: MemberUpdateRequest,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    from src.app.services.common import ensure_admin_or_owner

    # Единая точка авторизации + загрузки участника на весь partial-запрос:
    # тело может менять и display_name, и login одновременно (или ни одного —
    # пустой body), поэтому и права, и сам участник достаются один раз, а не
    # по разу на каждое поле (apply_* ниже работают с уже готовым `member`).
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id)
    await entitlements.require_active_subscription(session, org, user.id)
    member = await org_service.get_member(session, org_id, member_user_id)

    if "display_name" in body.model_fields_set:
        member, old_value, new_value = await org_service.apply_display_name_update(
            session,
            member,
            body.display_name,
        )
        await audit_service.record(
            session,
            action=AuditAction.member_display_name_update,
            resource_type=AuditResource.member,
            organization_id=org_id,
            actor_user_id=user.id,
            resource_id=member.id,
            summary={
                "user_id": str(member_user_id),
                "old_display_name": old_value,
                "new_display_name": new_value,
            },
            ip_address=get_client_ip(request),
        )

    if body.login is not None:
        member = await member_account_service.apply_login_update(
            session,
            member,
            body.login,
        )
        await audit_service.record(
            session,
            action=AuditAction.member_login_update,
            resource_type=AuditResource.member,
            organization_id=org_id,
            actor_user_id=user.id,
            resource_id=member.id,
            summary={"user_id": str(member_user_id), "new_login": member.user.login},
            ip_address=get_client_ip(request),
        )

    await session.commit()

    # Эндпоинт доступен только owner/admin/super_admin — ставку показываем всегда
    # (то же правило видимости current_rate, что и в list_members/update_member_role).
    from src.app.services import payroll as payroll_service

    current_rates = await payroll_service.get_current_rates(session, [member.id])
    return ApiResponse.success(_member_to_response(member, current_rates.get(member.id)))


def _settings_to_response(s: "OrganizationSettings") -> dict[str, Any]:
    return OrganizationSettingsResponse(
        organization_id=str(s.organization_id),
        geo_check_enabled=s.geo_check_enabled,
        require_work_location=s.require_work_location,
        max_pause_minutes=s.max_pause_minutes,
        max_pauses_per_shift=s.max_pauses_per_shift,
        auto_finish_by_schedule=s.auto_finish_by_schedule,
        require_schedule=s.require_schedule,
        late_tolerance_minutes=s.late_tolerance_minutes,
        overtime_request_days=s.overtime_request_days,
        early_start_minutes=s.early_start_minutes,
        checklist_grace_minutes=s.checklist_grace_minutes,
    ).model_dump()


@router.get(
    "/{org_id}/settings",
    summary="Настройки организации",
    description=(
        "Текущие настройки организации (геопроверка, лимиты пауз, "
        "автозавершение). Доступно владельцу (Owner) и админам."
    ),
)
async def get_org_settings(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    settings = await settings_service.get_settings(session, org_id, user.id)
    return ApiResponse.success(_settings_to_response(settings))


@router.patch(
    "/{org_id}/settings",
    summary="Обновить настройки",
    description=(
        "Обновляет настройки организации. Передавайте только поля, которые "
        "нужно изменить. Доступно владельцу (Owner) и админам."
    ),
)
async def update_org_settings(
    org_id: uuid.UUID,
    body: OrganizationSettingsUpdate,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    fields = body.model_dump(exclude_unset=True)
    settings = await settings_service.update_settings(
        session,
        org_id,
        user.id,
        **fields,
    )
    await audit_service.record(
        session,
        action=AuditAction.settings_update,
        resource_type=AuditResource.settings,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=org_id,
        summary=fields,
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(_settings_to_response(settings))


@router.get(
    "/{org_id}/shifts",
    response_model=ApiResponse[ShiftListResponse],
    summary="Смены сотрудников",
    description=(
        "Список смен сотрудников организации с пагинацией и фильтрами. "
        "Доступно владельцу (Owner) и админам."
    ),
)
async def list_org_shifts(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    user_id: uuid.UUID | None = Query(None, description="Фильтр по UUID сотрудника"),
    status: str | None = Query(None, description="Фильтр по статусу: active, paused, finished"),
    date_from: dt_datetime | None = Query(None, description="Смены начатые после этой даты"),
    date_to: dt_datetime | None = Query(None, description="Смены начатые до этой даты"),
    checklists: str | None = Query(
        None,
        description="Фильтр по чек-листам: none, all_completed, has_incomplete, "
        "required_incomplete (checklist_reports)",
    ),
    only_late: bool | None = Query(
        None, description="Только смены с опозданием (с учётом допуска, work_schedules)"
    ),
    work_schedule_id: uuid.UUID | None = Query(None, description="Фильтр по графику работы"),
    has_overtime: str | None = Query(
        None, description="Фильтр по заявке на переработку: pending, approved, any"
    ),
    include_deleted: bool = Query(
        False,
        description="Показывать и удалённые смены (soft-delete). "
        "Только owner/admin (manual_time_entry)",
    ),
    only_manual: bool = Query(
        False,
        description="Только смены, заведённые или правленые вручную "
        "(created_by_user_id IS NOT NULL OR edited_at IS NOT NULL, manual_time_entry)",
    ),
    geo_fallback: bool | None = Query(
        None,
        description="Фильтр «старт без геопроверки» (shift_geo_photo_fallback): "
        "true — только смены, стартовавшие по фото вместо координат, false — только "
        "обычные, не передан — без фильтра",
    ),
    limit: int = Query(20, ge=1, le=100, description="Размер страницы (1–100)"),
    offset: int = Query(0, ge=0, description="Смещение для пагинации"),
    sort: str = Query("started_at", description="Поле сортировки: started_at, finished_at"),
    order: str = Query("desc", description="Направление: asc или desc"),
) -> ApiResponse:
    # Only owner or admin can view org shifts
    from src.app.services.work_location import _check_admin_or_owner

    org = await org_service.get_organization(session, org_id)
    await _check_admin_or_owner(session, org, user.id)

    from src.app.models.shift import ShiftStatus as ShiftStatusEnum
    from src.app.services.shift import ShiftError

    status_enum = None
    if status is not None:
        try:
            status_enum = ShiftStatusEnum(status)
        except ValueError:
            raise ShiftError(
                "INVALID_STATUS",
                f"Статус должен быть: {', '.join(s.value for s in ShiftStatusEnum)}",
                400,
            ) from None

    shift_service.validate_date_range(date_from, date_to)
    shift_service.validate_checklists_filter(checklists)
    shift_service.validate_has_overtime_filter(has_overtime)

    org_settings = org.settings
    late_tolerance = org_settings.late_tolerance_minutes if org_settings is not None else 0

    shifts, total = await shift_service.get_org_shifts(
        session,
        org_id,
        user_id=user_id,
        status=status_enum,
        date_from=date_from,
        date_to=date_to,
        checklists=checklists,
        only_late=only_late,
        late_tolerance_minutes=late_tolerance,
        work_schedule_id=work_schedule_id,
        has_overtime=has_overtime,
        include_deleted=include_deleted,
        only_manual=only_manual,
        geo_fallback=geo_fallback,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )
    identities = await shift_service.build_org_shift_identities(session, org_id, shifts)
    actor_names = await shift_service.build_manual_actor_names(session, shifts)
    summaries = await checklist_instance_service.get_checklists_summary_for_shifts(
        session, [s.id for s in shifts]
    )
    overtime_map = await overtime_service.get_latest_overtime_for_shifts(
        session, [s.id for s in shifts]
    )
    return ApiResponse.success(
        ShiftListResponse(
            items=[
                _shift_to_response(
                    s,
                    identities.get(s.user_id),
                    summaries.get(s.id, checklist_instance_service.ZERO_SHIFT_CHECKLISTS_SUMMARY),
                    late_tolerance_minutes=late_tolerance,
                    overtime=overtime_map.get(s.id),
                    organization_timezone=org.timezone,
                    created_by_name=(
                        actor_names.get(s.created_by_user_id) if s.created_by_user_id else None
                    ),
                    edited_by_name=(
                        actor_names.get(s.edited_by_user_id) if s.edited_by_user_id else None
                    ),
                )
                for s in shifts
            ],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/{org_id}/shifts/{shift_id}",
    response_model=ApiResponse[ShiftResponse],
    summary="Деталь смены сотрудника",
    description=(
        "Деталь конкретной смены сотрудника организации для обзора владельцем "
        "(Owner) или админом. Делает карточку списка кликабельной. Содержит имя/"
        "почту/роль сотрудника и полный список пауз. Чек-листы смены — отдельным "
        "запросом `GET /shifts/{shift_id}/checklists`. Удалённую смену (soft-delete) "
        "отдаёт только с `include_deleted=true` (manual_time_entry, A5) — без "
        "параметра `404 SHIFT_NOT_FOUND`, как для отсутствующей."
    ),
)
async def get_org_shift(
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    include_deleted: bool = Query(
        False,
        description="Отдать деталь и для удалённой смены (soft-delete). "
        "Без параметра удалённая смена трактуется как отсутствующая "
        "(404 SHIFT_NOT_FOUND). Только owner/admin (manual_time_entry)",
    ),
) -> ApiResponse:
    from src.app.services.work_location import _check_admin_or_owner

    org = await org_service.get_organization(session, org_id)
    await _check_admin_or_owner(session, org, user.id)

    shift = await shift_service.get_org_shift_detail(
        session, org_id, shift_id, include_deleted=include_deleted
    )
    identities = await shift_service.build_org_shift_identities(session, org_id, [shift])
    actor_names = await shift_service.build_manual_actor_names(session, [shift])
    summaries = await checklist_instance_service.get_checklists_summary_for_shifts(
        session, [shift.id]
    )
    overtime_map = await overtime_service.get_latest_overtime_for_shifts(session, [shift.id])
    late_tolerance = org.settings.late_tolerance_minutes if org.settings is not None else 0
    return ApiResponse.success(
        _shift_to_response(
            shift,
            identities.get(shift.user_id),
            summaries.get(shift.id, checklist_instance_service.ZERO_SHIFT_CHECKLISTS_SUMMARY),
            late_tolerance_minutes=late_tolerance,
            overtime=overtime_map.get(shift.id),
            organization_timezone=org.timezone,
            created_by_name=(
                actor_names.get(shift.created_by_user_id) if shift.created_by_user_id else None
            ),
            edited_by_name=(
                actor_names.get(shift.edited_by_user_id) if shift.edited_by_user_id else None
            ),
        )
    )


@router.get(
    "/{org_id}/checklist-instances",
    tags=["checklist-instances"],
    summary="Реестр экземпляров чек-листов организации",
    description=(
        "Плоский список всех экземпляров чек-листов смен организации "
        "(персональные смены и is_deleted=true исключены) с фильтрами по "
        "сотруднику/шаблону/типу/статусу/точке/периоду, сортировкой и "
        "пагинацией. Доступно владельцу (Owner) и админам (checklist_reports)."
    ),
)
async def list_org_checklist_instances(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    user_id: uuid.UUID | None = Query(None, description="Фильтр по UUID сотрудника"),
    template_id: uuid.UUID | None = Query(None, description="Фильтр по UUID шаблона"),
    checklist_type: str | None = Query(
        None,
        alias="type",
        description="Фильтр по типу: shift_start, shift_end",
    ),
    status: str | None = Query(
        None, description="Фильтр по статусу: pending, completed, incomplete"
    ),
    state: str | None = Query(
        None,
        description="Агрегированный фильтр: completed, not_completed. "
        "Игнорируется, если передан status",
    ),
    is_required: bool | None = Query(None, description="Только обязательные / необязательные"),
    work_location_id: uuid.UUID | None = Query(None, description="Фильтр по точке смены"),
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница по началу смены, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница по началу смены, включительно (UTC)"
    ),
    limit: int = Query(20, ge=1, le=100, description="Размер страницы (1–100)"),
    offset: int = Query(0, ge=0, description="Смещение для пагинации"),
    sort: str = Query(
        "shift_started_at",
        description="Поле сортировки: shift_started_at, completed_at, created_at",
    ),
    order: str = Query("desc", description="Направление: asc или desc"),
) -> ApiResponse:
    rows, total = await checklist_instance_service.list_org_checklist_instances(
        session,
        org_id,
        user.id,
        user_id=user_id,
        template_id=template_id,
        type_=checklist_type,
        status=status,
        state=state,
        is_required=is_required,
        work_location_id=work_location_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )
    return ApiResponse.success(
        OrgChecklistInstanceListResponse(
            items=[_org_instance_row_to_response(r) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/{org_id}/stats",
    summary="Статистика организации",
    description=(
        "Агрегированная статистика по организации с разбивкой по каждому "
        "сотруднику. Окно — пресет `period` (день/неделя/месяц) ЛИБО "
        "произвольный диапазон `date_from`/`date_to` (UTC, включительно по "
        "началу смены); источники окна взаимоисключающи. Доступно владельцу "
        "(Owner) и админам."
    ),
)
async def org_stats(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    period: str | None = Query(
        None, description="Пресет окна: day, week, month. Взаимоисключающ с date_from/date_to"
    ),
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница окна по started_at, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница окна по started_at, включительно (UTC)"
    ),
) -> ApiResponse:
    from src.app.services.work_location import _check_admin_or_owner

    org = await org_service.get_organization(session, org_id)
    await _check_admin_or_owner(session, org, user.id)

    stats = await shift_service.get_org_stats(
        session,
        org_id,
        period,
        date_from=date_from,
        date_to=date_to,
    )
    return ApiResponse.success(OrgStatsResponse(**stats).model_dump(mode="json"))


def _audit_to_entry(entry: "AuditLog", names: dict[uuid.UUID, str]) -> dict[str, Any]:
    if entry.actor_user_id is None:
        actor_name = "Система"
    else:
        actor_name = names.get(entry.actor_user_id, "Неизвестно")
    return AuditLogEntry(
        id=str(entry.id),
        organization_id=str(entry.organization_id) if entry.organization_id else None,
        actor_user_id=str(entry.actor_user_id) if entry.actor_user_id else None,
        actor_name=actor_name,
        action=entry.action,
        resource_type=entry.resource_type,
        resource_id=str(entry.resource_id) if entry.resource_id else None,
        summary=entry.summary,
        ip_address=entry.ip_address,
        created_at=entry.created_at,
    ).model_dump(mode="json")


@router.get(
    "/{org_id}/audit-logs",
    summary="Лента аудита организации",
    description=(
        "Журнал чувствительных действий owner/admin и системных авто-действий "
        "(Celery). Append-only, только чтение. Доступно владельцу (Owner) и "
        "админам. Сортировка по created_at DESC. `date_to` включительно (UTC)."
    ),
)
async def list_org_audit_logs(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    action: AuditAction | None = Query(None, description="Фильтр по коду действия"),
    actor_user_id: uuid.UUID | None = Query(None, description="Фильтр по UUID инициатора"),
    date_from: dt_datetime | None = Query(None, description="Нижняя граница по created_at (UTC)"),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница по created_at, включительно (UTC)"
    ),
    limit: int = Query(50, ge=1, le=200, description="Размер страницы (1–200)"),
    offset: int = Query(0, ge=0, description="Смещение для пагинации"),
) -> ApiResponse:
    from src.app.services.common import ensure_admin_or_owner

    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id, message="Нет прав на просмотр аудита")
    shift_service.validate_date_range(date_from, date_to)

    items, total, names = await audit_service.list_audit_logs(
        session,
        org_id,
        limit=limit,
        offset=offset,
        action=action,
        actor_user_id=actor_user_id,
        date_from=date_from,
        date_to=date_to,
    )
    return ApiResponse.success(
        AuditLogListResponse(
            items=[_audit_to_entry(it, names) for it in items],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )
