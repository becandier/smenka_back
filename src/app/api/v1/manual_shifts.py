"""Ручной ввод/правка/удаление/восстановление смен администратором (manual_time_entry)."""

import uuid
from typing import Any

from fastapi import APIRouter, Query

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.api.v1.shifts import (
    _enrich_single_shift,
    _shift_to_response,
    get_organization_timezones,
)
from src.app.models.shift import Shift
from src.app.schemas.base import ApiResponse
from src.app.schemas.shift import ManualShiftCreate, ManualShiftUpdate, ShiftDeletedResponse
from src.app.services import manual_shift as manual_shift_service
from src.app.services import shift as shift_service

router = APIRouter(prefix="/organizations/{org_id}", tags=["manual-shifts"])


async def _manual_shift_response(session: SessionDep, shift: Shift) -> dict[str, Any]:
    """Полный ShiftResponse орг-контекста: late_tolerance/переработка + имена
    админов created_by/edited_by, без N+1 на единичный объект."""
    late_tolerance, overtime = await _enrich_single_shift(session, shift)
    actor_names = await shift_service.build_manual_actor_names(session, [shift])
    timezone_map = await get_organization_timezones(
        session, {shift.organization_id} if shift.organization_id is not None else set()
    )
    return _shift_to_response(
        shift,
        late_tolerance_minutes=late_tolerance,
        overtime=overtime,
        created_by_name=(
            actor_names.get(shift.created_by_user_id) if shift.created_by_user_id else None
        ),
        edited_by_name=(
            actor_names.get(shift.edited_by_user_id) if shift.edited_by_user_id else None
        ),
        organization_timezone=(
            timezone_map.get(shift.organization_id) if shift.organization_id is not None else None
        ),
    )


@router.post(
    "/shifts",
    status_code=201,
    summary="Создать смену вручную",
    description=(
        "Заводит смену за сотрудника задним числом — сразу завершённой "
        "(status=finished, finish_reason=manual). Чек-листы не создаются. "
        "Owner/admin (manual_time_entry)."
    ),
)
async def create_manual_shift(
    org_id: uuid.UUID,
    body: ManualShiftCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await manual_shift_service.create_manual_shift(
        session,
        org_id,
        user.id,
        user_id=body.user_id,
        started_at=body.started_at,
        finished_at=body.finished_at,
        work_location_id=body.work_location_id,
        work_schedule_id=body.work_schedule_id,
        pauses=body.pauses,
        note=body.note,
    )
    await session.commit()
    return ApiResponse.success(await _manual_shift_response(session, shift))


@router.patch(
    "/shifts/{shift_id}",
    summary="Изменить смену вручную",
    description=(
        "Правит существующую смену. Для смены finished — можно менять всё. Для "
        "active/paused — started_at/work_location_id/note; передача finished_at "
        "завершает её задним числом (инструмент для зависших смен). Удалённую "
        "смену править нельзя (сначала restore). Owner/admin (manual_time_entry)."
    ),
)
async def update_manual_shift(
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    body: ManualShiftUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    # model_dump() превращает вложенные ManualPauseInput в обычные dict — сервису
    # нужны объекты с атрибутами .started_at/.finished_at (та же сигнатура, что и
    # в create_manual_shift, где body.pauses передаётся как есть).
    fields = body.model_dump(exclude_unset=True)
    if "pauses" in fields:
        fields["pauses"] = body.pauses
    shift = await manual_shift_service.update_manual_shift(
        session,
        org_id,
        shift_id,
        user.id,
        fields,
    )
    await session.commit()
    return ApiResponse.success(await _manual_shift_response(session, shift))


@router.delete(
    "/shifts/{shift_id}",
    summary="Удалить смену (soft-delete)",
    description=(
        "Данные не удаляются физически: is_deleted=true, deleted_by_user_id, "
        "deleted_at. Смена исчезает из списков/статистики/payroll. Owner/admin "
        "(manual_time_entry)."
    ),
)
async def delete_manual_shift(
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    note: str | None = Query(
        default=None, max_length=500, description="Причина удаления (опционально)"
    ),
) -> ApiResponse:
    await manual_shift_service.delete_shift(session, org_id, shift_id, user.id, note=note)
    await session.commit()
    return ApiResponse.success(ShiftDeletedResponse(deleted=True).model_dump())


@router.post(
    "/shifts/{shift_id}/restore",
    summary="Восстановить удалённую смену",
    description=(
        "Восстановление уже неудалённой смены идемпотентно — возвращает её как "
        "есть, не ошибку. Owner/admin (manual_time_entry)."
    ),
)
async def restore_manual_shift(
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await manual_shift_service.restore_shift(session, org_id, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(await _manual_shift_response(session, shift))
