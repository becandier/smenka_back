import uuid

from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.organization_role import (
    MemberRoleAssignRequest,
    RoleCreate,
    RoleListResponse,
    RoleResponse,
    RoleUpdate,
)
from src.app.services import organization_role as role_service

router = APIRouter(
    prefix="/organizations/{org_id}",
    tags=["organization-roles"],
)


def _role_to_response(role) -> dict:
    return RoleResponse(
        id=str(role.id),
        name=role.name,
        created_at=role.created_at,
    ).model_dump(mode="json")


@router.post(
    "/roles",
    status_code=201,
    summary="Создать кастомную роль",
    description="Создаёт кастомную роль в организации (например: бариста, кассир). Доступно владельцу и админам.",
)
async def create_role(
    org_id: uuid.UUID,
    body: RoleCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    role = await role_service.create_role(session, org_id, body.name, user.id)
    await session.commit()
    return ApiResponse.success(_role_to_response(role))


@router.get(
    "/roles",
    summary="Список кастомных ролей",
    description="Все кастомные роли организации. Доступно всем участникам и владельцу.",
)
async def list_roles(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    roles = await role_service.get_roles(session, org_id, user.id)
    return ApiResponse.success(
        RoleListResponse(
            items=[_role_to_response(r) for r in roles],
        ).model_dump(mode="json")
    )


@router.patch(
    "/roles/{role_id}",
    summary="Переименовать роль",
    description="Меняет название кастомной роли. Доступно владельцу и админам.",
)
async def update_role(
    org_id: uuid.UUID,
    role_id: uuid.UUID,
    body: RoleUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    role = await role_service.update_role(
        session, org_id, role_id, body.name, user.id,
    )
    await session.commit()
    return ApiResponse.success(_role_to_response(role))


@router.delete(
    "/roles/{role_id}",
    summary="Удалить роль",
    description="Удаляет кастомную роль. У всех участников с этой ролью role_id обнуляется. Доступно владельцу и админам.",
)
async def delete_role(
    org_id: uuid.UUID,
    role_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await role_service.delete_role(session, org_id, role_id, user.id)
    await session.commit()
    return ApiResponse.success({"message": "Роль удалена"})


@router.patch(
    "/members/{user_id}/custom-role",
    summary="Назначить кастомную роль участнику",
    description="Назначает или снимает кастомную роль у участника. role_id=null — снять роль. Доступно владельцу и админам. Системная роль (admin/employee) меняется отдельным эндпоинтом.",
)
async def assign_member_role(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberRoleAssignRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    role_uuid = uuid.UUID(body.role_id) if body.role_id else None
    member = await role_service.assign_role_to_member(
        session, org_id, user_id, role_uuid, user.id,
    )
    await session.commit()
    from src.app.api.v1.organizations import _member_to_response
    return ApiResponse.success(_member_to_response(member))
