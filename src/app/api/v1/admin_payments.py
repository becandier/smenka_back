import uuid
from datetime import datetime

from fastapi import APIRouter, Query

from src.app.api.deps import SessionDep, SuperAdminDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.billing import (
    AdminPaymentCreatedBy,
    AdminPaymentListResponse,
    AdminPaymentTotals,
)
from src.app.schemas.billing import (
    AdminPaymentRow as AdminPaymentRowSchema,
)
from src.app.services import billing as billing_service
from src.app.services.billing import AdminPaymentRow
from src.app.services.shift import ensure_utc

router = APIRouter(prefix="/admin", tags=["admin-payments"])


def _row_to_response(row: AdminPaymentRow) -> dict[str, object]:
    payment = row.payment
    created_by = None
    if row.created_by is not None:
        created_by = AdminPaymentCreatedBy(
            id=str(row.created_by.id),
            email=row.created_by.email,
            name=row.created_by.name,
        )
    return AdminPaymentRowSchema(
        id=str(payment.id),
        kind=payment.kind,
        plan_code=payment.plan_code,
        plan_name=row.plan_name,
        months=payment.months,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        status=payment.status,
        is_test=payment.is_test,
        paid_at=payment.paid_at,
        applied_at=payment.applied_at,
        created_at=payment.created_at,
        organization_id=str(payment.organization_id),
        organization_name=row.organization_name,
        created_by=created_by,
    ).model_dump(mode="json")


@router.get(
    "/payments",
    summary="Платёжный реестр платформы (super_admin)",
    description=(
        "Все онлайн-платежи (extend/upgrade через ЮKassa) с фильтрами. `totals` — сумма "
        "успешных платежей и их число с учётом organization_id/date_from/date_to, но всегда "
        "без тестовых платежей (is_test=false) и всегда только succeeded — не зависит от "
        "переданных status/is_test."
    ),
)
async def list_admin_payments(
    user: SuperAdminDep,
    session: SessionDep,
    status: str | None = Query(None, description="pending/succeeded/canceled/refunded"),
    organization_id: uuid.UUID | None = Query(None),
    is_test: bool | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    rows, total, totals = await billing_service.list_admin_payments(
        session,
        status=status,
        organization_id=organization_id,
        is_test=is_test,
        date_from=ensure_utc(date_from) if date_from is not None else None,
        date_to=ensure_utc(date_to) if date_to is not None else None,
        limit=limit,
        offset=offset,
    )
    return ApiResponse.success(
        AdminPaymentListResponse(
            items=[_row_to_response(r) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
            totals=AdminPaymentTotals(**totals),
        ).model_dump(mode="json")
    )
