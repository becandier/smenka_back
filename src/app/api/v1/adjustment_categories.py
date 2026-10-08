import uuid

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.adjustment import PayrollAdjustmentCategory
from src.app.schemas.adjustment import (
    AdjustmentCategoryCreate,
    AdjustmentCategoryDeletedResponse,
    AdjustmentCategoryListResponse,
    AdjustmentCategoryResponse,
    AdjustmentCategoryUpdate,
)
from src.app.schemas.base import ApiResponse
from src.app.services import adjustment as adjustment_service

router = APIRouter(prefix="/organizations/{org_id}", tags=["adjustments"])


async def _build_category_payloads(
    session: AsyncSession,
    categories: list[PayrollAdjustmentCategory],
) -> list[AdjustmentCategoryResponse]:
    """Категории + число неотменённых начислений по каждой одним запросом (без N+1)."""
    counts = await adjustment_service.count_adjustments_by_category(
        session, [c.id for c in categories]
    )
    return [
        AdjustmentCategoryResponse(
            id=str(c.id),
            organization_id=str(c.organization_id),
            name=c.name,
            is_deleted=c.is_deleted,
            deleted_at=c.deleted_at,
            created_at=c.created_at,
            adjustments_count=counts.get(c.id, 0),
        )
        for c in categories
    ]


@router.get(
    "/adjustment-categories",
    summary="Категории ручных начислений",
    description=(
        "Справочник категорий начислений организации, сортировка по lower(name), без "
        "пагинации. include_deleted=true — вместе с удалёнными. Owner/admin."
    ),
)
async def list_adjustment_categories(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    include_deleted: bool = Query(False, description="Включить удалённые категории"),
) -> ApiResponse:
    categories = await adjustment_service.list_categories(
        session, org_id, user.id, include_deleted=include_deleted
    )
    items = await _build_category_payloads(session, categories)
    return ApiResponse.success(AdjustmentCategoryListResponse(items=items).model_dump(mode="json"))


@router.post(
    "/adjustment-categories",
    status_code=201,
    summary="Создать категорию начислений",
    description=(
        "Имя уникально среди неудалённых категорий организации без учёта регистра "
        "(дубль — 409 ADJUSTMENT_CATEGORY_DUPLICATE). Owner/admin."
    ),
)
async def create_adjustment_category(
    org_id: uuid.UUID,
    body: AdjustmentCategoryCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    category = await adjustment_service.create_category(session, org_id, user.id, name=body.name)
    await session.commit()
    payloads = await _build_category_payloads(session, [category])
    return ApiResponse.success(payloads[0].model_dump(mode="json"))


@router.patch(
    "/adjustment-categories/{category_id}",
    summary="Переименовать категорию начислений",
    description=(
        "Новое имя отражается во всех начислениях и отчётах (ссылка, не снимок). "
        "Удалённую категорию править нельзя — 404. Owner/admin."
    ),
)
async def update_adjustment_category(
    org_id: uuid.UUID,
    category_id: uuid.UUID,
    body: AdjustmentCategoryUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    category = await adjustment_service.update_category(
        session, org_id, category_id, user.id, name=body.name
    )
    await session.commit()
    payloads = await _build_category_payloads(session, [category])
    return ApiResponse.success(payloads[0].model_dump(mode="json"))


@router.delete(
    "/adjustment-categories/{category_id}",
    summary="Удалить категорию начислений (soft-delete)",
    description=(
        "Категория уходит из выбора; уже созданные начисления сохраняют её и "
        "показывают по имени. Восстановления нет (ADR-003). Owner/admin."
    ),
)
async def delete_adjustment_category(
    org_id: uuid.UUID,
    category_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await adjustment_service.delete_category(session, org_id, category_id, user.id)
    await session.commit()
    return ApiResponse.success(AdjustmentCategoryDeletedResponse(deleted=True).model_dump())
