import uuid
from typing import Any

from fastapi import APIRouter, Query

from src.app.api.deps import SessionDep, SuperAdminDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.subscription import (
    AdminSubscriptionListResponse,
    AdminSubscriptionUsage,
    PlanLimits,
    SubscriptionEventActor,
    SubscriptionEventListResponse,
    SubscriptionEventResponse,
    SubscriptionExtendRequest,
    SubscriptionPatchRequest,
    SubscriptionResponse,
    SubscriptionSummaryByStatus,
    SubscriptionSummaryResponse,
)
from src.app.schemas.subscription import (
    AdminSubscriptionRow as AdminSubscriptionRowSchema,
)
from src.app.services import entitlements
from src.app.services import subscription as subscription_service
from src.app.services.subscription import AdminSubscriptionRow

router = APIRouter(prefix="/admin", tags=["admin-subscriptions"])


def _registry_row_to_response(row: AdminSubscriptionRow) -> dict[str, Any]:
    return AdminSubscriptionRowSchema(
        organization_id=str(row.org.id),
        organization_name=row.org.name,
        owner_email=row.owner_email,
        owner_login=row.owner_login,
        plan_code=row.sub.plan_code,
        plan_name=row.plan.name,
        status=row.status.value,
        trial_ends_at=row.sub.trial_ends_at,
        current_period_end=row.sub.current_period_end,
        grace_ends_at=entitlements.grace_ends_at(row.sub),
        days_left=entitlements.days_left(row.sub),
        limits=PlanLimits(
            max_employees=row.effective_plan.max_employees,
            max_locations=row.effective_plan.max_locations,
        ),
        usage=AdminSubscriptionUsage(employees=row.employees, locations=row.locations),
        note=row.sub.note,
        updated_at=row.sub.updated_at,
    ).model_dump(mode="json")


@router.get(
    "/subscriptions",
    summary="Реестр подписок (super_admin)",
    description=(
        "Все подписки (организации с `is_deleted=true` исключены). Фильтры: "
        "status (эффективный, множественный), plan_code, q (ILIKE по названию "
        "организации), expiring_soon (эффективный статус trialing/active и "
        "0 <= days_left <= 7, посчитано на всей таблице — тем же предикатом, что "
        "expiring_in_7_days в /subscriptions/summary). Сортировка по умолчанию — "
        "ближайшее окончание сверху."
    ),
)
async def list_admin_subscriptions(
    user: SuperAdminDep,
    session: SessionDep,
    status: list[str] | None = Query(None, description="Эффективный статус, можно несколько раз"),
    plan_code: str | None = Query(None),
    q: str | None = Query(None, description="Поиск по названию организации (ILIKE)"),
    expiring_soon: bool = Query(
        False,
        description="Только организации, истекающие в ближайшие 7 дней (trialing/active, "
        "0 <= days_left <= 7), отфильтровано на всей таблице",
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    sort: str | None = Query(
        None,
        description="current_period_end | organization_name; не передан — по умолчанию "
        "ближайшее окончание сверху",
    ),
) -> ApiResponse:
    rows, total = await subscription_service.list_admin_subscriptions(
        session,
        statuses=status,
        plan_code=plan_code,
        q=q,
        expiring_soon=expiring_soon,
        limit=limit,
        offset=offset,
        sort=sort,
    )
    return ApiResponse.success(
        AdminSubscriptionListResponse(
            items=[_registry_row_to_response(r) for r in rows],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/subscriptions/summary",
    summary="Сводка по монетизации (super_admin)",
    description="Разбивка по эффективному статусу и по тарифу, MRR (сумма цен планов "
    "организаций со статусом active), число истекающих в ближайшие 7 дней.",
)
async def get_subscriptions_summary(
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    data = await subscription_service.get_summary(session)
    return ApiResponse.success(
        SubscriptionSummaryResponse(
            by_status=SubscriptionSummaryByStatus(**data["by_status"]),
            by_plan=data["by_plan"],
            mrr_minor=data["mrr_minor"],
            expiring_in_7_days=data["expiring_in_7_days"],
        ).model_dump(mode="json")
    )


@router.patch(
    "/organizations/{org_id}/subscription",
    summary="Ручная правка подписки (super_admin)",
    description=(
        "Все поля опциональны, применяются только переданные. plan_code должен "
        "существовать и быть активным. status=active требует current_period_end "
        "(уже установленного или переданного в этом же запросе)."
    ),
)
async def patch_organization_subscription(
    org_id: uuid.UUID,
    body: SubscriptionPatchRequest,
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    # Возврат patch_subscription не используется напрямую в ответе — payload
    # пересобирается через build_subscription_payload, чтобы отдать тот же
    # контракт, что и GET .../subscription (эффективные limits/features,
    # grace_ends_at, usage).
    await subscription_service.patch_subscription(
        session,
        org_id,
        user.id,
        plan_code=body.plan_code,
        status=body.status,
        trial_ends_at=body.trial_ends_at,
        current_period_start=body.current_period_start,
        current_period_end=body.current_period_end,
        note=body.note,
    )
    await session.commit()
    payload = await subscription_service.build_subscription_payload(session, org_id)
    return ApiResponse.success(SubscriptionResponse(**payload).model_dump(mode="json"))


@router.post(
    "/organizations/{org_id}/subscription/extend",
    summary="Продлить подписку — «оплачено» (super_admin)",
    description=(
        "Основная кнопка супер-админа: период продлевается от большей из двух "
        "дат (сегодня или текущий конец периода). amount_minor по умолчанию — "
        "цена плана × months. Сбрасывает антидубль уведомлений об истечении."
    ),
)
async def extend_organization_subscription(
    org_id: uuid.UUID,
    body: SubscriptionExtendRequest,
    user: SuperAdminDep,
    session: SessionDep,
) -> ApiResponse:
    await subscription_service.extend_subscription(
        session,
        org_id,
        user.id,
        months=body.months,
        plan_code=body.plan_code,
        amount_minor=body.amount_minor,
        note=body.note,
    )
    await session.commit()
    payload = await subscription_service.build_subscription_payload(session, org_id)
    return ApiResponse.success(SubscriptionResponse(**payload).model_dump(mode="json"))


@router.get(
    "/organizations/{org_id}/subscription/events",
    summary="История подписки (super_admin)",
    description="Append-only журнал изменений подписки, новые сверху.",
)
async def list_organization_subscription_events(
    org_id: uuid.UUID,
    user: SuperAdminDep,
    session: SessionDep,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    events, total, actors = await subscription_service.list_events(
        session, org_id, limit=limit, offset=offset
    )
    items = []
    for e in events:
        actor = None
        if e.actor_user_id is not None:
            actor_user = actors.get(e.actor_user_id)
            if actor_user is not None:
                actor = SubscriptionEventActor(
                    id=str(actor_user.id),
                    email=actor_user.email,
                    name=actor_user.name,
                )
        items.append(
            SubscriptionEventResponse(
                id=str(e.id),
                type=e.type,
                from_plan_code=e.from_plan_code,
                to_plan_code=e.to_plan_code,
                from_status=e.from_status,
                to_status=e.to_status,
                period_end_before=e.period_end_before,
                period_end_after=e.period_end_after,
                months=e.months,
                amount_minor=e.amount_minor,
                note=e.note,
                actor=actor,
                created_at=e.created_at,
            ).model_dump(mode="json")
        )
    return ApiResponse.success(
        SubscriptionEventListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )
