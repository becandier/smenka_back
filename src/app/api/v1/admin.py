import uuid
from typing import Any

from fastapi import APIRouter, Query

from src.app.api.deps import SessionDep, SuperAdminDep
from src.app.models.oauth import OAuthProviderSetting
from src.app.models.user import User
from src.app.schemas.admin import (
    AdminOrganizationListResponse,
    AdminOrganizationResponse,
    AdminStatsResponse,
    AdminUserDetailResponse,
    AdminUserListResponse,
    AdminUserResponse,
    UpdateUserRoleRequest,
)
from src.app.schemas.base import ApiResponse
from src.app.schemas.oauth import (
    OAuthProviderSettingListResponse,
    OAuthProviderSettingResponse,
    UpsertOAuthProviderRequest,
)
from src.app.services import admin as admin_service
from src.app.services import oauth_provider_settings as oauth_provider_settings_service

router = APIRouter(prefix="/admin", tags=["admin"])


def _user_to_response(user: User) -> dict[str, Any]:
    return AdminUserResponse(
        id=str(user.id),
        email=user.email,
        name=user.name,
        phone=user.phone,
        is_verified=user.is_verified,
        role=user.role.value,
        created_at=user.created_at,
    ).model_dump(mode="json")


def _oauth_setting_to_response(setting: OAuthProviderSetting | dict[str, Any]) -> dict[str, Any]:
    if isinstance(setting, OAuthProviderSetting):
        return OAuthProviderSettingResponse(
            provider=setting.provider,
            client_type=setting.client_type,
            client_id=setting.client_id,
            enabled=setting.enabled,
            updated_by=str(setting.updated_by) if setting.updated_by else None,
            updated_at=setting.updated_at,
        ).model_dump(mode="json")
    return OAuthProviderSettingResponse(**setting).model_dump(mode="json")


@router.get(
    "/users",
    summary="Список пользователей (super_admin)",
    description="Все пользователи платформы с поиском (email/имя), фильтрами role и "
    "is_verified, сортировкой и пагинацией.",
)
async def list_users(
    user: SuperAdminDep,
    session: SessionDep,
    search: str | None = Query(None, description="Поиск по email или имени"),
    role: str | None = Query(None, description="Фильтр по роли: super_admin, user"),
    is_verified: bool | None = Query(None, description="Фильтр по верификации"),
    limit: int = Query(20, ge=1, le=100, description="Размер страницы (1–100)"),
    offset: int = Query(0, ge=0, description="Смещение для пагинации"),
    sort: str = Query("created_at", description="Поле сортировки: created_at, email"),
    order: str = Query("desc", description="Направление: asc или desc"),
) -> ApiResponse:
    users, total = await admin_service.list_users(
        session,
        search=search,
        role=role,
        is_verified=is_verified,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )
    return ApiResponse.success(
        AdminUserListResponse(
            items=[_user_to_response(u) for u in users],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/users/{user_id}",
    summary="Детали пользователя (super_admin)",
    description="Профиль пользователя с агрегатами: число организаций во владении, "
    "число членств, число смен.",
)
async def get_user(
    user_id: uuid.UUID,
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    target, owned, member, shifts = await admin_service.get_user_detail(session, user_id)
    return ApiResponse.success(
        AdminUserDetailResponse(
            id=str(target.id),
            email=target.email,
            name=target.name,
            phone=target.phone,
            is_verified=target.is_verified,
            role=target.role.value,
            created_at=target.created_at,
            owned_organizations_count=owned,
            member_organizations_count=member,
            shifts_count=shifts,
        ).model_dump(mode="json")
    )


@router.patch(
    "/users/{user_id}/role",
    summary="Сменить роль пользователя (super_admin)",
    description="Меняет глобальную роль (super_admin/user). Нельзя снять super_admin "
    "с самого себя.",
)
async def update_user_role(
    user_id: uuid.UUID,
    body: UpdateUserRoleRequest,
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    updated = await admin_service.update_user_role(session, user_id, body.role, user.id)
    await session.commit()
    return ApiResponse.success(_user_to_response(updated))


@router.get(
    "/organizations",
    summary="Обзор организаций (super_admin)",
    description="Все организации платформы с email владельца и числом участников. "
    "Фильтры is_deleted и search, сортировка, пагинация.",
)
async def list_organizations(
    user: SuperAdminDep,
    session: SessionDep,
    search: str | None = Query(None, description="Поиск по названию"),
    is_deleted: bool | None = Query(None, description="Фильтр по флагу удаления"),
    limit: int = Query(20, ge=1, le=100, description="Размер страницы (1–100)"),
    offset: int = Query(0, ge=0, description="Смещение для пагинации"),
    sort: str = Query("created_at", description="Поле сортировки: created_at, name"),
    order: str = Query("desc", description="Направление: asc или desc"),
) -> ApiResponse:
    rows, total = await admin_service.list_organizations(
        session,
        search=search,
        is_deleted=is_deleted,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )
    items = [
        AdminOrganizationResponse(
            id=str(org.id),
            name=org.name,
            owner_id=str(org.owner_id),
            owner_email=owner_email,
            member_count=member_count,
            is_deleted=org.is_deleted,
            created_at=org.created_at,
        ).model_dump(mode="json")
        for org, owner_email, member_count in rows
    ]
    return ApiResponse.success(
        AdminOrganizationListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/stats",
    summary="Сводная статистика платформы (super_admin)",
    description="Агрегаты для дашборда: пользователи, организации, активные смены, "
    "смены за сегодня и неделю.",
)
async def stats(
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    data = await admin_service.get_stats(session)
    return ApiResponse.success(AdminStatsResponse(**data).model_dump())


@router.get(
    "/oauth-providers",
    summary="Настройки OAuth-провайдеров (super_admin)",
    description="Все 5 допустимых комбинаций provider/client_type. Ненастроенные "
    "отдаются заглушкой (client_id=null, enabled=false).",
)
async def list_oauth_providers(
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    settings = await oauth_provider_settings_service.list_provider_settings(session)
    return ApiResponse.success(
        OAuthProviderSettingListResponse(
            items=[_oauth_setting_to_response(s) for s in settings]
        ).model_dump(mode="json")
    )


@router.put(
    "/oauth-providers/{provider}/{client_type}",
    summary="Upsert настройки OAuth-провайдера (super_admin)",
    description="Создаёт/обновляет client_id и enabled для комбинации provider/"
    "client_type. Недопустимая комбинация → 422 VALIDATION_ERROR.",
)
async def upsert_oauth_provider(
    provider: str,
    client_type: str,
    body: UpsertOAuthProviderRequest,
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    setting = await oauth_provider_settings_service.upsert_provider_setting(
        session,
        provider=provider,
        client_type=client_type,
        client_id=body.client_id,
        enabled=body.enabled,
        updated_by_id=user.id,
    )
    await session.commit()
    return ApiResponse.success(_oauth_setting_to_response(setting))
