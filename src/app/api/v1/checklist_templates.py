import uuid
from typing import Any

from fastapi import APIRouter, Query

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.checklist import ChecklistTemplate, ChecklistTemplateItem
from src.app.schemas.base import ApiResponse
from src.app.schemas.checklist import (
    ItemsReorderRequest,
    TemplateCreate,
    TemplateDetailResponse,
    TemplateItemCreate,
    TemplateItemResponse,
    TemplateItemUpdate,
    TemplateListResponse,
    TemplateResponse,
    TemplateUpdate,
)
from src.app.services import checklist_template as tpl_service

router = APIRouter(
    prefix="/organizations/{org_id}/checklist-templates",
    tags=["checklist-templates"],
)


def _template_to_response(template: ChecklistTemplate, items_count: int) -> dict[str, Any]:
    return TemplateResponse(
        id=str(template.id),
        name=template.name,
        type=template.type.value,
        is_required=template.is_required,
        items_count=items_count,
        is_archived=template.is_archived,
        created_at=template.created_at,
        updated_at=template.updated_at,
    ).model_dump(mode="json")


def _item_to_response(item: ChecklistTemplateItem) -> dict[str, Any]:
    return TemplateItemResponse(
        id=str(item.id),
        text=item.text,
        is_required=item.is_required,
        position=item.position,
        photo_requirement=item.photo_requirement.value,
        photo_source=item.photo_source.value,
    ).model_dump(mode="json")


def _template_detail_to_response(template: ChecklistTemplate) -> dict[str, Any]:
    return TemplateDetailResponse(
        id=str(template.id),
        name=template.name,
        type=template.type.value,
        is_required=template.is_required,
        is_archived=template.is_archived,
        created_at=template.created_at,
        updated_at=template.updated_at,
        items=[_item_to_response(it) for it in sorted(template.items, key=lambda x: x.position)],
    ).model_dump(mode="json")


@router.post(
    "",
    status_code=201,
    summary="Создать шаблон чек-листа",
    description="Создаёт шаблон чек-листа. Доступно владельцу и админам.",
)
async def create_template(
    org_id: uuid.UUID,
    body: TemplateCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    template = await tpl_service.create_template(
        session,
        org_id,
        body.name,
        body.type,
        body.is_required,
        user.id,
    )
    await session.commit()
    return ApiResponse.success(_template_to_response(template, 0))


@router.get(
    "",
    summary="Список шаблонов",
    description=(
        "Список шаблонов организации. По умолчанию архивные скрыты. Доступно владельцу и админам."
    ),
)
async def list_templates(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    include_archived: bool = Query(False, description="Включить архивные шаблоны"),
) -> ApiResponse:
    templates = await tpl_service.get_templates(
        session,
        org_id,
        user.id,
        include_archived=include_archived,
    )
    return ApiResponse.success(
        TemplateListResponse(
            items=[_template_to_response(t, c) for t, c in templates],
        ).model_dump(mode="json")
    )


@router.get(
    "/{template_id}",
    summary="Детали шаблона с пунктами",
    description="Возвращает шаблон с упорядоченными пунктами. Доступно владельцу и админам.",
)
async def get_template_detail(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    template = await tpl_service.get_template_detail(
        session,
        org_id,
        template_id,
        user.id,
    )
    return ApiResponse.success(_template_detail_to_response(template))


@router.patch(
    "/{template_id}",
    summary="Обновить шаблон",
    description=(
        "Обновляет поля шаблона (передавайте только изменяемые). Доступно владельцу и админам."
    ),
)
async def update_template(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    body: TemplateUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    template, items_count = await tpl_service.update_template(
        session,
        org_id,
        template_id,
        user.id,
        name=body.name,
        type_=body.type,
        is_required=body.is_required,
    )
    await session.commit()
    return ApiResponse.success(_template_to_response(template, items_count))


@router.delete(
    "/{template_id}",
    summary="Архивировать шаблон",
    description=(
        "Помечает шаблон как архивный (is_archived=true). Для назначения новым "
        "сменам шаблон больше не используется. Существующие экземпляры в активных "
        "сменах сохраняются."
    ),
)
async def delete_template(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await tpl_service.delete_template(session, org_id, template_id, user.id)
    await session.commit()
    return ApiResponse.success({"message": "Шаблон архивирован"})


@router.post(
    "/{template_id}/items",
    status_code=201,
    summary="Добавить пункт",
    description="Добавляет новый пункт в конец шаблона.",
)
async def add_item(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    body: TemplateItemCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    item = await tpl_service.add_item(
        session,
        org_id,
        template_id,
        body.text,
        body.is_required,
        user.id,
        photo_requirement=body.photo_requirement,
        photo_source=body.photo_source,
    )
    await session.commit()
    return ApiResponse.success(_item_to_response(item))


@router.patch(
    "/{template_id}/items/{item_id}",
    summary="Обновить пункт",
)
async def update_item(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    item_id: uuid.UUID,
    body: TemplateItemUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    item = await tpl_service.update_item(
        session,
        org_id,
        template_id,
        item_id,
        user.id,
        text=body.text,
        is_required=body.is_required,
        photo_requirement=body.photo_requirement,
        photo_source=body.photo_source,
    )
    await session.commit()
    return ApiResponse.success(_item_to_response(item))


@router.delete(
    "/{template_id}/items/{item_id}",
    summary="Удалить пункт",
)
async def delete_item(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    item_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await tpl_service.delete_item(session, org_id, template_id, item_id, user.id)
    await session.commit()
    return ApiResponse.success({"message": "Пункт удалён"})


@router.put(
    "/{template_id}/items/reorder",
    summary="Изменить порядок пунктов",
    description=(
        "Принимает полный список UUID пунктов в нужном порядке. Должен содержать "
        "ВСЕ пункты шаблона без дубликатов."
    ),
)
async def reorder_items(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    body: ItemsReorderRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    ids = [uuid.UUID(s) for s in body.item_ids]
    items = await tpl_service.reorder_items(
        session,
        org_id,
        template_id,
        ids,
        user.id,
    )
    await session.commit()
    return ApiResponse.success({"items": [_item_to_response(it) for it in items]})
