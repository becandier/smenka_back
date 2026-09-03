"""`GET /organizations/{org_id}/work-locations/nearby` (shift_start_location_choice).

Отдельный файл/роутер (а не эндпоинт в `work_locations.py`): контракт фичи
фиксирует путь `.../work-locations/nearby`, отличный от сегмента CRUD-эндпоинтов
точек (`.../locations`) в том файле — один роутер на файл, см. AGENTS.md.
"""

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.work_location import (
    WorkLocationNearbyItem,
    WorkLocationNearbyResponse,
    WorkLocationNearestOutside,
)
from src.app.services import work_location as wl_service

if TYPE_CHECKING:
    from src.app.services.work_location import LocationDistance

router = APIRouter(
    prefix="/organizations/{org_id}/work-locations",
    tags=["work-locations"],
)


def _nearby_item_to_response(pair: "LocationDistance", *, is_nearest: bool) -> dict[str, Any]:
    return WorkLocationNearbyItem(
        id=str(pair.location.id),
        name=pair.location.name,
        address=pair.location.address,
        latitude=pair.location.latitude,
        longitude=pair.location.longitude,
        radius_meters=pair.location.radius_meters,
        distance_meters=int(pair.distance_meters),
        is_nearest=is_nearest,
    ).model_dump(mode="json")


def _nearest_outside_to_response(pair: "LocationDistance") -> dict[str, Any]:
    return WorkLocationNearestOutside(
        id=str(pair.location.id),
        name=pair.location.name,
        address=pair.location.address,
        distance_meters=int(pair.distance_meters),
        radius_meters=pair.location.radius_meters,
    ).model_dump(mode="json")


@router.get(
    "/nearby",
    response_model=ApiResponse[WorkLocationNearbyResponse],
    summary="Точки организации рядом с координатами",
    description=(
        "Точки, в чей радиус попадают переданные координаты, отсортированные по "
        "возрастанию расстояния (`is_nearest=true` у первой). Пустой `items` — "
        "штатный случай «сотрудник вне всех зон», не ошибка. `nearest_outside` — "
        "ближайшая точка организации ВНЕ радиуса, если такая есть. Тот же расчёт "
        "попадания в радиус, что и при `POST /shifts/start`, — список не может "
        "разойтись с тем, что примет старт смены. Доступно владельцу, admin и "
        "участникам организации."
    ),
)
async def get_nearby_locations(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    latitude: float = Query(ge=-90, le=90, description="Широта"),
    longitude: float = Query(ge=-180, le=180, description="Долгота"),
) -> ApiResponse:
    matched, nearest_outside = await wl_service.get_nearby_work_locations(
        session, org_id, user.id, latitude, longitude
    )
    return ApiResponse.success(
        WorkLocationNearbyResponse(
            items=[
                _nearby_item_to_response(pair, is_nearest=index == 0)
                for index, pair in enumerate(matched)
            ],
            nearest_outside=(
                _nearest_outside_to_response(nearest_outside)
                if nearest_outside is not None
                else None
            ),
        ).model_dump(mode="json")
    )
