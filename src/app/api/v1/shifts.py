# src/app/api/v1/shifts.py
import uuid
from datetime import datetime as dt_datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.audit_log import AuditAction, AuditResource
from src.app.models.organization import Organization
from src.app.models.shift import Shift
from src.app.models.shift_overtime_request import ShiftOvertimeRequest
from src.app.schemas.base import ApiResponse
from src.app.schemas.overtime import OvertimeCreateRequest, OvertimeInfo
from src.app.schemas.shift import (
    ShiftListResponse,
    ShiftResponse,
    ShiftStartRequest,
    ShiftStatsResponse,
)
from src.app.services import audit as audit_service
from src.app.services import overtime as overtime_service
from src.app.services import payroll as payroll_service
from src.app.services import shift as shift_service
from src.app.services.checklist_instance import ShiftChecklistsSummary
from src.app.services.organization_settings import (
    get_late_tolerance_minutes_map,
    get_settings_for_org,
)
from src.app.services.shift import ShiftIdentity, calculate_worked_seconds, compute_late_seconds
from src.app.utils.request import get_client_ip

router = APIRouter(prefix="/shifts", tags=["shifts"])


def _overtime_payload(request: ShiftOvertimeRequest | None) -> dict[str, Any] | None:
    if request is None:
        return None
    return OvertimeInfo(
        id=str(request.id),
        minutes=request.minutes,
        status=request.status.value,
        comment=request.comment,
        review_comment=request.review_comment,
        reviewed_at=request.reviewed_at,
        created_at=request.created_at,
    ).model_dump(mode="json")


def _shift_to_response(
    shift: Shift,
    identity: ShiftIdentity | None = None,
    checklists_summary: ShiftChecklistsSummary | None = None,
    *,
    late_tolerance_minutes: int = 0,
    overtime: ShiftOvertimeRequest | None = None,
    created_by_name: str | None = None,
    edited_by_name: str | None = None,
    earnings: dict[str, Any] | None = None,
    organization_timezone: str | None = None,
) -> dict[str, Any]:
    """Сериализовать смену.

    Без `identity` (персональный контекст) additive-поля сотрудника остаются
    `null`. В орг-контексте `identity` наполняет `user_name` / `display_name` /
    `user_email` / `role` / `custom_role_name`. Аналогично `checklists_summary` заполняется
    ТОЛЬКО в орг-эндпоинтах смен (checklist_reports); в персональном контексте
    остаётся `null`. `late_tolerance_minutes`/`overtime` — ингредиенты для R5/R6
    (work_schedules), передаются вызывающим кодом (батч без N+1 в списках).
    `is_manual`/`is_edited`/`manual_note`/`edited_at`/`is_deleted` (manual_time_entry)
    читаются прямо со смены — видны и в персональном, и в орг-контексте (R7).
    `created_by_name`/`edited_by_name` — имя админа, только орг-контекст.
    `geo_fallback*` (shift_geo_photo_fallback) тоже читаются со смены: у обычной
    смены это `false`/`null`/`null`. `earnings` (shift_history_earnings, ADR-005) —
    словарь из `payroll.get_shift_earnings_map` либо `None`; передаётся вызывающим
    кодом ТОЛЬКО из `list_shifts`/`get_shift` (батч без N+1) — остальные эндпоинты
    смены (`/start`, `/pause`, `/resume`, `/finish`) его не считают и всегда
    сериализуют `null`.
    """
    work_location = getattr(shift, "work_location", None)
    return ShiftResponse(
        id=str(shift.id),
        user_id=str(shift.user_id),
        organization_id=str(shift.organization_id) if shift.organization_id else None,
        organization_timezone=organization_timezone,
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
        display_name=identity.display_name if identity is not None else None,
        user_email=identity.user_email if identity is not None else None,
        role=identity.role if identity is not None else None,
        custom_role_name=identity.custom_role_name if identity is not None else None,
        checklists_summary=(
            {
                "total": checklists_summary.total,
                "completed": checklists_summary.completed,
                "required_total": checklists_summary.required_total,
                "required_incomplete": checklists_summary.required_incomplete,
            }
            if checklists_summary is not None
            else None
        ),
        work_schedule_id=str(shift.work_schedule_id) if shift.work_schedule_id else None,
        schedule_name=shift.schedule_name,
        scheduled_start_at=shift.scheduled_start_at,
        scheduled_end_at=shift.scheduled_end_at,
        late_seconds=compute_late_seconds(shift, late_tolerance_minutes),
        finish_reason=shift.finish_reason.value if shift.finish_reason else None,
        overtime=_overtime_payload(overtime),
        is_manual=shift.created_by_user_id is not None,
        is_edited=shift.edited_at is not None,
        manual_note=shift.manual_note,
        edited_at=shift.edited_at,
        edited_by_name=edited_by_name,
        created_by_name=created_by_name,
        is_deleted=shift.is_deleted,
        geo_fallback=shift.geo_fallback_reason is not None,
        geo_fallback_reason=(
            shift.geo_fallback_reason.value if shift.geo_fallback_reason is not None else None
        ),
        geo_fallback_photo_file_id=(
            str(shift.geo_fallback_photo_file_id) if shift.geo_fallback_photo_file_id else None
        ),
        earnings=earnings,
    ).model_dump(mode="json")


