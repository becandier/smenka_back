"""База знаний организации: дерево узлов, ACL-резолюция, привязка файлов.

ACL для employee считается обходом от узла вверх по `parent_id` с приоритетом
категорий: 1) персональное правило, 2) ролевое правило, 3) `all_members` на узле
или предке, 4) по умолчанию deny. Персональное всегда сильнее ролевого; внутри
категории ближайший узел перебивает дальний. owner/admin/super_admin игнорируют
ACL (полный доступ). Файлы страницы привязываются через `knowledge_node_files`
(паттерн checklist_photos): добавление — `is_attached=true` + строка реестра,
удаление — `is_attached=false` + `delete_file` (объект S3 и строка `files`).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.file import File, FileCategory
from src.app.models.knowledge import (
    KnowledgeAccessEffect,
    KnowledgeNode,
    KnowledgeNodeAccess,
    KnowledgeNodeFile,
    KnowledgeNodeKind,
    KnowledgeSubjectType,
)
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_role import OrganizationRole
from src.app.models.user import User, UserRole
from src.app.services import organization as org_service
from src.app.services.common import ensure_admin_or_owner
from src.app.services.file_storage import delete_file, presigned_urls_for

logger = get_logger(__name__)

_FILE_BLOCK_TYPES = frozenset({"image", "file"})


class KnowledgeError(Exception):
    """Доменная ошибка базы знаний. Маппится в {data,error} в main.py."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


