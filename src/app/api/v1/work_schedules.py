import uuid
from datetime import time
from typing import Any

from fastapi import APIRouter, Query, Request

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.models.organization import OrganizationMember
from src.app.models.work_schedule import WorkSchedule
from src.app.schemas.base import ApiResponse
from src.app.schemas.work_schedule import (
    EffectiveSchedulesResponse,
    MySchedulesResponse,
    ScheduleAssignmentsResponse,
    ScheduleLocationAssignmentRequest,
    ScheduleMemberOverrideRequest,
    ScheduleRoleAssignmentRequest,
    ShiftScheduleChangeRequest,
    WorkScheduleCreate,
    WorkScheduleListResponse,
    WorkScheduleResponse,
    WorkScheduleUpdate,
)
from src.app.services import audit as audit_service
from src.app.services import shift as shift_service
from src.app.services import work_schedule as ws_service
from src.app.utils.request import get_client_ip

router = APIRouter(prefix="/organizations/{org_id}", tags=["work-schedules"])


def _format_hhmm(value: time) -> str:
    return value.strftime("%H:%M")


def _parse_hhmm(raw: str) -> time:
    return time.fromisoformat(raw)


def _schedule_to_response(
    schedule: WorkSchedule,
    role_ids: list[uuid.UUID],
    location_ids: list[uuid.UUID],
) -> dict[str, Any]:
    return WorkScheduleResponse(
        id=str(schedule.id),
        name=schedule.name,
        start_time=_format_hhmm(schedule.start_time),
        end_time=_format_hhmm(schedule.end_time),
        duration_minutes=schedule.duration_minutes,
        crosses_midnight=schedule.crosses_midnight,
        is_archived=schedule.is_archived,
        role_ids=[str(r) for r in role_ids],
        work_location_ids=[str(loc) for loc in location_ids],
        created_at=schedule.created_at,
    ).model_dump(mode="json")


def _member_info(member: OrganizationMember) -> dict[str, str]:
    return {
        "user_id": str(member.user_id),
        "user_name": member.user.name,
        # "" вместо null — admin-created учётка без email (admin_created_accounts).
        "user_email": member.user.email_display,
    }


def _schedule_base_fields(schedule: WorkSchedule) -> dict[str, Any]:
    """Общие поля графика (id/имя/время/длительность), переиспользуются
    эффективными наборами (`get_member_schedules`/`get_my_schedules`), которым
    не нужны `is_archived`/`role_ids`/`work_location_ids` из `_schedule_to_response`."""
    return {
        "id": str(schedule.id),
        "name": schedule.name,
        "start_time": _format_hhmm(schedule.start_time),
        "end_time": _format_hhmm(schedule.end_time),
        "duration_minutes": schedule.duration_minutes,
        "crosses_midnight": schedule.crosses_midnight,
    }


# --- CRUD ------------------------------------------------------------------


