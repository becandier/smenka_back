import uuid

from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.work_location import (
    WorkLocationCreate,
    WorkLocationListResponse,
    WorkLocationResponse,
    WorkLocationUpdate,
)
from src.app.services import work_location as wl_service

router = APIRouter(
    prefix="/organizations/{org_id}/locations",
    tags=["work-locations"],
)


def _location_to_response(loc) -> dict:
    return WorkLocationResponse(
        id=str(loc.id),
        organization_id=str(loc.organization_id),
        name=loc.name,
        latitude=loc.latitude,
        longitude=loc.longitude,
        radius_meters=loc.radius_meters,
        created_at=loc.created_at,
    ).model_dump(mode="json")


@router.post("", status_code=201)
async def create_location(
    org_id: uuid.UUID,
    body: WorkLocationCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    location = await wl_service.create_work_location(
        session, org_id, user.id,
        name=body.name,
        latitude=body.latitude,
        longitude=body.longitude,
        radius_meters=body.radius_meters,
    )
    await session.commit()
    return ApiResponse.success(_location_to_response(location))


@router.get("")
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


@router.patch("/{location_id}")
async def update_location(
    org_id: uuid.UUID,
    location_id: uuid.UUID,
    body: WorkLocationUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    fields = body.model_dump(exclude_unset=True)
    location = await wl_service.update_work_location(
        session, org_id, location_id, user.id, **fields,
    )
    await session.commit()
    return ApiResponse.success(_location_to_response(location))


@router.delete("/{location_id}")
async def delete_location(
    org_id: uuid.UUID,
    location_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await wl_service.delete_work_location(session, org_id, location_id, user.id)
    await session.commit()
    return ApiResponse.success({"message": "Точка удалена"})
