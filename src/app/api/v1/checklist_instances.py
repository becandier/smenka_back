import uuid
from typing import Any

from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.checklist import ChecklistInstance, ChecklistInstanceItem
from src.app.schemas.base import ApiResponse
from src.app.schemas.checklist import (
    ChecklistInstanceDetailResponse,
    ChecklistInstanceListResponse,
    ChecklistInstanceResponse,
    InstanceItemResponse,
    InstanceItemUpdate,
    ItemsSummary,
)
from src.app.services import checklist_instance as instance_service

router = APIRouter(prefix="/shifts/{shift_id}/checklists", tags=["checklist-instances"])


def _instance_to_response(
    instance: ChecklistInstance, total: int, completed: int
) -> dict[str, Any]:
    return ChecklistInstanceResponse(
        id=str(instance.id),
        name=instance.name,
        type=instance.type.value,
        is_required=instance.is_required,
        status=instance.status.value,
        completed_at=instance.completed_at,
        items_summary=ItemsSummary(total=total, completed=completed),
        created_at=instance.created_at,
    ).model_dump(mode="json")


def _item_to_response(item: ChecklistInstanceItem) -> dict[str, Any]:
    return InstanceItemResponse(
        id=str(item.id),
        text=item.text,
        is_required=item.is_required,
        position=item.position,
        is_completed=item.is_completed,
        comment=item.comment,
        completed_at=item.completed_at,
        change_count=item.change_count,
    ).model_dump(mode="json")


def _instance_detail_to_response(instance: ChecklistInstance) -> dict[str, Any]:
    return ChecklistInstanceDetailResponse(
        id=str(instance.id),
        name=instance.name,
        type=instance.type.value,
        is_required=instance.is_required,
        status=instance.status.value,
        completed_at=instance.completed_at,
        created_at=instance.created_at,
        items=[_item_to_response(it) for it in sorted(instance.items, key=lambda x: x.position)],
    ).model_dump(mode="json")


@router.get(
    "",
    summary="Чек-листы смены",
    description="Список экземпляров чек-листов смены со сводкой. Доступно владельцу "
    "смены, владельцу и админам организации.",
)
async def list_shift_checklists(
    shift_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    triples = await instance_service.get_shift_checklists(session, shift_id, user.id)
    return ApiResponse.success(
        ChecklistInstanceListResponse(
            items=[_instance_to_response(i, t, c) for i, t, c in triples],
        ).model_dump(mode="json")
    )


@router.get(
    "/{instance_id}",
    summary="Детали экземпляра чек-листа",
    description="Экземпляр с упорядоченными пунктами.",
)
async def get_instance(
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    instance = await instance_service.get_instance_detail(
        session,
        shift_id,
        instance_id,
        user.id,
    )
    return ApiResponse.success(_instance_detail_to_response(instance))


@router.patch(
    "/{instance_id}/items/{item_id}",
    summary="Обновить пункт",
    description="Отметить/снять пункт и добавить комментарий. Только владелец смены, "
    "только пока смена активна.",
)
async def update_item(
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    item_id: uuid.UUID,
    body: InstanceItemUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    item = await instance_service.update_instance_item(
        session,
        shift_id,
        instance_id,
        item_id,
        user.id,
        is_completed=body.is_completed,
        comment=body.comment,
    )
    await session.commit()
    return ApiResponse.success(_item_to_response(item))
