import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.schemas.base import ApiResponse
from src.app.schemas.work_location import (
    WorkLocationCreate,
    WorkLocationListResponse,
    WorkLocationResponse,
    WorkLocationUpdate,
)
from src.app.services import audit as audit_service
from src.app.services import work_location as wl_service
from src.app.utils.request import get_client_ip

if TYPE_CHECKING:
    from src.app.models.work_location import WorkLocation

router = APIRouter(
    prefix="/organizations/{org_id}/locations",
    tags=["work-locations"],
)


def _location_to_response(loc: "WorkLocation") -> dict[str, Any]:
    return WorkLocationResponse(
        id=str(loc.id),
        organization_id=str(loc.organization_id),
        name=loc.name,
        latitude=loc.latitude,
        longitude=loc.longitude,
        radius_meters=loc.radius_meters,
        address=loc.address,
        created_at=loc.created_at,
    ).model_dump(mode="json")


@router.post(
    "",
    status_code=201,
    summary="Создать рабочую точку",
    description=(
        "Создаёт рабочую точку (геозону) для организации. При включённой "
        "геопроверке сотрудники смогут начать смену только внутри радиуса "
        "одной из точек. Доступно владельцу и админам."
    ),
)
async def create_location(
    org_id: uuid.UUID,
    body: WorkLocationCreate,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    location = await wl_service.create_work_location(
        session,
        org_id,
        user.id,
        name=body.name,
        latitude=body.latitude,
        longitude=body.longitude,
        radius_meters=body.radius_meters,
        address=body.address,
    )
    await audit_service.record(
        session,
        action=AuditAction.location_create,
        resource_type=AuditResource.location,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=location.id,
        summary={
            "name": body.name,
            "latitude": body.latitude,
            "longitude": body.longitude,
            "radius_meters": body.radius_meters,
            "address": body.address,
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(_location_to_response(location))


@router.get(
    "",
    summary="Список рабочих точек",
    description="Все рабочие точки организации. Доступно владельцу и участникам.",
)
async def list_locations(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    locations = await wl_service.get_work_locations(session, org_id, user.id)
    return ApiResponse.success(
        WorkLocationListResponse(
            items=[_location_to_response(loc) for loc in locations],
        ).model_dump(mode="json")
    )


@router.patch(
    "/{location_id}",
    summary="Обновить рабочую точку",
    description=(
        "Обновляет параметры рабочей точки. Передавайте только поля, которые "
        "нужно изменить. Доступно владельцу и админам."
    ),
)
async def update_location(
    org_id: uuid.UUID,
    location_id: uuid.UUID,
    body: WorkLocationUpdate,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    fields = body.model_dump(exclude_unset=True)
    location = await wl_service.update_work_location(
        session,
        org_id,
        location_id,
        user.id,
        **fields,
    )
    await audit_service.record(
        session,
        action=AuditAction.location_update,
        resource_type=AuditResource.location,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=location_id,
        summary=fields,
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(_location_to_response(location))


@router.delete(
    "/{location_id}",
    summary="Удалить рабочую точку",
    description="Удаляет рабочую точку. Доступно владельцу и админам.",
)
async def delete_location(
    org_id: uuid.UUID,
    location_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    await wl_service.delete_work_location(session, org_id, location_id, user.id)
    await audit_service.record(
        session,
        action=AuditAction.location_delete,
        resource_type=AuditResource.location,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=location_id,
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success({"message": "Точка удалена"})