# --- Доступ узлов / контекст зрителя ------------------------------------------
async def _get_node(
    session: AsyncSession,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> KnowledgeNode:
    query = select(KnowledgeNode).where(
        KnowledgeNode.id == node_id,
        KnowledgeNode.organization_id == org_id,
    )
    if for_update:
        query = query.with_for_update()
    node = (await session.execute(query)).scalar_one_or_none()
    if node is None:
        raise KnowledgeError("KNOWLEDGE_NODE_NOT_FOUND", "Узел базы знаний не найден", 404)
    return node


async def _resolve_viewer(
    session: AsyncSession,
    org: Organization,
    user: User,
) -> tuple[str, OrganizationMember | None]:
    """Возвращает ('manager', None) для owner/admin/super_admin либо ('employee', member).

    Для не-участника (и не super_admin) → FORBIDDEN: существование базы знаний не
    раскрываем сверх факта членства в org.
    """
    if org.owner_id == user.id or user.role == UserRole.super_admin:
        return "manager", None
    member = (
        await session.execute(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise KnowledgeError("FORBIDDEN", "Нет доступа к организации", 403)
    if member.role == MemberRole.admin:
        return "manager", None
    return "employee", member


# --- ACL-индекс и резолюция ---------------------------------------------------
@dataclass
class _AclIndex:
    nodes: dict[uuid.UUID, KnowledgeNode]
    children: dict[uuid.UUID | None, list[uuid.UUID]]
    member_rules: dict[uuid.UUID, dict[uuid.UUID, KnowledgeAccessEffect]]
    role_rules: dict[uuid.UUID, dict[uuid.UUID, KnowledgeAccessEffect]]


async def _load_acl_index(session: AsyncSession, org_id: uuid.UUID) -> _AclIndex:
    """Все узлы org + их ACL-правила одним проходом (без N+1 на резолюцию)."""
    nodes = list(
        (
            await session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.organization_id == org_id)
                .order_by(KnowledgeNode.position, KnowledgeNode.created_at)
            )
        )
        .scalars()
        .all()
    )
    nodes_by_id = {n.id: n for n in nodes}
    children: dict[uuid.UUID | None, list[uuid.UUID]] = defaultdict(list)
    for node in nodes:
        children[node.parent_id].append(node.id)

    member_rules: dict[uuid.UUID, dict[uuid.UUID, KnowledgeAccessEffect]] = defaultdict(dict)
    role_rules: dict[uuid.UUID, dict[uuid.UUID, KnowledgeAccessEffect]] = defaultdict(dict)
    if nodes_by_id:
        access_rows = (
            await session.execute(
                select(KnowledgeNodeAccess).where(
                    KnowledgeNodeAccess.node_id.in_(nodes_by_id.keys())
                )
            )
        ).scalars()
        for rule in access_rows:
            is_member = rule.subject_type == KnowledgeSubjectType.member
            is_role = rule.subject_type == KnowledgeSubjectType.role
            if is_member and rule.member_user_id is not None:
                member_rules[rule.node_id][rule.member_user_id] = rule.effect
            elif is_role and rule.role_id is not None:
                role_rules[rule.node_id][rule.role_id] = rule.effect
    return _AclIndex(nodes_by_id, children, member_rules, role_rules)


def _path_ids(index: _AclIndex, node_id: uuid.UUID) -> list[uuid.UUID]:
    """Путь от узла вверх к корню включительно (от ближнего к дальнему).

    `seen`-страж гарантирует завершение даже при гипотетическом цикле parent_id
    (его не должно быть — см. _move_node, — но single-row FOR UPDATE не закрывает
    гонку встречного reparent; защищаемся, как _ancestor_ids/_collect_subtree_ids).
    """
    path: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    cursor: uuid.UUID | None = node_id
    while cursor is not None and cursor in index.nodes and cursor not in seen:
        seen.add(cursor)
        path.append(cursor)
        cursor = index.nodes[cursor].parent_id
    return path


def _resolve_employee(
    index: _AclIndex,
    node_id: uuid.UUID,
    user_id: uuid.UUID,
    role_id: uuid.UUID | None,
) -> bool:
    """Эффективный allow employee к узлу. Приоритет категорий 1→2→3→4."""
    path = _path_ids(index, node_id)
    # 1) персональное правило — ближайший узел с правилом на меня решает.
    for nid in path:
        rule = index.member_rules.get(nid)
        if rule and user_id in rule:
            return rule[user_id] == KnowledgeAccessEffect.allow
    # 2) ролевое правило — ближайший узел с правилом на мою кастомную роль.
    if role_id is not None:
        for nid in path:
            rule = index.role_rules.get(nid)
            if rule and role_id in rule:
                return rule[role_id] == KnowledgeAccessEffect.allow
    # 3) all_members на узле или любом предке; иначе (4) — deny по умолчанию.
    return any(index.nodes[nid].all_members for nid in path)


def _compute_visible(index: _AclIndex, directly: set[uuid.UUID]) -> set[uuid.UUID]:
    """Узел виден, если он доступен напрямую или у него есть видимый потомок."""
    visible: set[uuid.UUID] = set()

    def mark(node_id: uuid.UUID) -> bool:
        child_visible = False
        for child_id in index.children.get(node_id, []):
            if mark(child_id):
                child_visible = True
        if node_id in directly or child_visible:
            visible.add(node_id)
            return True
        return False

    for root_id in index.children.get(None, []):
        mark(root_id)
    return visible


# --- M1: создание ------------------------------------------------------------
async def create_node(
    session: AsyncSession,
    org_id: uuid.UUID,
    user: User,
    *,
    parent_id: uuid.UUID | None,
    kind: KnowledgeNodeKind,
    title: str,
    icon: str | None,
    position: int | None,
) -> KnowledgeNode:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id)
    if parent_id is not None:
        await _get_node(session, org_id, parent_id)  # родитель строго в этой org

    if position is None:
        position = await _next_position(session, org_id, parent_id)

    node = KnowledgeNode(
        organization_id=org_id,
        parent_id=parent_id,
        kind=kind,
        title=title,
        icon=icon,
        position=position,
        content=[] if kind == KnowledgeNodeKind.page else None,
        created_by=user.id,
    )
    session.add(node)
    await session.flush()
    logger.info(
        "knowledge_node_created", org_id=str(org_id), node_id=str(node.id), kind=kind.value
    )
    return node


async def _next_position(
    session: AsyncSession,
    org_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    *,
    exclude_id: uuid.UUID | None = None,
) -> int:
    conditions = [KnowledgeNode.organization_id == org_id, _parent_filter(parent_id)]
    if exclude_id is not None:
        conditions.append(KnowledgeNode.id != exclude_id)
    max_pos = (
        await session.execute(select(func.max(KnowledgeNode.position)).where(*conditions))
    ).scalar()
    return (max_pos + 1) if max_pos is not None else 0


