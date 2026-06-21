# src/app/api/v1/shifts.py
import uuid
from datetime import datetime as dt_datetime
from typing import Any

from fastapi import APIRouter, Query, Request

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.models.shift import Shift
from src.app.schemas.base import ApiResponse
from src.app.schemas.shift import (
    ShiftListResponse,
    ShiftResponse,
    ShiftStartRequest,
    ShiftStatsResponse,
)
from src.app.services import audit as audit_service
from src.app.services import shift as shift_service
from src.app.services.shift import ShiftIdentity, calculate_worked_seconds
from src.app.utils.request import get_client_ip

router = APIRouter(prefix="/shifts", tags=["shifts"])


def _shift_to_response(
    shift: Shift,
    identity: ShiftIdentity | None = None,
) -> dict[str, Any]:
    """Сериализовать смену.

    Без `identity` (персональный контекст) additive-поля сотрудника остаются
    `null`. В орг-контексте `identity` наполняет `user_name` / `user_email` /
    `role` / `custom_role_name`.
    """
    work_location = getattr(shift, "work_location", None)
    return ShiftResponse(
        id=str(shift.id),
        user_id=str(shift.user_id),
        organization_id=str(shift.organization_id) if shift.organization_id else None,
        started_at=shift.started_at,
        finished_at=shift.finished_at,
        status=shift.status.value,
        work_location_id=str(shift.work_location_id) if shift.work_location_id else None,
        work_location=(
            {
                "id": str(work_location.id),
                "name": work_location.name,
                "address": work_location.address,
            }
            if work_location is not None
            else None
        ),
        pauses=[
            {
                "id": str(p.id),
                "shift_id": str(p.shift_id),
                "started_at": p.started_at,
                "finished_at": p.finished_at,
            }
            for p in shift.pauses
        ],
        worked_seconds=calculate_worked_seconds(shift),
        has_incomplete_required_checklists=bool(
            getattr(shift, "has_incomplete_required_checklists", False)
        ),
        user_name=identity.user_name if identity is not None else None,
        user_email=identity.user_email if identity is not None else None,
        role=identity.role if identity is not None else None,
        custom_role_name=identity.custom_role_name if identity is not None else None,
    ).model_dump(mode="json")


@router.get(
    "",
    summary="История смен",
    description="Список персональных смен текущего пользователя с пагинацией. "
    "Поддерживает фильтрацию по статусу и дате.",
)
async def list_shifts(
    user: CurrentUserDep,
    session: SessionDep,
    status: str | None = Query(None, description="Filter by status: active, paused, finished"),
    date_from: dt_datetime | None = Query(
        None, description="Filter shifts started after this datetime"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Filter shifts started before this datetime"
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    sort: str = Query("started_at", description="Поле сортировки: started_at, finished_at"),
    order: str = Query("desc", description="Направление: asc или desc"),
) -> ApiResponse:
    from src.app.models.shift import ShiftStatus
    from src.app.services.shift import ShiftError

    status_enum = None
    if status is not None:
        try:
            status_enum = ShiftStatus(status)
        except ValueError:
            raise ShiftError(
                "INVALID_STATUS",
                f"Статус должен быть: {', '.join(s.value for s in ShiftStatus)}",
                400,
            ) from None

    shift_service.validate_date_range(date_from, date_to)

    shifts, total = await shift_service.get_shifts(
        session,
        user.id,
        status=status_enum,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )
    await session.commit()
    return ApiResponse.success(
        ShiftListResponse(
            items=[_shift_to_response(s) for s in shifts],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/stats",
    summary="Статистика смен",
    description="Агрегированная статистика персональных смен: суммарное время, "
    "количество, среднее. Окно — пресет `period` (день/неделя/месяц) ЛИБО "
    "произвольный диапазон `date_from`/`date_to` (UTC, включительно по началу "
    "смены). Источники окна взаимоисключающи.",
)
async def shift_stats(
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
    stats = await shift_service.get_shift_stats(
        session,
        user.id,
        period,
        date_from=date_from,
        date_to=date_to,
    )
    await session.commit()
    return ApiResponse.success(ShiftStatsResponse(**stats).model_dump(mode="json"))


@router.post(
    "/start",
    status_code=201,
    summary="Начать смену",
    description="Начинает новую смену. Без `organization_id` — персональная смена. "
    "С `organization_id` — организационная смена (требуется членство, при включённой "
    "геопроверке нужны координаты). Допускается одна активная персональная смена + "
    "по одной на каждую организацию одновременно.",
)
async def start_shift(
    user: CurrentUserDep,
    session: SessionDep,
    body: ShiftStartRequest | None = None,
) -> ApiResponse:
    org_id = None
    lat = None
    lng = None
    work_location_id = None
    if body is not None:
        org_id = uuid.UUID(body.organization_id) if body.organization_id else None
        lat = body.latitude
        lng = body.longitude
        work_location_id = body.work_location_id

    shift = await shift_service.start_shift(
        session,
        user.id,
        organization_id=org_id,
        latitude=lat,
        longitude=lng,
        work_location_id=work_location_id,
    )
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))


@router.post(
    "/{shift_id}/pause",
    summary="Поставить на паузу",
    description="Ставит активную смену на паузу. Для организационных смен может быть "
    "ограничено настройкой `max_pauses_per_shift`.",
)
async def pause_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.pause_shift(session, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))


@router.post(
    "/{shift_id}/resume",
    summary="Возобновить смену",
    description="Снимает смену с паузы и возвращает в статус active.",
)
async def resume_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.resume_shift(session, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))


@router.post(
    "/{shift_id}/finish",
    summary="Завершить смену",
    description="Завершает активную или стоящую на паузе смену. Все открытые паузы "
    "автоматически закрываются.",
)
async def finish_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    shift = await shift_service.finish_shift(session, shift_id, user.id)
    await audit_service.record(
        session,
        action=AuditAction.shift_finish,
        resource_type=AuditResource.shift,
        organization_id=shift.organization_id,
        actor_user_id=user.id,
        resource_id=shift.id,
        summary={"finished_at": shift.finished_at.isoformat() if shift.finished_at else None},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))