@router.post(
    "/work-schedules",
    status_code=201,
    summary="Создать график работы",
    description="Интервал времени внутри суток; `end_time < start_time` — ночной график, "
    "переходящий через полночь. Owner/admin.",
)
async def create_schedule(
    org_id: uuid.UUID,
    body: WorkScheduleCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    schedule = await ws_service.create_schedule(
        session,
        org_id,
        user.id,
        name=body.name,
        start_time=_parse_hhmm(body.start_time),
        end_time=_parse_hhmm(body.end_time),
    )
    await session.commit()
    return ApiResponse.success(_schedule_to_response(schedule, [], []))


@router.get(
    "/work-schedules",
    summary="Список графиков работы",
    description="Привязки (role_ids/work_location_ids) отдаются сразу — без N+1 в админке. "
    "Owner/admin.",
)
async def list_schedules(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    include_archived: bool = Query(False, description="Включить архивные графики"),
) -> ApiResponse:
    rows = await ws_service.list_schedules(
        session, org_id, user.id, include_archived=include_archived
    )
    return ApiResponse.success(
        WorkScheduleListResponse(
            items=[_schedule_to_response(s, role_ids, loc_ids) for s, role_ids, loc_ids in rows],
            total=len(rows),
        ).model_dump(mode="json")
    )


@router.get(
    "/work-schedules/{schedule_id}",
    summary="Детали графика работы",
    description="Owner/admin.",
)
async def get_schedule_detail(
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    schedule, role_ids, location_ids = await ws_service.get_schedule_detail(
        session, org_id, schedule_id, user.id
    )
    return ApiResponse.success(_schedule_to_response(schedule, role_ids, location_ids))


@router.patch(
    "/work-schedules/{schedule_id}",
    summary="Обновить график работы",
    description="Все поля опциональны (exclude_unset). Правка времени не меняет уже "
    "созданные смены — там снимок. Owner/admin.",
)
async def update_schedule(
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    body: WorkScheduleUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    fields = body.model_dump(exclude_unset=True)
    schedule = await ws_service.update_schedule(
        session,
        org_id,
        schedule_id,
        user.id,
        name=fields.get("name"),
        start_time=_parse_hhmm(fields["start_time"]) if "start_time" in fields else None,
        end_time=_parse_hhmm(fields["end_time"]) if "end_time" in fields else None,
        is_archived=fields.get("is_archived"),
    )
    await session.commit()
    role_ids = (await ws_service.get_role_ids_for_schedules(session, [schedule.id])).get(
        schedule.id, []
    )
    location_ids = (await ws_service.get_location_ids_for_schedules(session, [schedule.id])).get(
        schedule.id, []
    )
    return ApiResponse.success(_schedule_to_response(schedule, role_ids, location_ids))


@router.delete(
    "/work-schedules/{schedule_id}",
    summary="Удалить график работы",
    description="Физическое удаление. У смен, где график уже использован, `work_schedule_id` "
    "становится null (FK SET NULL), но снимок (`schedule_name`/`scheduled_*`) остаётся — "
    "история не рушится. Owner/admin.",
)
async def delete_schedule(
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await ws_service.delete_schedule(session, org_id, schedule_id, user.id)
    await session.commit()
    return ApiResponse.success(None)


# --- Назначения ---------------------------------------------------------------


@router.put(
    "/work-schedules/{schedule_id}/roles",
    summary="Назначить график ролям",
    description="PUT-семантика: передайте полный список ролей. Owner/admin.",
)
async def assign_schedule_to_roles(
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    body: ScheduleRoleAssignmentRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    role_uuids = [uuid.UUID(r) for r in body.role_ids]
    result_ids = await ws_service.set_schedule_roles(
        session, org_id, schedule_id, role_uuids, user.id
    )
    await session.commit()
    return ApiResponse.success({"role_ids": [str(r) for r in result_ids]})


@router.put(
    "/work-schedules/{schedule_id}/locations",
    summary="Задать точки графика",
    description="PUT-семантика: передайте полный список точек. Пустой список снимает все "
    "привязки (график снова действует на всех точках). Owner/admin.",
)
async def assign_schedule_to_locations(
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    body: ScheduleLocationAssignmentRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    location_uuids = [uuid.UUID(loc_id) for loc_id in body.work_location_ids]
    result_ids = await ws_service.set_schedule_locations(
        session, org_id, schedule_id, location_uuids, user.id
    )
    await session.commit()
    return ApiResponse.success({"work_location_ids": [str(loc_id) for loc_id in result_ids]})


@router.get(
    "/work-schedules/{schedule_id}/assignments",
    summary="Кому назначен график",
    description="Роли + точки + сотрудники с личными add/remove. Owner/admin.",
)
async def get_schedule_assignments(
    org_id: uuid.UUID,
    schedule_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    (
        role_ids,
        personal_add,
        personal_remove,
        location_ids,
    ) = await ws_service.get_schedule_assignments(session, org_id, schedule_id, user.id)
    return ApiResponse.success(
        ScheduleAssignmentsResponse(
            role_ids=[str(r) for r in role_ids],
            work_location_ids=[str(loc_id) for loc_id in location_ids],
            personal_add=[_member_info(m) for m in personal_add],
            personal_remove=[_member_info(m) for m in personal_remove],
        ).model_dump(mode="json")
    )


@router.put(
    "/members/{user_id}/schedule-overrides",
    summary="Личные исключения графиков сотрудника",
    description="PUT-семантика: полная замена персональных исключений сотрудника. Owner/admin.",
)
async def set_member_schedule_overrides(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    body: ScheduleMemberOverrideRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    overrides = [(uuid.UUID(o.schedule_id), o.override_type) for o in body.overrides]
    parsed = await ws_service.set_member_schedule_overrides(
        session, org_id, user_id, overrides, user.id
    )
    await session.commit()
    return ApiResponse.success(
        {
            "overrides": [
                {"schedule_id": str(sid), "override_type": t.value} for sid, t in parsed
            ],
        }
    )


@router.get(
    "/members/{user_id}/schedules",
    summary="Эффективный набор графиков сотрудника",
    description="Резолв R1 — для проверки настройки в админке. Owner/admin.",
)
async def get_member_schedules(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    work_location_id: uuid.UUID | None = Query(default=None),
) -> ApiResponse:
    pairs = await ws_service.get_member_effective_schedules(
        session, org_id, user_id, user.id, work_location_id=work_location_id
    )
    return ApiResponse.success(
        EffectiveSchedulesResponse(
            items=[
                {**_schedule_base_fields(s), "is_archived": s.is_archived, "source": source}
                for s, source in pairs
            ],
        ).model_dump(mode="json")
    )


@router.get(
    "/my-schedules",
    summary="Мои доступные графики",
    description="Эффективный набор графиков текущего сотрудника + плановое окно, если начать "
    "смену прямо сейчас. Ключевой эндпоинт для экрана старта смены в мобилке. При включённой "
    "геопроверке точку можно резолвить по `lat`/`lng` (тем же подбором, что и `POST "
    "/shifts/start`) — если ни одна зона не совпала, это НЕ ошибка, точка просто остаётся "
    "неопределённой. Явный `work_location_id` имеет приоритет над `lat`/`lng`. Owner (не "
    "участник организации) получает пустой список — не трекает время.",
)
async def get_my_schedules(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    work_location_id: uuid.UUID | None = Query(default=None),
    lat: float | None = Query(
        default=None, ge=-90, le=90, description="Широта — резолв точки при геопроверке"
    ),
    lng: float | None = Query(
        default=None, ge=-180, le=180, description="Долгота — резолв точки при геопроверке"
    ),
) -> ApiResponse:
    result = await ws_service.get_my_schedules(
        session,
        org_id,
        user.id,
        work_location_id=work_location_id,
        latitude=lat,
        longitude=lng,
    )
    resolved = result.resolved_work_location
    return ApiResponse.success(
        MySchedulesResponse(
            items=[
                {
                    **_schedule_base_fields(it.schedule),
                    "next_start_at": it.next_start_at,
                    "next_end_at": it.next_end_at,
                    "is_current": it.is_current,
                    "starts_in_minutes": it.starts_in_minutes,
                }
                for it in result.items
            ],
            total=len(result.items),
            require_schedule=result.require_schedule,
            resolved_work_location=(
                {"id": str(resolved.id), "name": resolved.name} if resolved is not None else None
            ),
        ).model_dump(mode="json")
    )


# --- R7: смена графика у смены -------------------------------------------------


@router.patch(
    "/shifts/{shift_id}/schedule",
    summary="Сменить график у смены",
    description="Owner/admin меняет график у смены (активной или завершённой), если "
    "сотрудник выбрал не тот. `scheduled_*` пересчитываются от неизменного `started_at`; "
    "`finished_at` завершённой смены не меняется никогда. `work_schedule_id: null` — "
    "снять график (авто-завершение отключается).",
)
async def change_shift_schedule(
    org_id: uuid.UUID,
    shift_id: uuid.UUID,
    body: ShiftScheduleChangeRequest,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    from src.app.api.v1.shifts import _enrich_single_shift, _shift_to_response

    new_schedule_id = uuid.UUID(body.work_schedule_id) if body.work_schedule_id else None

    # Снимок старого значения ДО мутации — нужен в summary аудита (R7).
    before = await shift_service.get_org_shift_detail(session, org_id, shift_id)
    old_schedule_id = before.work_schedule_id

    shift = await ws_service.change_shift_schedule(
        session, org_id, shift_id, user.id, new_schedule_id
    )
    await audit_service.record(
        session,
        action=AuditAction.shift_schedule_change,
        resource_type=AuditResource.shift,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=shift.id,
        summary={
            "old_schedule_id": str(old_schedule_id) if old_schedule_id else None,
            "new_schedule_id": str(new_schedule_id) if new_schedule_id else None,
            "scheduled_start_at": (
                shift.scheduled_start_at.isoformat() if shift.scheduled_start_at else None
            ),
            "scheduled_end_at": (
                shift.scheduled_end_at.isoformat() if shift.scheduled_end_at else None
            ),
        },
        ip_address=get_client_ip(request),
    )
    await session.commit()
    late_tolerance, overtime = await _enrich_single_shift(session, shift)
    # Орг-контекст (manual_time_entry): created_by_name/edited_by_name должны
    # заполняться и здесь, как в остальных орг-эндпоинтах смен.
    actor_names = await shift_service.build_manual_actor_names(session, [shift])
    return ApiResponse.success(
        _shift_to_response(
            shift,
            late_tolerance_minutes=late_tolerance,
            overtime=overtime,
            created_by_name=(
                actor_names.get(shift.created_by_user_id) if shift.created_by_user_id else None
            ),
            edited_by_name=(
                actor_names.get(shift.edited_by_user_id) if shift.edited_by_user_id else None
            ),
        )
    )
