import uuid

from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.subscription import SubscriptionResponse
from src.app.services import organization as org_service
from src.app.services import subscription as subscription_service
from src.app.services.common import ensure_admin_or_owner

router = APIRouter(prefix="/organizations/{org_id}", tags=["subscriptions"])


@router.get(
    "/subscription",
    summary="Состояние подписки организации",
    description=(
        "Эффективный статус, границы периода, grace, лимиты/фичи (эффективные — "
        "в trialing от premium), usage. Доступно владельцу и admin-участнику "
        "(не employee)."
    ),
)
async def get_organization_subscription(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id, message="Доступно владельцу и admin")
    payload = await subscription_service.build_subscription_payload(session, org_id)
    return ApiResponse.success(SubscriptionResponse(**payload).model_dump(mode="json"))
