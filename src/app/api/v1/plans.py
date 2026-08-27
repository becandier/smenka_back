from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.plan import Plan
from src.app.schemas.base import ApiResponse
from src.app.schemas.subscription import PlanFeatures, PlanLimits, PlanListResponse, PlanResponse
from src.app.services import subscription as subscription_service

router = APIRouter(tags=["plans"])


def _plan_to_response(plan: Plan) -> dict[str, object]:
    return PlanResponse(
        code=plan.code,
        name=plan.name,
        price_minor=plan.price_minor,
        currency=plan.currency,
        limits=PlanLimits(max_employees=plan.max_employees, max_locations=plan.max_locations),
        features=PlanFeatures(fines=plan.feature_fines, test_import=plan.feature_test_import),
        sort_order=plan.sort_order,
    ).model_dump(mode="json")


@router.get(
    "/plans",
    summary="Витрина тарифов",
    description="Активные тарифы (`is_active=true`), по `sort_order`. Доступно любому "
    "авторизованному пользователю.",
)
async def list_plans(
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    plans = await subscription_service.list_plans(session)
    return ApiResponse.success(
        PlanListResponse(items=[_plan_to_response(p) for p in plans]).model_dump(mode="json")
    )
