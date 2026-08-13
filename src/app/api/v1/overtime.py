import uuid
from datetime import datetime as dt_datetime
from typing import Any

from fastapi import APIRouter, Query

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.overtime import (
    OrgOvertimeRequestListResponse,
    OvertimeReviewRequest,
    OvertimeReviewResponse,
)
from src.app.services import overtime as overtime_service

router = APIRouter(prefix="/organizations/{org_id}", tags=["overtime-requests"])


def _row_to_response(row: overtime_service.OrgOvertimeRow) -> dict[str, Any]:
    req = row.request
    user = row.user
    return {
        "id": str(req.id),
        "shift_id": str(req.shift_id),
        "minutes": req.minutes,
        "comment": req.comment,
        "status": req.status.value,
        "review_comment": req.review_comment,
        "reviewed_at": req.reviewed_at,
        "created_at": req.created_at,
        "user": {
            "id": str(user.id) if user is not None else str(row.shift.user_id),
            "user_name": user.name if user is not None else "Unknown",
            "display_name": row.display_name,
            # "" вместо null — admin-created учётка без email (admin_created_accounts).
            "email": user.email_display if user is not None else "",
        },
        "shift": {
            "started_at": row.shift.started_at,
            "finished_at": row.shift.finished_at,
            "scheduled_start_at": row.shift.scheduled_start_at,
            "scheduled_end_at": row.shift.scheduled_end_at,
            "schedule_name": row.shift.schedule_name,
            "work_location_name": row.work_location_name,
        },
    }


@router.get(
    "/overtime-requests",
    summary="Реестр заявок на переработку",
    description="Owner/admin. Фильтры: status, user_ids (CSV), date_from/date_to (по "
    "shift.started_at), пагинация.",
)
async def list_overtime_requests(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    status: str | None = Query(None, description="pending | approved | rejected"),
    user_ids: str | None = Query(None, description="CSV UUIDов сотрудников"),
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница по shift.started_at, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница по shift.started_at, включительно (UTC)"
    ),
    limit: int = Query(50, ge=1, le=200, description="Размер страницы (1–200)"),
    offset: int = Query(0, ge=0, description="Смещение для пагинации"),
) -> ApiResponse:
    parsed_user_ids: list[uuid.UUID] | None = None
    if user_ids:
        parsed_user_ids = [
            uuid.UUID(token.strip()) for token in user_ids.split(",") if token.strip()
        ]

    rows, total = await overtime_service.list_org_overtime_requests(
        session,
        org_id,
        user.id,
        status=status,
        user_ids=parsed_user_ids,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return ApiResponse.success(
        OrgOvertimeRequestListResponse(
            items=[_row_to_response(r) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.patch(
    "/overtime-requests/{request_id}",
    summary="Рассмотреть заявку на переработку",
    description="Owner/admin. status ∈ {approved, rejected}. Повторное рассмотрение уже "
    "рассмотренной заявки → 409 OVERTIME_ALREADY_REVIEWED.",
)
async def review_overtime_request(
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    body: OvertimeReviewRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    from src.app.models.audit_log import AuditAction, AuditResource
    from src.app.services import audit as audit_service

    overtime_request = await overtime_service.review_overtime_request(
        session,
        org_id,
        request_id,
        user.id,
        status=body.status,
        review_comment=body.review_comment,
    )
    await audit_service.record(
        session,
        action=AuditAction.overtime_review,
        resource_type=AuditResource.overtime,
        organization_id=org_id,
        actor_user_id=user.id,
        resource_id=overtime_request.id,
        summary={
            "status": overtime_request.status.value,
            "shift_id": str(overtime_request.shift_id),
        },
    )
    await session.commit()
    return ApiResponse.success(
        OvertimeReviewResponse(
            id=str(overtime_request.id),
            shift_id=str(overtime_request.shift_id),
            minutes=overtime_request.minutes,
            comment=overtime_request.comment,
            status=overtime_request.status.value,
            review_comment=overtime_request.review_comment,
            reviewed_at=overtime_request.reviewed_at,
            created_at=overtime_request.created_at,
        ).model_dump(mode="json")
    )
