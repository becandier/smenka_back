# src/app/api/v1/shifts.py
import uuid
from datetime import datetime as dt_datetime

from fastapi import APIRouter, Query

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.shift import ShiftListResponse, ShiftResponse, ShiftStatsResponse
from src.app.services import shift as shift_service
from src.app.services.shift import calculate_worked_seconds

router = APIRouter(prefix="/shifts", tags=["shifts"])


def _shift_to_response(shift) -> dict:
    return ShiftResponse(
        id=str(shift.id),
        user_id=str(shift.user_id),
        started_at=shift.started_at,
        finished_at=shift.finished_at,
        status=shift.status.value,
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
    ).model_dump(mode="json")


@router.get("")
async def list_shifts(
    user: CurrentUserDep,
    session: SessionDep,
    status: str | None = Query(None, description="Filter by status: active, paused, finished"),
    date_from: dt_datetime | None = Query(None, description="Filter shifts started after this datetime"),
    date_to: dt_datetime | None = Query(None, description="Filter shifts started before this datetime"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
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
            )

    shifts, total = await shift_service.get_shifts(
        session,
        user.id,
        status=status_enum,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
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


@router.get("/stats")
async def shift_stats(
    user: CurrentUserDep,
    session: SessionDep,
    period: str = Query(..., description="Period: day, week, month"),
) -> ApiResponse:
    stats = await shift_service.get_shift_stats(session, user.id, period)
    await session.commit()
    return ApiResponse.success(
        ShiftStatsResponse(**stats).model_dump()
    )


@router.post("/start", status_code=201)
async def start_shift(
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.start_shift(session, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))


@router.post("/{shift_id}/pause")
async def pause_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.pause_shift(session, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))


@router.post("/{shift_id}/resume")
async def resume_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.resume_shift(session, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))


@router.post("/{shift_id}/finish")
async def finish_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.finish_shift(session, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(_shift_to_response(shift))