async def get_organization_timezones(
    session: SessionDep, organization_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Текущие IANA-зоны организаций одним запросом, включая soft-deleted org."""
    if not organization_ids:
        return {}
    rows = await session.execute(
        select(Organization.id, Organization.timezone).where(Organization.id.in_(organization_ids))
    )
    return dict(rows.tuples().all())


async def _enrich_single_shift(
    session: SessionDep,
    shift: Shift,
) -> tuple[int, ShiftOvertimeRequest | None]:
    """late_tolerance_minutes организации смены (0 — персональная/без записи настроек)
    + последняя заявка на переработку. Для одиночных эндпоинтов (старт/пауза/резюме/финиш)."""
    late_tolerance = 0
    if shift.organization_id is not None:
        org_settings = await get_settings_for_org(session, shift.organization_id)
        if org_settings is not None:
            late_tolerance = org_settings.late_tolerance_minutes
    overtime_map = await overtime_service.get_latest_overtime_for_shifts(session, [shift.id])
    return late_tolerance, overtime_map.get(shift.id)


async def _single_shift_response(
    session: SessionDep,
    shift: Shift,
    *,
    earnings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Сериализовать одну смену с ровно одним запросом IANA-контекста при наличии org."""
    late_tolerance, overtime = await _enrich_single_shift(session, shift)
    timezone_map = await get_organization_timezones(
        session, {shift.organization_id} if shift.organization_id is not None else set()
    )
    return _shift_to_response(
        shift,
        late_tolerance_minutes=late_tolerance,
        overtime=overtime,
        earnings=earnings,
        organization_timezone=(
            timezone_map.get(shift.organization_id) if shift.organization_id is not None else None
        ),
    )


@router.get(
    "",
    response_model=ApiResponse[ShiftListResponse],
    summary="История смен",
    description="История смен текущего пользователя с пагинацией — персональные и "
    "организационные вперемешку (или срез через `scope`/`organization_id`). "
    "Поддерживает фильтрацию по статусу, дате и контексту.",
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
    scope: str | None = Query(
        None,
        description="Срез истории: all (по умолчанию, прежнее поведение) | personal "
        "(organization_id IS NULL) | organization (нужен organization_id)",
    ),
    organization_id: str | None = Query(
        None,
        description="UUID организации, обязателен при scope=organization, запрещён "
        "при остальных scope. Членство не проверяется — фильтр применяется к "
        "собственным сменам пользователя",
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

    scope_enum = shift_service.parse_history_scope(scope)
    organization_id_uuid = shift_service.parse_history_organization_id(organization_id)
    shift_service.validate_history_scope(scope_enum, organization_id_uuid)

    shifts, total = await shift_service.get_shifts(
        session,
        user.id,
        status=status_enum,
        date_from=date_from,
        date_to=date_to,
        scope=scope_enum,
        organization_id=organization_id_uuid,
        limit=limit,
        offset=offset,
        sort=sort,
        order=order,
    )
    await session.commit()

    # Персональная история может смешивать org-смены разных организаций
    # пользователя — все батчи без N+1 (work_schedules: R5/R6; shift_history_earnings).
    org_ids = {s.organization_id for s in shifts if s.organization_id is not None}
    timezone_map = await get_organization_timezones(session, org_ids)
    tolerance_map = await get_late_tolerance_minutes_map(session, org_ids)
    overtime_map = await overtime_service.get_latest_overtime_for_shifts(
        session, [s.id for s in shifts]
    )
    earnings_map = await payroll_service.get_shift_earnings_map(session, shifts)
    return ApiResponse.success(
        ShiftListResponse(
            items=[
                _shift_to_response(
                    s,
                    late_tolerance_minutes=(
                        tolerance_map.get(s.organization_id, 0) if s.organization_id else 0
                    ),
                    overtime=overtime_map.get(s.id),
                    earnings=earnings_map.get(s.id),
                    organization_timezone=(
                        timezone_map.get(s.organization_id)
                        if s.organization_id is not None
                        else None
                    ),
                )
                for s in shifts
            ],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/stats",
    summary="Статистика смен",
    description="Агрегированная статистика смен пользователя — персональных и "
    "организационных вперемешку (или срез через `scope`/`organization_id`): "
    "суммарное время, количество, среднее. Окно — пресет `period` "
    "(день/неделя/месяц) ЛИБО произвольный диапазон `date_from`/`date_to` (UTC, "
    "включительно по началу смены). Источники окна взаимоисключающи. При "
    "одинаковых `scope`/`organization_id`/окне описывает то же множество смен, "
    "что и `GET /shifts`.",
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
    scope: str | None = Query(
        None,
        description="Срез истории: all (по умолчанию, прежнее поведение) | personal "
        "(organization_id IS NULL) | organization (нужен organization_id)",
    ),
    organization_id: str | None = Query(
        None,
        description="UUID организации, обязателен при scope=organization, запрещён "
        "при остальных scope. Членство не проверяется — фильтр применяется к "
        "собственным сменам пользователя",
    ),
) -> ApiResponse:
    scope_enum = shift_service.parse_history_scope(scope)
    organization_id_uuid = shift_service.parse_history_organization_id(organization_id)
    shift_service.validate_history_scope(scope_enum, organization_id_uuid)

    stats = await shift_service.get_shift_stats(
        session,
        user.id,
        period,
        date_from=date_from,
        date_to=date_to,
        scope=scope_enum,
        organization_id=organization_id_uuid,
    )
    await session.commit()
    return ApiResponse.success(ShiftStatsResponse(**stats).model_dump(mode="json"))


@router.post(
    "/start",
    response_model=ApiResponse[ShiftResponse],
    status_code=201,
    summary="Начать смену",
    description="Начинает новую смену. Без `organization_id` — персональная смена. "
    "С `organization_id` — организационная смена (требуется членство, при включённой "
    "геопроверке нужны координаты). Допускается одна активная персональная смена + "
    "по одной на каждую организацию одновременно. Если геолокация на клиенте "
    "физически недоступна — вместо координат можно прислать `geo_fallback_photo_id` "
    "(файл категории `shift_geo_photo`) + `geo_fallback_reason` и обязательный "
    "`work_location_id`: смена стартует помеченной «без геопроверки» "
    "(shift_geo_photo_fallback).",
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
    work_schedule_id = None
    geo_fallback_photo_id = None
    geo_fallback_reason = None
    if body is not None:
        org_id = uuid.UUID(body.organization_id) if body.organization_id else None
        lat = body.latitude
        lng = body.longitude
        work_location_id = body.work_location_id
        work_schedule_id = body.work_schedule_id
        geo_fallback_photo_id = body.geo_fallback_photo_id
        geo_fallback_reason = body.geo_fallback_reason

    shift = await shift_service.start_shift(
        session,
        user.id,
        organization_id=org_id,
        latitude=lat,
        longitude=lng,
        work_location_id=work_location_id,
        work_schedule_id=work_schedule_id,
        geo_fallback_photo_id=geo_fallback_photo_id,
        geo_fallback_reason=geo_fallback_reason,
    )
    await session.commit()
    return ApiResponse.success(await _single_shift_response(session, shift))


@router.get(
    "/{shift_id}",
    response_model=ApiResponse[ShiftResponse],
    summary="Деталь своей смены",
    description="Деталь собственной смены текущего пользователя по id — персональной "
    "(`organization_id=null`) или организационной, где пользователь является её владельцем. "
    "Объявлен ПОСЛЕ статических `/stats` и `/start`, иначе перехватил бы их как shift_id. "
    "Чужая, несуществующая или удалённая (soft-delete) смена — `404 SHIFT_NOT_FOUND` "
    "(существование чужих смен не раскрывается).",
)
async def get_shift(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    shift = await shift_service.get_own_shift_detail(session, shift_id, user.id)
    earnings_map = await payroll_service.get_shift_earnings_map(session, [shift])
    return ApiResponse.success(
        await _single_shift_response(session, shift, earnings=earnings_map.get(shift.id))
    )


@router.post(
    "/{shift_id}/pause",
    response_model=ApiResponse[ShiftResponse],
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
    return ApiResponse.success(await _single_shift_response(session, shift))


@router.post(
    "/{shift_id}/resume",
    response_model=ApiResponse[ShiftResponse],
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
    return ApiResponse.success(await _single_shift_response(session, shift))


@router.post(
    "/{shift_id}/overtime",
    status_code=201,
    summary="Подать заявку на переработку",
    description="Владелец завершённой смены просит зачесть время сверх планового окна. "
    "Допустимо только когда у смены есть график, факт не превысил план, срок подачи "
    "не истёк и по смене нет активной заявки (work_schedules, R6).",
)
async def create_overtime_request(
    shift_id: uuid.UUID,
    body: OvertimeCreateRequest,
    user: CurrentUserDep,
    session: SessionDep,
    request: Request,
) -> ApiResponse:
    overtime_request = await overtime_service.create_overtime_request(
        session,
        shift_id,
        user.id,
        minutes=body.minutes,
        comment=body.comment,
    )
    await audit_service.record(
        session,
        action=AuditAction.overtime_request,
        resource_type=AuditResource.overtime,
        organization_id=None,
        actor_user_id=user.id,
        resource_id=overtime_request.id,
        summary={"shift_id": str(shift_id), "minutes": body.minutes},
        ip_address=get_client_ip(request),
    )
    await session.commit()
    return ApiResponse.success(_overtime_payload(overtime_request))


@router.delete(
    "/{shift_id}/overtime",
    summary="Отозвать заявку на переработку",
    description="Удаляет свою заявку на переработку по смене, пока она в статусе pending. "
    "200 с конвертом {data:null,error:null} — как остальные DELETE в проекте (не 204 без "
    "тела: контракт {data,error} обязан быть на любом ответе).",
)
async def delete_overtime_request(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await overtime_service.delete_own_overtime_request(session, shift_id, user.id)
    await session.commit()
    return ApiResponse.success(None)


@router.post(
    "/{shift_id}/finish",
    response_model=ApiResponse[ShiftResponse],
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
    return ApiResponse.success(await _single_shift_response(session, shift))