def _parent_filter(parent_id: uuid.UUID | None) -> Any:
    if parent_id is None:
        return KnowledgeNode.parent_id.is_(None)
    return KnowledgeNode.parent_id == parent_id


# --- M2: дерево --------------------------------------------------------------
async def get_tree(
    session: AsyncSession,
    org_id: uuid.UUID,
    user: User,
) -> tuple[list[KnowledgeNode], dict[uuid.UUID | None, list[uuid.UUID]], set[uuid.UUID], bool]:
    """Возвращает (упорядоченные узлы, children-map, visible-ids, include_all_members)."""
    org = await org_service.get_organization(session, org_id)
    viewer, member = await _resolve_viewer(session, org, user)
    index = await _load_acl_index(session, org_id)

    if viewer == "manager":
        visible = set(index.nodes.keys())
        include_all_members = True
    else:
        role_id = member.role_id if member is not None else None
        directly = {nid for nid in index.nodes if _resolve_employee(index, nid, user.id, role_id)}
        visible = _compute_visible(index, directly)
        include_all_members = False

    ordered = list(index.nodes.values())  # уже отсортированы по position в _load_acl_index
    return ordered, index.children, visible, include_all_members


# --- M3: деталь --------------------------------------------------------------
async def get_node_detail(
    session: AsyncSession,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    user: User,
) -> tuple[KnowledgeNode, list[tuple[uuid.UUID, str]], list[dict[str, Any]] | None]:
    org = await org_service.get_organization(session, org_id)
    viewer, member = await _resolve_viewer(session, org, user)
    node = await _get_node(session, org_id, node_id)

    if viewer == "employee":
        index = await _load_acl_index(session, org_id)
        role_id = member.role_id if member is not None else None
        if not _resolve_employee(index, node_id, user.id, role_id):
            # Недоступный узел для employee — как несуществующий (не 403).
            raise KnowledgeError("KNOWLEDGE_NODE_NOT_FOUND", "Узел базы знаний не найден", 404)

    breadcrumbs = await _build_breadcrumbs(session, org_id, node)
    content = await _build_page_content(session, node)
    return node, breadcrumbs, content


async def _build_breadcrumbs(
    session: AsyncSession,
    org_id: uuid.UUID,
    node: KnowledgeNode,
) -> list[tuple[uuid.UUID, str]]:
    """Путь от корня к узлу включительно (в порядке от корня)."""
    rows = (
        await session.execute(
            select(KnowledgeNode.id, KnowledgeNode.parent_id, KnowledgeNode.title).where(
                KnowledgeNode.organization_id == org_id
            )
        )
    ).all()
    by_id = {row.id: (row.parent_id, row.title) for row in rows}
    chain: list[tuple[uuid.UUID, str]] = []
    cursor: uuid.UUID | None = node.id
    while cursor is not None and cursor in by_id:
        parent_id, title = by_id[cursor]
        chain.append((cursor, title))
        cursor = parent_id
    chain.reverse()
    return chain


async def _build_page_content(
    session: AsyncSession,
    node: KnowledgeNode,
) -> list[dict[str, Any]] | None:
    """Для страницы — блоки с обогащением presigned; для раздела — None."""
    if node.kind != KnowledgeNodeKind.page:
        return None
    return await _enrich_content(session, node.content or [])


