import uuid

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.core.config import get_settings
from src.app.models.payment import Payment
from src.app.schemas.base import ApiResponse
from src.app.schemas.billing import (
    BillingCheckoutRequest,
    BillingCheckoutResponse,
    BillingConfigResponse,
    BillingOptionsResponse,
    BillingPaymentListResponse,
    BillingPaymentResponse,
)
from src.app.services import billing as billing_service
from src.app.services import organization as org_service
from src.app.services.common import ensure_admin_or_owner

router = APIRouter(tags=["billing"])
settings = get_settings()


async def _payment_response(session: AsyncSession, payment: Payment) -> dict[str, object]:
    plan_names = await billing_service.get_plan_names(session, {payment.plan_code})
    return BillingPaymentResponse(
        id=str(payment.id),
        kind=payment.kind,
        plan_code=payment.plan_code,
        plan_name=plan_names.get(payment.plan_code, payment.plan_code),
        months=payment.months,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        status=payment.status,
        is_test=payment.is_test,
        paid_at=payment.paid_at,
        applied_at=payment.applied_at,
        created_at=payment.created_at,
    ).model_dump(mode="json")


@router.get(
    "/billing/config",
    summary="Состояние платёжного модуля",
    description="enabled/mode/provider. Секреты не отдаются. Доступно любому авторизованному "
    "пользователю — админка рисует по mode бейдж «Тестовый режим оплаты».",
)
async def get_billing_config(user: CurrentUserDep) -> ApiResponse:
    data = billing_service.get_billing_config(settings)
    return ApiResponse.success(BillingConfigResponse(**data).model_dump(mode="json"))


@router.get(
    "/organizations/{org_id}/billing/options",
    summary="Витрина: что и почём можно оплатить",
    description="Продление (1/3/6 мес со скидкой 0/5/10%) по обоим тарифам + доступность и "
    "сумма апгрейда Стандарт→Премиум. Доступно владельцу и admin. Работает и в suspended — "
    "billing/* не подпадает под проверку активной подписки.",
)
async def get_billing_options(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id, message="Доступно владельцу и admin")
    data = await billing_service.get_billing_options(session, settings, org_id)
    return ApiResponse.success(BillingOptionsResponse(**data).model_dump(mode="json"))


@router.post(
    "/organizations/{org_id}/billing/checkout",
    summary="Создать платёж",
    description="Сумма пересчитывается на сервере — тело клиента на amount не влияет. "
    "Pending-платёж младше 15 минут с теми же параметрами переиспользуется вместо создания "
    "нового. Работает и в suspended.",
)
async def create_billing_checkout(
    org_id: uuid.UUID,
    body: BillingCheckoutRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id, message="Доступно владельцу и admin")
    payment = await billing_service.create_checkout(
        session,
        settings,
        org_id,
        user.id,
        kind=body.kind,
        plan_code=body.plan_code,
        months=body.months,
    )
    await session.commit()
    return ApiResponse.success(
        BillingCheckoutResponse(
            payment_id=str(payment.id),
            confirmation_url=payment.confirmation_url,
            amount_minor=payment.amount_minor,
            currency=payment.currency,
            status=payment.status,
        ).model_dump(mode="json")
    )


@router.get(
    "/organizations/{org_id}/billing/payments/{payment_id}",
    summary="Статус платежа",
    description="Если платёж всё ещё pending и с момента создания прошло больше 10 секунд — "
    "сервер сам опрашивает провайдера и применяет платёж тут же (страховка на случай "
    "опоздавшего/потерянного вебхука).",
)
async def get_billing_payment(
    org_id: uuid.UUID,
    payment_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id, message="Доступно владельцу и admin")
    payment = await billing_service.get_payment_for_org(session, settings, org_id, payment_id)
    return ApiResponse.success(await _payment_response(session, payment))


@router.get(
    "/organizations/{org_id}/billing/payments",
    summary="История платежей организации",
    description="Новые сверху.",
)
async def list_billing_payments(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id, message="Доступно владельцу и admin")
    payments, total = await billing_service.list_payments_for_org(
        session, org_id, limit=limit, offset=offset
    )
    plan_names = await billing_service.get_plan_names(session, {p.plan_code for p in payments})
    items = [
        BillingPaymentResponse(
            id=str(p.id),
            kind=p.kind,
            plan_code=p.plan_code,
            plan_name=plan_names.get(p.plan_code, p.plan_code),
            months=p.months,
            amount_minor=p.amount_minor,
            currency=p.currency,
            status=p.status,
            is_test=p.is_test,
            paid_at=p.paid_at,
            applied_at=p.applied_at,
            created_at=p.created_at,
        ).model_dump(mode="json")
        for p in payments
    ]
    return ApiResponse.success(
        BillingPaymentListResponse(
            items=items, total=total, limit=limit, offset=offset
        ).model_dump(mode="json")
    )
