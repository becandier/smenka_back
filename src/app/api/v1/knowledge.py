"""Эндпоинты базы знаний организации (knowledge_base).

Управление узлами/ACL — owner/admin/super_admin; чтение дерева и деталей —
дополнительно employee с ACL-фильтрацией. Пути без хвостового слэша,
`{org_id}` во всех путях; конверт {data,error}.
"""

import uuid
from typing import Any

from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.knowledge import KnowledgeNode, KnowledgeNodeAccess
from src.app.schemas.base import ApiResponse
from src.app.schemas.knowledge import (
    AccessReplaceRequest,
    AccessResponse,
    AccessRuleResponse,
    Breadcrumb,
    NodeCreate,
    NodeDetailResponse,
    NodeResponse,
    NodeUpdate,
    ReorderRequest,
)
from src.app.services import knowledge as knowledge_service

router = APIRouter(prefix="/organizations/{org_id}", tags=["knowledge"])


def _node_to_response(node: KnowledgeNode) -> NodeResponse:
    return NodeResponse(
        id=str(node.id),
        parent_id=str(node.parent_id) if node.parent_id is not None else None,
        kind=node.kind.value,
        title=node.title,
        icon=node.icon,
        position=node.position,
        all_members=node.all_members,
        content=node.content,
        created_at=node.created_at,
        updated_at=node.updated_at,
    )


def _detail_to_response(
    node: KnowledgeNode,
    breadcrumbs: list[tuple[uuid.UUID, str]],
    content: list[dict[str, Any]] | None,
) -> NodeDetailResponse:
    return NodeDetailResponse(
        id=str(node.id),
        parent_id=str(node.parent_id) if node.parent_id is not None else None,
        kind=node.kind.value,
        title=node.title,
        icon=node.icon,
        position=node.position,
        all_members=node.all_members,
        created_at=node.created_at,
        updated_at=node.updated_at,
        breadcrumbs=[Breadcrumb(id=str(nid), title=title) for nid, title in breadcrumbs],
        content=content,
    )


def _access_to_response(
    all_members: bool,
    rules: list[KnowledgeNodeAccess],
) -> AccessResponse:
    return AccessResponse(
        all_members=all_members,
        rules=[
            AccessRuleResponse(
                id=str(rule.id),
                subject_type=rule.subject_type.value,
                role_id=str(rule.role_id) if rule.role_id is not None else None,
                member_user_id=str(rule.member_user_id)
                if rule.member_user_id is not None
                else None,
                effect=rule.effect.value,
            )
            for rule in rules
        ],
    )


def _build_tree_payload(
    nodes_by_id: dict[uuid.UUID, KnowledgeNode],
    children: dict[uuid.UUID | None, list[uuid.UUID]],
    visible: set[uuid.UUID],
    parent_id: uuid.UUID | None,
    include_all_members: bool,
) -> list[dict[str, Any]]:
    """Рекурсивно собирает дерево; all_members — только для управляющих ролей."""
    items: list[dict[str, Any]] = []
    for child_id in children.get(parent_id, []):
        if child_id not in visible:
            continue
        node = nodes_by_id[child_id]
        item: dict[str, Any] = {
            "id": str(node.id),
            "kind": node.kind.value,
            "title": node.title,
            "icon": node.icon,
            "position": node.position,
        }
        if include_all_members:
            item["all_members"] = node.all_members
        item["children"] = _build_tree_payload(
            nodes_by_id, children, visible, child_id, include_all_members
        )
        items.append(item)
    return items


