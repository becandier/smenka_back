"""Профили доступа организации и каталог разделов кабинета (access_profiles, ADR-006)."""

import uuid
from typing import Any

from fastapi import APIRouter, Query, Request

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.core.access_catalog import ACCESS_SECTIONS
from src.app.models.access_profile import OrganizationAccessProfile
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.schemas.access_profile import (
    AccessCatalogResponse,
    AccessProfileCreate,
    AccessProfileListResponse,
    AccessProfileResponse,
    AccessProfileUpdate,
    AccessSectionResponse,
)
from src.app.schemas.base import ApiResponse
from src.app.services import access_profile as profile_service
from src.app.services import audit as audit_service
from src.app.utils.request import get_client_ip

router = APIRouter(tags=["access-profiles"])


def _profile_to_response(profile: OrganizationAccessProfile, members_count: int) -> dict[str, Any]:
    return AccessProfileResponse(
        id=str(profile.id),
        organization_id=str(profile.organization_id),
        name=profile.name,
        description=profile.description,
        permissions=dict(profile.permissions),
        members_count=members_count,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        is_deleted=profile.is_deleted,
        deleted_at=profile.deleted_at,
    ).model_dump(mode="json")


@router.get(
    "/access-permissions",
    summary="Каталог разделов кабинета",
    description=(
        "Разделы веб-кабинета организации и допустимые уровни доступа (view/manage) "
        "для конструктора профилей. Порядок — порядок построения конструктора; "
        "русские названия — на клиенте по `code`. Любой аутентифицированный."
    ),
)
async def get_access_permissions(user: CurrentUserDep) -> ApiResponse:
    return ApiResponse.success(
        AccessCatalogResponse(
            sections=[
                AccessSectionResponse(code=s.code, group=s.group, levels=list(s.levels))
                for s in ACCESS_SECTIONS
            ]
        ).model_dump(mode="json")
    )


@router.get(
    "/organizations/{org_id}/access-profiles",
    summary="Профили доступа организации",
    description=(
        "Список профилей доступа, по названию. Полный администратор или обладатель "
        "`members:view`. `include_deleted=true` — вместе с удалёнными."
    ),
)
async def list_access_profiles(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    include_deleted: bool = Query(False, description="Включить удалённые профили"),
) -> ApiResponse:
    profiles, counts = await profile_service.list_profiles(
        session, org_id, user.id, include_deleted=include_deleted
    )
    return ApiResponse.success(
        AccessProfileListResponse(
            items=[_profile_to_response(p, counts.get(p.id, 0)) for p in profiles],
        ).model_dump(mode="json")
    )


@router.post(
    "/organizations/{org_id}/access-profiles",
    status_code=201,
    summary="Создать профиль доступа",
    description=(
        "Создаёт именованный набор прав по разделам кабинета. Только полный "
        "администратор. Название уникально без учёта регистра среди активных "
        "профилей организации (409 ACCESS_PROFILE_NAME_TAKEN)."
    ),
)
async def create_access_profile(
    org_id: uuid.UUID,
    body: AccessProfileCreate,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    profile = await profile_service.create_profile(
        session,
        org_id,
        user.id,
        name=body.name,
        description=body.description,
        permissions=body.permissions,
    )
    await audit_service.record(
        session,
        action=AuditAction.access_profile_create,
        resource_type=AuditResource.access_profile,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=profile.id,
        summary={"name": profile.name, "permissions": dict(profile.permissions)},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(_profile_to_response(profile, 0))


@router.get(
    "/organizations/{org_id}/access-profiles/{profile_id}",
    summary="Профиль доступа",
    description="Полный администратор или обладатель `members:view`.",
)
async def get_access_profile(
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    profile, members_count = await profile_service.get_profile_detail(
        session, org_id, profile_id, user.id
    )
    return ApiResponse.success(_profile_to_response(profile, members_count))


@router.patch(
    "/organizations/{org_id}/access-profiles/{profile_id}",
    summary="Изменить профиль доступа",
    description=(
        "Partial-обновление: name, description, permissions (заменяется целиком). "
        "Изменение прав действует немедленно для всех обладателей профиля. Только "
        "полный администратор."
    ),
)
async def update_access_profile(
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    body: AccessProfileUpdate,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    fields: dict[str, object] = {}
    for key in body.model_fields_set:
        value = getattr(body, key)
        # name/permissions = null трактуются как «не менять»; description = null — очистить.
        if value is None and key != "description":
            continue
        fields[key] = value
    profile, changed = await profile_service.update_profile(
        session, org_id, profile_id, user.id, fields=fields
    )
    if changed:
        summary: dict[str, Any] = {"changed_fields": changed}
        if "name" in changed:
            summary["name"] = profile.name
        if "permissions" in changed:
            summary["permissions"] = dict(profile.permissions)
        await audit_service.record(
            session,
            action=AuditAction.access_profile_update,
            resource_type=AuditResource.access_profile,
            organization_id=org_id,
            actor_user_id=user.id,
            resource_id=profile.id,
            summary=summary,
            ip_address=get_client_ip(request),
        )
    await session.commit()
    counts = await profile_service.count_members(session, [profile.id])
    return ApiResponse.success(_profile_to_response(profile, counts.get(profile.id, 0)))


@router.delete(
    "/organizations/{org_id}/access-profiles/{profile_id}",
    summary="Удалить профиль доступа",
    description=(
        "Soft-delete профиля; у всех его обладателей профиль снимается в той же "
        "транзакции (доступ в кабинет пропадает). Повторное удаление — 404. Только "
        "полный администратор."
    ),
)
async def delete_access_profile(
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    profile, detached = await profile_service.delete_profile(session, org_id, profile_id, user.id)
    await audit_service.record(
        session,
        action=AuditAction.access_profile_delete,
        resource_type=AuditResource.access_profile,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=profile.id,
        summary={"name": profile.name, "detached_members": detached},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success({"message": "Профиль доступа удалён", "detached_members": detached})


@router.post(
    "/organizations/{org_id}/access-profiles/{profile_id}/restore",
    summary="Восстановить профиль доступа",
    description=(
        "Восстанавливает удалённый профиль (участникам он не возвращается). "
        "Активный профиль → 409 ACCESS_PROFILE_NOT_DELETED; название занято "
        "активным профилем → 409 ACCESS_PROFILE_NAME_TAKEN. Только полный администратор."
    ),
)
async def restore_access_profile(
    org_id: uuid.UUID,
    profile_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    profile = await profile_service.restore_profile(session, org_id, profile_id, user.id)
    await audit_service.record(
        session,
        action=AuditAction.access_profile_restore,
        resource_type=AuditResource.access_profile,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=profile.id,
        summary={"name": profile.name},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(_profile_to_response(profile, 0))