async def _enrich_content(
    session: AsyncSession,
    content: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Добавляет свежие presigned url/url_expires_at в блоки image/file (без N+1).

    При сбое storage конкретный блок получает url=None — структура страницы не
    рушится (клиент дотянет ссылку через GET /api/v1/files/{id}).
    """
    file_ids = _content_file_ids(content)
    url_map: dict[uuid.UUID, tuple[str | None, Any]] = {}
    if file_ids:
        files = list(
            (await session.execute(select(File).where(File.id.in_(file_ids)))).scalars().all()
        )
        url_map = await presigned_urls_for(files)

    enriched: list[dict[str, Any]] = []
    for block in content:
        item = dict(block)
        if item.get("type") in _FILE_BLOCK_TYPES and item.get("file_id"):
            file_id = uuid.UUID(str(item["file_id"]))
            url, expires_at = url_map.get(file_id, (None, None))
            item["url"] = url
            item["url_expires_at"] = expires_at
        enriched.append(item)
    return enriched


def _content_file_ids(content: list[dict[str, Any]] | None) -> set[uuid.UUID]:
    """file_id из блоков image/file контента."""
    ids: set[uuid.UUID] = set()
    for block in content or []:
        if block.get("type") in _FILE_BLOCK_TYPES and block.get("file_id"):
            ids.add(uuid.UUID(str(block["file_id"])))
    return ids


# --- M4: обновление ----------------------------------------------------------
async def update_node(
    session: AsyncSession,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    user: User,
    *,
    fields: dict[str, Any],
) -> tuple[KnowledgeNode, list[tuple[uuid.UUID, str]], list[dict[str, Any]] | None]:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id)
    node = await _get_node(session, org_id, node_id, for_update="parent_id" in fields)

    if "content" in fields:
        if node.kind != KnowledgeNodeKind.page:
            raise KnowledgeError("VALIDATION_ERROR", "content допустим только для страницы", 422)
        new_content: list[dict[str, Any]] = fields["content"] or []
        await _recompute_file_bindings(session, org_id, node, new_content, user)
        node.content = new_content

    if fields.get("title") is not None:
        node.title = fields["title"]
    if "icon" in fields:
        node.icon = fields["icon"]
    if fields.get("all_members") is not None:
        node.all_members = fields["all_members"]

    if "parent_id" in fields:
        await _move_node(session, org_id, node, fields["parent_id"], fields.get("position"))
    elif fields.get("position") is not None:
        node.position = fields["position"]

    await session.flush()
    logger.info("knowledge_node_updated", org_id=str(org_id), node_id=str(node_id))

    breadcrumbs = await _build_breadcrumbs(session, org_id, node)
    content = await _build_page_content(session, node)
    return node, breadcrumbs, content


async def _move_node(
    session: AsyncSession,
    org_id: uuid.UUID,
    node: KnowledgeNode,
    new_parent_id: uuid.UUID | None,
    explicit_position: int | None,
) -> None:
    if new_parent_id == node.id:
        raise KnowledgeError("KNOWLEDGE_NODE_CYCLE", "Нельзя переместить узел в самого себя", 400)
    if new_parent_id is not None:
        await _get_node(session, org_id, new_parent_id)  # родитель строго в этой org
        ancestors = await _ancestor_ids(session, org_id, new_parent_id)
        if node.id in ancestors:
            raise KnowledgeError(
                "KNOWLEDGE_NODE_CYCLE",
                "Перемещение под собственного потомка запрещено",
                400,
            )

    moved_parent = new_parent_id != node.parent_id
    node.parent_id = new_parent_id
    if explicit_position is not None:
        node.position = explicit_position
    elif moved_parent:
        node.position = await _next_position(session, org_id, new_parent_id, exclude_id=node.id)


async def _ancestor_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
    start_id: uuid.UUID,
) -> set[uuid.UUID]:
    """id всех предков узла включая сам узел (для проверки цикла перемещения)."""
    ids: set[uuid.UUID] = set()
    cursor: uuid.UUID | None = start_id
    while cursor is not None and cursor not in ids:
        row = (
            await session.execute(
                select(KnowledgeNode.id, KnowledgeNode.parent_id).where(
                    KnowledgeNode.id == cursor,
                    KnowledgeNode.organization_id == org_id,
                )
            )
        ).first()
        if row is None:
            break
        ids.add(row.id)
        cursor = row.parent_id
    return ids


async def _recompute_file_bindings(
    session: AsyncSession,
    org_id: uuid.UUID,
    node: KnowledgeNode,
    new_content: list[dict[str, Any]],
    user: User,
) -> None:
    """Diff множеств file_id: добавить новые (валидация + is_attached), удалить исчезнувшие.

    Добавления (могут упасть на UNIQUE) — до удалений (необратимый delete_file S3),
    чтобы при ошибке привязки откатить транзакцию без потери уже удалённых объектов.
    """
    new_ids = _content_file_ids(new_content)
    current_ids = set(
        (
            await session.execute(
                select(KnowledgeNodeFile.file_id).where(KnowledgeNodeFile.node_id == node.id)
            )
        )
        .scalars()
        .all()
    )
    to_add = new_ids - current_ids
    to_remove = current_ids - new_ids

    for file_id in to_add:
        file = await _validate_kb_file(session, org_id, file_id, node.id)
        file.is_attached = True
        session.add(KnowledgeNodeFile(node_id=node.id, file_id=file_id))
        try:
            await session.flush()
        except IntegrityError as exc:
            # Гонка «один файл — две страницы»: конфликт UNIQUE(file_id).
            await session.rollback()
            raise KnowledgeError(
                "KNOWLEDGE_FILE_INVALID",
                "Файл уже привязан к другой странице",
                400,
            ) from exc

    for file_id in to_remove:
        registry_row = (
            await session.execute(
                select(KnowledgeNodeFile).where(
                    KnowledgeNodeFile.node_id == node.id,
                    KnowledgeNodeFile.file_id == file_id,
                )
            )
        ).scalar_one_or_none()
        if registry_row is not None:
            await session.delete(registry_row)
        removed = (
            await session.execute(select(File).where(File.id == file_id))
        ).scalar_one_or_none()
        if removed is not None:
            removed.is_attached = False
            await session.flush()
            await delete_file(session, file_id, user)


async def _validate_kb_file(
    session: AsyncSession,
    org_id: uuid.UUID,
    file_id: uuid.UUID,
    node_id: uuid.UUID,
) -> File:
    """Файл существует, категории knowledge_base, той же org, не привязан к другой странице."""
    file = (await session.execute(select(File).where(File.id == file_id))).scalar_one_or_none()
    if (
        file is None
        or file.category != FileCategory.knowledge_base
        or file.organization_id != org_id
    ):
        raise KnowledgeError("KNOWLEDGE_FILE_INVALID", "Недопустимый файл в блоке", 400)
    other = (
        await session.execute(
            select(KnowledgeNodeFile).where(
                KnowledgeNodeFile.file_id == file_id,
                KnowledgeNodeFile.node_id != node_id,
            )
        )
    ).scalar_one_or_none()
    if other is not None:
        raise KnowledgeError("KNOWLEDGE_FILE_INVALID", "Файл уже привязан к другой странице", 400)
    return file


# --- M5: удаление поддерева --------------------------------------------------
async def delete_node(
    session: AsyncSession,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    user: User,
) -> None:
    """Удалить узел и поддерево; файлы поддерева — из S3 и `files` ДО каскада (приватность)."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id)
    node = await _get_node(session, org_id, node_id)

    subtree_ids = await _collect_subtree_ids(session, org_id, node_id)
    file_ids = set(
        (
            await session.execute(
                select(KnowledgeNodeFile.file_id).where(KnowledgeNodeFile.node_id.in_(subtree_ids))
            )
        )
        .scalars()
        .all()
    )
    for file_id in file_ids:
        file = (await session.execute(select(File).where(File.id == file_id))).scalar_one_or_none()
        if file is None:
            continue
        file.is_attached = False
        await session.flush()
        await delete_file(session, file_id, user)

    await session.delete(node)  # ON DELETE CASCADE сносит потомков, их ACL и реестр
    await session.flush()
    logger.info(
        "knowledge_node_deleted",
        org_id=str(org_id),
        node_id=str(node_id),
        subtree=len(subtree_ids),
        files=len(file_ids),
    )


async def _collect_subtree_ids(
    session: AsyncSession,
    org_id: uuid.UUID,
    root_id: uuid.UUID,
) -> set[uuid.UUID]:
    rows = (
        await session.execute(
            select(KnowledgeNode.id, KnowledgeNode.parent_id).where(
                KnowledgeNode.organization_id == org_id
            )
        )
    ).all()
    children: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for row in rows:
        if row.parent_id is not None:
            children[row.parent_id].append(row.id)

    subtree: set[uuid.UUID] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in subtree:
            continue
        subtree.add(current)
        stack.extend(children.get(current, []))
    return subtree


# --- M6: переупорядочивание --------------------------------------------------
async def reorder_nodes(
    session: AsyncSession,
    org_id: uuid.UUID,
    user: User,
    *,
    parent_id: uuid.UUID | None,
    ordered_ids: list[uuid.UUID],
) -> None:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id)
    if parent_id is not None:
        await _get_node(session, org_id, parent_id)

    children = list(
        (
            await session.execute(
                select(KnowledgeNode)
                .where(KnowledgeNode.organization_id == org_id, _parent_filter(parent_id))
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    actual_ids = {child.id for child in children}
    for node_id in ordered_ids:
        if node_id not in actual_ids:
            raise KnowledgeError("KNOWLEDGE_NODE_NOT_FOUND", "Узел базы знаний не найден", 404)
    if len(ordered_ids) != len(actual_ids) or set(ordered_ids) != actual_ids:
        raise KnowledgeError(
            "VALIDATION_ERROR",
            "Набор ordered_ids должен совпадать с детьми родителя",
            422,
        )

    by_id = {child.id: child for child in children}
    for index, node_id in enumerate(ordered_ids):
        by_id[node_id].position = index
    await session.flush()
    logger.info("knowledge_nodes_reordered", org_id=str(org_id), count=len(ordered_ids))


# --- A1/A2: ACL --------------------------------------------------------------
async def get_access(
    session: AsyncSession,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    user: User,
) -> tuple[bool, list[KnowledgeNodeAccess]]:
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id)
    node = await _get_node(session, org_id, node_id)
    rules = list(
        (
            await session.execute(
                select(KnowledgeNodeAccess)
                .where(KnowledgeNodeAccess.node_id == node_id)
                .order_by(KnowledgeNodeAccess.created_at)
            )
        )
        .scalars()
        .all()
    )
    return node.all_members, rules


async def replace_access(
    session: AsyncSession,
    org_id: uuid.UUID,
    node_id: uuid.UUID,
    user: User,
    *,
    all_members: bool,
    rules: list[Any],
) -> tuple[bool, list[KnowledgeNodeAccess]]:
    """Bulk-замена набора правил + all_members в одной транзакции."""
    org = await org_service.get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, user.id)
    node = await _get_node(session, org_id, node_id, for_update=True)

    seen: set[tuple[str, uuid.UUID]] = set()
    for rule in rules:
        if rule.subject_type == KnowledgeSubjectType.role:
            await _ensure_role_in_org(session, org_id, rule.role_id)
            key = ("role", rule.role_id)
        else:
            await _ensure_member_in_org(session, org_id, rule.member_user_id)
            key = ("member", rule.member_user_id)
        if key in seen:
            raise KnowledgeError("VALIDATION_ERROR", "Дубликат субъекта в наборе правил", 422)
        seen.add(key)

    await session.execute(
        delete(KnowledgeNodeAccess).where(KnowledgeNodeAccess.node_id == node_id)
    )
    created: list[KnowledgeNodeAccess] = []
    for rule in rules:
        row = KnowledgeNodeAccess(
            node_id=node_id,
            subject_type=rule.subject_type,
            role_id=rule.role_id,
            member_user_id=rule.member_user_id,
            effect=rule.effect,
        )
        session.add(row)
        created.append(row)
    node.all_members = all_members
    await session.flush()
    logger.info("knowledge_access_replaced", org_id=str(org_id), node_id=str(node_id))
    return node.all_members, created


async def _ensure_role_in_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    role_id: uuid.UUID,
) -> None:
    role = (
        await session.execute(
            select(OrganizationRole.id).where(
                OrganizationRole.id == role_id,
                OrganizationRole.organization_id == org_id,
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise KnowledgeError("ROLE_NOT_FOUND", "Роль не найдена", 404)


async def _ensure_member_in_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
) -> None:
    member = (
        await session.execute(
            select(OrganizationMember.id).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == member_user_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise KnowledgeError("MEMBER_NOT_FOUND", "Участник не найден", 404)
