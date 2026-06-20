import uuid
from datetime import datetime as dt_datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, Request

from src.app.api.deps import CurrentUserDep, SessionDep, SuperAdminDep
from src.app.api.v1.shifts import _shift_to_response
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.models.user import UserRole
from src.app.schemas.audit import AuditLogEntry, AuditLogListResponse
from src.app.schemas.base import ApiResponse
from src.app.schemas.organization import (
    InviteCodeResponse,
    JoinResponse,
    MemberListResponse,
    MemberResponse,
    MemberRoleUpdate,
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
from src.app.schemas.shift import ShiftListResponse
from src.app.services import audit as audit_service
from src.app.services import organization as org_service
from src.app.services import organization_settings as settings_service
from src.app.services import shift as shift_service
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
) -> dict[str, Any]:
    geo_check = org.settings.geo_check_enabled if org.settings else False
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
        geo_check_enabled=geo_check,
        created_at=org.created_at,
        my_role=my_role,
        my_custom_role=custom_role_payload,
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
        user_email=member.user.email,
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
    return ApiResponse.success(_org_to_response(org, my_role, my_custom_role))


@router.patch(
    "/{org_id}",
    summary="Обновить организацию",
    description="Обновляет название организации. Только для владельца (Owner).",
)
async def update_organization(
    org_id: uuid.UUID,
    body: OrganizationUpdate,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    org = await org_service.update_organization(session, org_id, user.id, body.name)
    await audit_service.record(
        session,
        action=AuditAction.org_update,
        resource_type=AuditResource.organization,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=org_id,
        summary={"name": body.name},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(_org_to_response(org, "owner", None))


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
    return ApiResponse.success(
        JoinResponse(
            organization_id=str(org.id),
            organization_name=org.name,
            role=member.role.value,
            geo_check_enabled=geo_check,
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


@router.patch(
    "/{org_id}/members/{member_user_id}/role",
    summary="Изменить роль участника",
    description=(
        "Назначает или снимает роль admin у участника. Доступно владельцу (Owner) и super_admin."
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

    # Эндпоинт доступен только owner/super_admin — ставку показываем всегда
    from src.app.services import payroll as payroll_service

    current_rates = await payroll_service.get_current_rates(session, [member.id])
    return ApiResponse.success(_member_to_response(member, current_rates.get(member.id)))


def _settings_to_response(s: "OrganizationSettings") -> dict[str, Any]:
    return OrganizationSettingsResponse(
        organization_id=str(s.organization_id),
        geo_check_enabled=s.geo_check_enabled,
        auto_finish_hours=s.auto_finish_hours,
        max_pause_minutes=s.max_pause_minutes,
        max_pauses_per_shift=s.max_pauses_per_shift,
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

    shifts, total = await shift_service.get_org_shifts(
        session,
        org_id,
        user_id=user_id,
        status=status_enum,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )
    identities = await shift_service.build_org_shift_identities(session, org_id, shifts)
    return ApiResponse.success(
        ShiftListResponse(
            items=[_shift_to_response(s, identities.get(s.user_id)) for s in shifts],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/{org_id}/shifts/{shift_id}",
    summary="Деталь смены сотрудника",
    description=(
        "Деталь конкретной смены сотрудника организации для обзора владельцем "
        "(Owner) или админом. Делает карточку списка кликабельной. Содержит имя/"
        "почту/роль сотрудника и полный список пауз. Чек-листы смены — отдельным "
        "запросом `GET /shifts/{shift_id}/checklists`."
    ),
)
async def get_org_shift(
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    from src.app.services.work_location import _check_admin_or_owner

    org = await org_service.get_organization(session, org_id)
    await _check_admin_or_owner(session, org, user.id)

    shift = await shift_service.get_org_shift_detail(session, org_id, shift_id)
    identities = await shift_service.build_org_shift_identities(session, org_id, [shift])
    return ApiResponse.success(_shift_to_response(shift, identities.get(shift.user_id)))


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