# --- Узлы --------------------------------------------------------------------
@router.post(
    "/knowledge/nodes",
    status_code=201,
    summary="Создать узел базы знаний",
    description="Раздел или страница в дереве org. Owner/admin/super_admin.",
)
async def create_node(
    org_id: uuid.UUID,
    body: NodeCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    node = await knowledge_service.create_node(
        session,
        org_id,
        user,
        parent_id=body.parent_id,
        kind=body.kind,
        title=body.title,
        icon=body.icon,
        position=body.position,
    )
    await session.commit()
    return ApiResponse.success(_node_to_response(node).model_dump(mode="json"))


@router.get(
    "/knowledge/nodes",
    summary="Дерево базы знаний",
    description=(
        "Вложенное дерево разделов/страниц. Owner/admin/super_admin — всё дерево; "
        "employee — отфильтровано по эффективному ACL (доступные узлы + разделы-предки)."
    ),
)
async def get_tree(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    ordered, children, visible, include_all_members = await knowledge_service.get_tree(
        session, org_id, user
    )
    nodes_by_id = {node.id: node for node in ordered}
    items = _build_tree_payload(nodes_by_id, children, visible, None, include_all_members)
    return ApiResponse.success({"items": items})


@router.put(
    "/knowledge/nodes/reorder",
    summary="Переупорядочить сиблингов",
    description="Полная замена порядка детей одного родителя. Owner/admin/super_admin.",
)
async def reorder_nodes(
    org_id: uuid.UUID,
    body: ReorderRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await knowledge_service.reorder_nodes(
        session,
        org_id,
        user,
        parent_id=body.parent_id,
        ordered_ids=body.ordered_ids,
    )
    await session.commit()
    return ApiResponse.success(None)


@router.get(
    "/knowledge/nodes/{node_id}",
    summary="Деталь узла",
    description=(
        "Узел с breadcrumbs; для страницы — блоки с presigned-обогащением. "
        "Owner/admin/super_admin всегда; employee — только при эффективном allow (иначе 404)."
    ),
)
async def get_node_detail(
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    node, breadcrumbs, content = await knowledge_service.get_node_detail(
        session, org_id, node_id, user
    )
    return ApiResponse.success(
        _detail_to_response(node, breadcrumbs, content).model_dump(mode="json")
    )


@router.patch(
    "/knowledge/nodes/{node_id}",
    summary="Обновить узел",
    description=(
        "Partial-обновление: title/icon/all_members/content/parent_id/position. "
        "content — только для страницы; пересчёт привязок файлов в той же транзакции. "
        "Owner/admin/super_admin."
    ),
)
async def update_node(
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    body: NodeUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    fields: dict[str, Any] = {}
    provided = body.model_fields_set
    if "title" in provided:
        fields["title"] = body.title
    if "icon" in provided:
        fields["icon"] = body.icon
    if "all_members" in provided:
        fields["all_members"] = body.all_members
    if "content" in provided:
        fields["content"] = (
            [block.model_dump(mode="json") for block in body.content]
            if body.content is not None
            else None
        )
    if "parent_id" in provided:
        fields["parent_id"] = body.parent_id
    if "position" in provided:
        fields["position"] = body.position

    node, breadcrumbs, content = await knowledge_service.update_node(
        session, org_id, node_id, user, fields=fields
    )
    await session.commit()
    return ApiResponse.success(
        _detail_to_response(node, breadcrumbs, content).model_dump(mode="json")
    )


@router.delete(
    "/knowledge/nodes/{node_id}",
    summary="Удалить узел и поддерево",
    description=(
        "Каскадно удаляет потомков, их ACL и привязки; файлы поддерева — из S3 и "
        "реестра files до каскада (приватность). Owner/admin/super_admin."
    ),
)
async def delete_node(
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await knowledge_service.delete_node(session, org_id, node_id, user)
    await session.commit()
    return ApiResponse.success(None)


# --- ACL ---------------------------------------------------------------------
@router.get(
    "/knowledge/nodes/{node_id}/access",
    summary="Правила доступа узла",
    description="Собственные ACL-правила узла + all_members. Owner/admin/super_admin.",
)
async def get_access(
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    all_members, rules = await knowledge_service.get_access(session, org_id, node_id, user)
    return ApiResponse.success(_access_to_response(all_members, rules).model_dump(mode="json"))


@router.put(
    "/knowledge/nodes/{node_id}/access",
    summary="Заменить правила доступа",
    description="Bulk-замена набора правил + all_members атомарно. Owner/admin/super_admin.",
)
async def replace_access(
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    body: AccessReplaceRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    all_members, rules = await knowledge_service.replace_access(
        session,
        org_id,
        node_id,
        user,
        all_members=body.all_members,
        rules=body.rules,
    )
    await session.commit()
    return ApiResponse.success(_access_to_response(all_members, rules).model_dump(mode="json"))
