import uuid
from datetime import datetime as dt_datetime

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.adjustment import PayrollAdjustment
from src.app.models.organization import OrganizationMember
from src.app.models.user import User
from src.app.schemas.adjustment import (
    AdjustmentCreate,
    AdjustmentDeletedResponse,
    AdjustmentListResponse,
    AdjustmentResponse,
    AdjustmentUpdate,
    MyAdjustmentListResponse,
    MyAdjustmentResponse,
)
from src.app.schemas.base import ApiResponse
from src.app.services import adjustment as adjustment_service

router = APIRouter(prefix="/organizations/{org_id}", tags=["adjustments"])


async def _build_adjustment_payloads(
    session: AsyncSession,
    adjustments: list[PayrollAdjustment],
) -> list[AdjustmentResponse]:
    """Обогатить начисления именем сотрудника (member → user) и назначившего, без N+1."""
    if not adjustments:
        return []
    member_ids = {a.member_id for a in adjustments}
    members_result = await session.execute(
        select(
            OrganizationMember.id,
            OrganizationMember.user_id,
            OrganizationMember.display_name,
        ).where(OrganizationMember.id.in_(member_ids))
    )
    members_rows = members_result.all()
    user_by_member = {row.id: row.user_id for row in members_rows}
    display_name_by_member = {row.id: row.display_name for row in members_rows}

    user_ids = set(user_by_member.values()) | {a.created_by_user_id for a in adjustments}
    users_result = await session.execute(select(User.id, User.name).where(User.id.in_(user_ids)))
    name_by_user = dict(users_result.tuples().all())

    payloads: list[AdjustmentResponse] = []
    for a in adjustments:
        uid = user_by_member.get(a.member_id)
        user_name = name_by_user.get(uid, "Unknown") if uid is not None else "Unknown"
        payloads.append(
            AdjustmentResponse(
                id=str(a.id),
                organization_id=str(a.organization_id),
                member_id=str(a.member_id),
                user_id=str(uid) if uid is not None else "",
                user_name=user_name,
                display_name=display_name_by_member.get(a.member_id),
                shift_id=str(a.shift_id) if a.shift_id is not None else None,
                amount_minor=a.amount_minor,
                currency=a.currency,
                reason=a.reason,
                comment=a.comment,
                occurred_at=a.occurred_at,
                created_by_user_id=str(a.created_by_user_id),
                created_by_name=name_by_user.get(a.created_by_user_id, "Unknown"),
                created_at=a.created_at,
            )
        )
    return payloads


def _my_adjustment_to_response(adjustment: PayrollAdjustment) -> MyAdjustmentResponse:
    return MyAdjustmentResponse(
        id=str(adjustment.id),
        amount_minor=adjustment.amount_minor,
        currency=adjustment.currency,
        reason=adjustment.reason,
        comment=adjustment.comment,
        occurred_at=adjustment.occurred_at,
        shift_id=str(adjustment.shift_id) if adjustment.shift_id is not None else None,
        created_at=adjustment.created_at,
    )


@router.post(
    "/adjustments",
    status_code=201,
    summary="Создать ручное начисление",
    description=(
        "Доплата (amount_minor > 0) или удержание (< 0) сотруднику, не привязанные "
        "к интервалу времени. occurred_at обязателен, если shift_id не передан; при "
        "переданном shift_id по умолчанию = started_at смены. Owner/admin."
    ),
)
async def create_adjustment(
    org_id: uuid.UUID,
    body: AdjustmentCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    adjustment = await adjustment_service.create_adjustment(
        session,
        org_id,
        user.id,
        member_id=body.member_id,
        amount_minor=body.amount_minor,
        currency=body.currency,
        reason=body.reason,
        occurred_at=body.occurred_at,
        shift_id=body.shift_id,
        comment=body.comment,
    )
    await session.commit()
    payloads = await _build_adjustment_payloads(session, [adjustment])
    return ApiResponse.success(payloads[0].model_dump(mode="json"))


@router.get(
    "/adjustments",
    summary="Список ручных начислений организации",
    description=(
        "Активные начисления под фильтром (member_id/shift_id/период), "
        "occurred_at DESC. Owner/admin."
    ),
)
async def list_adjustments(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    member_id: uuid.UUID | None = Query(None, description="Фильтр по сотруднику"),
    shift_id: uuid.UUID | None = Query(None, description="Фильтр по смене"),
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница по occurred_at, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница по occurred_at, включительно (UTC)"
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    adjustments, total = await adjustment_service.list_adjustments(
        session,
        org_id,
        user.id,
        member_id=member_id,
        shift_id=shift_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    items = await _build_adjustment_payloads(session, adjustments)
    return ApiResponse.success(
        AdjustmentListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.patch(
    "/adjustments/{adjustment_id}",
    summary="Исправить начисление",
    description="Правка записи начисления. member_id неизменен. Owner/admin.",
)
async def update_adjustment(
    org_id: uuid.UUID,
    adjustment_id: uuid.UUID,
    body: AdjustmentUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    adjustment = await adjustment_service.update_adjustment(
        session,
        org_id,
        adjustment_id,
        user.id,
        body.model_dump(exclude_unset=True),
    )
    await session.commit()
    payloads = await _build_adjustment_payloads(session, [adjustment])
    return ApiResponse.success(payloads[0].model_dump(mode="json"))


@router.delete(
    "/adjustments/{adjustment_id}",
    summary="Отменить начисление (soft-delete)",
    description="Owner/admin. Начисление перестаёт учитываться в payroll.",
)
async def delete_adjustment(
    org_id: uuid.UUID,
    adjustment_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await adjustment_service.delete_adjustment(session, org_id, adjustment_id, user.id)
    await session.commit()
    return ApiResponse.success(AdjustmentDeletedResponse(deleted=True).model_dump())


@router.get(
    "/my-adjustments",
    summary="Мои начисления",
    description=(
        "Свои активные начисления за период. Только участник (employee/admin); owner — 403."
    ),
)
async def my_adjustments(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница по occurred_at, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница по occurred_at, включительно (UTC)"
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    adjustments, total = await adjustment_service.list_my_adjustments(
        session,
        org_id,
        user.id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return ApiResponse.success(
        MyAdjustmentListResponse(
            items=[_my_adjustment_to_response(a) for a in adjustments],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )
