# src/app/api/v1/shifts.py
import uuid

from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.shift import ShiftResponse
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
