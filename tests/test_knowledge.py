"""Фича knowledge_base: дерево узлов, ACL-резолюция, привязка файлов, блоки.

Покрывает приёмку backend.md: ACL (приоритет категорий, наследование, 404 vs 403),
дерево employee, привязку/отвязку файлов, перемещение/циклы, reorder, замену ACL,
удаление поддерева с чисткой S3, чтение с presigned, валидацию BLOCK SCHEMA.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core import storage
from src.app.core.security import hash_password
from src.app.models.file import File
from src.app.models.knowledge import KnowledgeNode, KnowledgeNodeAccess, KnowledgeNodeFile
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_role import OrganizationRole
from src.app.models.user import User

JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 64
PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"0" * 128


@pytest.fixture(autouse=True)
def mock_storage(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """In-memory подмена S3-слоя (включая батч-подпись) — без реального MinIO."""
    store: dict[str, bytes] = {}

    async def fake_upload(key: str, body: bytes, content_type: str) -> None:
        store[key] = body

    async def fake_presign(key: str, filename: str) -> str:
        return f"https://storage.test/{key}?sig=fake"

    async def fake_presign_many(items: list[tuple[str, str]]) -> dict[str, str]:
        return {key: f"https://storage.test/{key}?sig=fake" for key, _ in items}

    async def fake_delete(key: str) -> None:
        store.pop(key, None)

    monkeypatch.setattr(storage, "upload_object", fake_upload)
    monkeypatch.setattr(storage, "generate_presigned_get", fake_presign)
    monkeypatch.setattr(storage, "generate_presigned_get_many", fake_presign_many)
    monkeypatch.setattr(storage, "delete_object", fake_delete)
    return store


# --- helpers -----------------------------------------------------------------
def _data(resp: Any) -> Any:
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["data"]


def _err(resp: Any) -> str:
    return resp.json()["error"]["code"]


async def _create(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: uuid.UUID,
    *,
    kind: str = "section",
    title: str = "Узел",
    parent_id: str | None = None,
    icon: str | None = None,
) -> Any:
    body: dict[str, Any] = {"kind": kind, "title": title}
    if parent_id is not None:
        body["parent_id"] = parent_id
    if icon is not None:
        body["icon"] = icon
    return await client.post(
        f"/api/v1/organizations/{org_id}/knowledge/nodes",
        headers=headers,
        json=body,
    )


async def _patch(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: uuid.UUID,
    node_id: str,
    **body: Any,
) -> Any:
    return await client.patch(
        f"/api/v1/organizations/{org_id}/knowledge/nodes/{node_id}",
        headers=headers,
        json=body,
    )


async def _detail(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: uuid.UUID,
    node_id: str,
) -> Any:
    return await client.get(
        f"/api/v1/organizations/{org_id}/knowledge/nodes/{node_id}",
        headers=headers,
    )


async def _tree(client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID) -> Any:
    return await client.get(
        f"/api/v1/organizations/{org_id}/knowledge/nodes",
        headers=headers,
    )


async def _set_access(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: uuid.UUID,
    node_id: str,
    *,
    all_members: bool = False,
    rules: list[dict[str, Any]] | None = None,
) -> Any:
    return await client.put(
        f"/api/v1/organizations/{org_id}/knowledge/nodes/{node_id}/access",
        headers=headers,
        json={"all_members": all_members, "rules": rules or []},
    )


async def _upload_kb_file(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: uuid.UUID,
    *,
    payload: bytes = JPEG_BYTES,
    filename: str = "pic.jpg",
    content_type: str = "image/jpeg",
) -> str:
    resp = await client.post(
        "/api/v1/files",
        headers=headers,
        files={"file": (filename, payload, content_type)},
        data={"category": "knowledge_base", "organization_id": str(org_id)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def _image_block(file_id: str, block_id: str = "img1") -> dict[str, Any]:
    return {"id": block_id, "type": "image", "file_id": file_id, "caption": "cap"}


# --- fixtures ----------------------------------------------------------------
@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="owner@example.com",
        password_hash=hash_password("Test1234"),
        name="Owner",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="orgadmin@example.com",
        password_hash=hash_password("Test1234"),
        name="Org Admin",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def admin_headers(admin_user: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "orgadmin@example.com", "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
async def outsider(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="outsider@example.com",
        password_hash=hash_password("Test1234"),
        name="Outsider",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def outsider_headers(outsider: User, client: AsyncClient) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "outsider@example.com", "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
async def org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="KB Org", owner_id=owner.id)
    db_session.add(organization)
    await db_session.commit()
    return organization


@pytest.fixture
async def org2(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="KB Org 2", owner_id=owner.id)
    db_session.add(organization)
    await db_session.commit()
    return organization


@pytest.fixture
async def admin_member(
    db_session: AsyncSession,
    org: Organization,
    admin_user: User,
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id,
        user_id=admin_user.id,
        role=MemberRole.admin,
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def employee_member(
    db_session: AsyncSession,
    org: Organization,
    verified_user: User,
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id,
        user_id=verified_user.id,
        role=MemberRole.employee,
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def employee_role(
    db_session: AsyncSession,
    org: Organization,
    employee_member: OrganizationMember,
) -> OrganizationRole:
    role = OrganizationRole(organization_id=org.id, name="Бариста")
    db_session.add(role)
    await db_session.flush()
    employee_member.role_id = role.id
    await db_session.commit()
    return role


# --- M1 + RBAC ---------------------------------------------------------------
async def test_create_section_and_page(client, owner_headers, org):
    sec = _data(await _create(client, owner_headers, org.id, kind="section", title="Регламенты"))
    assert sec["kind"] == "section"
    assert sec["content"] is None
    assert sec["position"] == 0

    page = _data(
        await _create(
            client, owner_headers, org.id, kind="page", title="Открытие", parent_id=sec["id"]
        )
    )
    assert page["kind"] == "page"
    assert page["content"] == []
    assert page["parent_id"] == sec["id"]
    assert page["position"] == 0  # первый ребёнок раздела


async def test_create_position_appends(client, owner_headers, org):
    a = _data(await _create(client, owner_headers, org.id, title="A"))
    b = _data(await _create(client, owner_headers, org.id, title="B"))
    assert a["position"] == 0
    assert b["position"] == 1


async def test_admin_can_create(client, admin_headers, org, admin_member):
    resp = await _create(client, admin_headers, org.id, title="AdminSec")
    assert resp.status_code == 201


async def test_employee_cannot_create(client, auth_headers, org, employee_member):
    resp = await _create(client, auth_headers, org.id, title="Nope")
    assert resp.status_code == 403
    assert _err(resp) == "FORBIDDEN"


async def test_create_unknown_kind_422(client, owner_headers, org):
    resp = await client.post(
        f"/api/v1/organizations/{org.id}/knowledge/nodes",
        headers=owner_headers,
        json={"kind": "folder", "title": "X"},
    )
    assert resp.status_code == 422
    assert _err(resp) == "VALIDATION_ERROR"


async def test_create_parent_other_org_404(client, owner_headers, org, org2):
    foreign = _data(await _create(client, owner_headers, org2.id, title="Foreign"))
    resp = await _create(client, owner_headers, org.id, title="Child", parent_id=foreign["id"])
    assert resp.status_code == 404
    assert _err(resp) == "KNOWLEDGE_NODE_NOT_FOUND"


# --- ACL resolution ----------------------------------------------------------
async def test_personal_deny_overrides_role_allow(
    client, owner_headers, auth_headers, org, verified_user, employee_role
):
    """Персональный deny (на предке) перебивает ролевой allow (ближе к узлу)."""
    sec = _data(await _create(client, owner_headers, org.id, kind="section", title="S"))
    page = _data(
        await _create(client, owner_headers, org.id, kind="page", title="P", parent_id=sec["id"])
    )
    # ролевой allow ближе (на странице), персональный deny дальше (на разделе)
    await _set_access(
        client,
        owner_headers,
        org.id,
        page["id"],
        rules=[{"subject_type": "role", "role_id": str(employee_role.id), "effect": "allow"}],
    )
    await _set_access(
        client,
        owner_headers,
        org.id,
        sec["id"],
        rules=[
            {"subject_type": "member", "member_user_id": str(verified_user.id), "effect": "deny"}
        ],
    )
    resp = await _detail(client, auth_headers, org.id, page["id"])
    assert resp.status_code == 404
    assert _err(resp) == "KNOWLEDGE_NODE_NOT_FOUND"


async def test_nearest_overrides_farther(
    client, owner_headers, auth_headers, org, verified_user, employee_member
):
    """Внутри категории ближайший узел перебивает дальний: deny на узле > allow на предке."""
    sec = _data(await _create(client, owner_headers, org.id, kind="section", title="S"))
    page = _data(
        await _create(client, owner_headers, org.id, kind="page", title="P", parent_id=sec["id"])
    )
    uid = str(verified_user.id)
    await _set_access(
        client,
        owner_headers,
        org.id,
        sec["id"],
        rules=[{"subject_type": "member", "member_user_id": uid, "effect": "allow"}],
    )
    await _set_access(
        client,
        owner_headers,
        org.id,
        page["id"],
        rules=[{"subject_type": "member", "member_user_id": uid, "effect": "deny"}],
    )
    assert (await _detail(client, auth_headers, org.id, sec["id"])).status_code == 200
    assert (await _detail(client, auth_headers, org.id, page["id"])).status_code == 404


async def test_all_members_on_ancestor_allows_descendant(
    client, owner_headers, auth_headers, org, employee_member
):
    sec = _data(await _create(client, owner_headers, org.id, kind="section", title="S"))
    page = _data(
        await _create(client, owner_headers, org.id, kind="page", title="P", parent_id=sec["id"])
    )
    await _set_access(client, owner_headers, org.id, sec["id"], all_members=True)
    assert (await _detail(client, auth_headers, org.id, page["id"])).status_code == 200


async def test_member_deny_overrides_all_members(
    client, owner_headers, auth_headers, org, verified_user, employee_member
):
    sec = _data(await _create(client, owner_headers, org.id, kind="section", title="S"))
    page = _data(
        await _create(client, owner_headers, org.id, kind="page", title="P", parent_id=sec["id"])
    )
    await _set_access(client, owner_headers, org.id, sec["id"], all_members=True)
    await _set_access(
        client,
        owner_headers,
        org.id,
        page["id"],
        rules=[
            {"subject_type": "member", "member_user_id": str(verified_user.id), "effect": "deny"}
        ],
    )
    assert (await _detail(client, auth_headers, org.id, page["id"])).status_code == 404


async def test_employee_no_allow_is_404_not_403(
    client, owner_headers, auth_headers, org, employee_member
):
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="Secret"))
    resp = await _detail(client, auth_headers, org.id, page["id"])
    assert resp.status_code == 404
    assert _err(resp) == "KNOWLEDGE_NODE_NOT_FOUND"


async def test_owner_ignores_acl(client, owner_headers, org, verified_user, employee_member):
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    # явный deny на участника не влияет на владельца (ACL к управляющим ролям не применяется)
    await _set_access(
        client,
        owner_headers,
        org.id,
        page["id"],
        rules=[
            {"subject_type": "member", "member_user_id": str(verified_user.id), "effect": "deny"}
        ],
    )
    assert (await _detail(client, owner_headers, org.id, page["id"])).status_code == 200


async def test_super_admin_ignores_acl(client, owner_headers, super_admin_headers, org):
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    assert (await _detail(client, super_admin_headers, org.id, page["id"])).status_code == 200


async def test_outsider_tree_forbidden(client, owner_headers, outsider_headers, org):
    await _create(client, owner_headers, org.id, title="S")
    resp = await _tree(client, outsider_headers, org.id)
    assert resp.status_code == 403
    assert _err(resp) == "FORBIDDEN"


# --- M2 дерево ---------------------------------------------------------------
async def test_tree_employee_sees_path_hides_inaccessible(
    client, owner_headers, auth_headers, org, employee_member
):
    sec = _data(await _create(client, owner_headers, org.id, kind="section", title="Доступный"))
    page = _data(
        await _create(client, owner_headers, org.id, kind="page", title="P", parent_id=sec["id"])
    )
    hidden = _data(await _create(client, owner_headers, org.id, kind="section", title="Скрытый"))
    await _create(client, owner_headers, org.id, kind="page", title="HP", parent_id=hidden["id"])
    # доступ только на вложенную страницу
    await _set_access(client, owner_headers, org.id, page["id"], all_members=True)

    items = _data(await _tree(client, auth_headers, org.id))["items"]
    titles = {it["title"] for it in items}
    assert "Доступный" in titles  # раздел-предок как навигационный контейнер
    assert "Скрытый" not in titles
    sec_item = next(it for it in items if it["title"] == "Доступный")
    assert [c["title"] for c in sec_item["children"]] == ["P"]
    assert "all_members" not in sec_item  # employee не получает управляющую инфу


async def test_tree_manager_includes_all_members(client, owner_headers, org):
    await _create(client, owner_headers, org.id, kind="section", title="S")
    items = _data(await _tree(client, owner_headers, org.id))["items"]
    assert "all_members" in items[0]


# --- M3 чтение ---------------------------------------------------------------
async def test_detail_breadcrumbs_and_section_content(client, owner_headers, org):
    sec = _data(await _create(client, owner_headers, org.id, kind="section", title="Раздел"))
    page = _data(
        await _create(client, owner_headers, org.id, kind="page", title="Стр", parent_id=sec["id"])
    )
    sec_detail = _data(await _detail(client, owner_headers, org.id, sec["id"]))
    assert sec_detail["content"] is None
    assert [b["title"] for b in sec_detail["breadcrumbs"]] == ["Раздел"]

    page_detail = _data(await _detail(client, owner_headers, org.id, page["id"]))
    assert page_detail["content"] == []
    assert [b["title"] for b in page_detail["breadcrumbs"]] == ["Раздел", "Стр"]


async def test_detail_enriches_presigned_url(client, owner_headers, org):
    fid = await _upload_kb_file(client, owner_headers, org.id)
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    _data(await _patch(client, owner_headers, org.id, page["id"], content=[_image_block(fid)]))

    detail = _data(await _detail(client, owner_headers, org.id, page["id"]))
    block = detail["content"][0]
    assert block["type"] == "image"
    assert block["url"].startswith("https://storage.test/")
    assert block["url_expires_at"] is not None


# --- M4 файлы ----------------------------------------------------------------
async def test_attach_file_sets_is_attached_and_registry(client, owner_headers, org, db_session):
    fid = await _upload_kb_file(client, owner_headers, org.id)
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    _data(await _patch(client, owner_headers, org.id, page["id"], content=[_image_block(fid)]))

    file = (await db_session.execute(select(File).where(File.id == uuid.UUID(fid)))).scalar_one()
    assert file.is_attached is True
    reg = (
        await db_session.execute(
            select(KnowledgeNodeFile).where(KnowledgeNodeFile.file_id == uuid.UUID(fid))
        )
    ).scalar_one_or_none()
    assert reg is not None


async def test_detach_file_deletes_object_and_row(
    client, owner_headers, org, db_session, mock_storage
):
    fid = await _upload_kb_file(client, owner_headers, org.id)
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    _data(await _patch(client, owner_headers, org.id, page["id"], content=[_image_block(fid)]))
    assert len(mock_storage) == 1

    # убрать блок из content → файл удаляется
    _data(await _patch(client, owner_headers, org.id, page["id"], content=[]))
    file = (
        await db_session.execute(select(File).where(File.id == uuid.UUID(fid)))
    ).scalar_one_or_none()
    assert file is None
    reg = (
        await db_session.execute(
            select(KnowledgeNodeFile).where(KnowledgeNodeFile.file_id == uuid.UUID(fid))
        )
    ).scalar_one_or_none()
    assert reg is None
    assert len(mock_storage) == 0


async def test_idempotent_patch_keeps_file(client, owner_headers, org, db_session, mock_storage):
    fid = await _upload_kb_file(client, owner_headers, org.id)
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    block = _image_block(fid)
    _data(await _patch(client, owner_headers, org.id, page["id"], content=[block]))
    _data(await _patch(client, owner_headers, org.id, page["id"], content=[block]))
    assert len(mock_storage) == 1
    file = (
        await db_session.execute(select(File).where(File.id == uuid.UUID(fid)))
    ).scalar_one_or_none()
    assert file is not None


async def test_attach_nonexistent_file_invalid(client, owner_headers, org):
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    resp = await _patch(
        client, owner_headers, org.id, page["id"], content=[_image_block(str(uuid.uuid4()))]
    )
    assert resp.status_code == 400
    assert _err(resp) == "KNOWLEDGE_FILE_INVALID"


async def test_attach_foreign_org_file_invalid(client, owner_headers, org, org2):
    fid = await _upload_kb_file(client, owner_headers, org2.id)
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    resp = await _patch(client, owner_headers, org.id, page["id"], content=[_image_block(fid)])
    assert resp.status_code == 400
    assert _err(resp) == "KNOWLEDGE_FILE_INVALID"


async def test_attach_file_already_bound_invalid(client, owner_headers, org):
    fid = await _upload_kb_file(client, owner_headers, org.id)
    p1 = _data(await _create(client, owner_headers, org.id, kind="page", title="P1"))
    p2 = _data(await _create(client, owner_headers, org.id, kind="page", title="P2"))
    _data(await _patch(client, owner_headers, org.id, p1["id"], content=[_image_block(fid)]))
    resp = await _patch(client, owner_headers, org.id, p2["id"], content=[_image_block(fid)])
    assert resp.status_code == 400
    assert _err(resp) == "KNOWLEDGE_FILE_INVALID"


async def test_content_for_section_422(client, owner_headers, org):
    sec = _data(await _create(client, owner_headers, org.id, kind="section", title="S"))
    resp = await _patch(
        client,
        owner_headers,
        org.id,
        sec["id"],
        content=[{"id": "p1", "type": "paragraph", "rich": [{"text": "hi"}]}],
    )
    assert resp.status_code == 422
    assert _err(resp) == "VALIDATION_ERROR"


# --- BLOCK SCHEMA ------------------------------------------------------------
async def test_unknown_block_type_422(client, owner_headers, org):
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    resp = await _patch(
        client, owner_headers, org.id, page["id"], content=[{"id": "b1", "type": "bogus"}]
    )
    assert resp.status_code == 422


async def test_broken_span_422(client, owner_headers, org):
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    resp = await _patch(
        client,
        owner_headers,
        org.id,
        page["id"],
        content=[{"id": "h1", "type": "heading", "level": 1, "rich": [{"bold": True}]}],
    )
    assert resp.status_code == 422


async def test_non_youtube_video_422(client, owner_headers, org):
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    resp = await _patch(
        client,
        owner_headers,
        org.id,
        page["id"],
        content=[
            {"id": "v1", "type": "video", "provider": "youtube", "url": "https://vimeo.com/9"}
        ],
    )
    assert resp.status_code == 422


async def test_youtube_video_normalized(client, owner_headers, org):
    page = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    detail = _data(
        await _patch(
            client,
            owner_headers,
            org.id,
            page["id"],
            content=[
                {
                    "id": "v1",
                    "type": "video",
                    "provider": "youtube",
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s",
                }
            ],
        )
    )
    assert detail["content"][0]["video_id"] == "dQw4w9WgXcQ"


# --- M4 перемещение / циклы --------------------------------------------------
async def test_move_under_descendant_cycle(client, owner_headers, org):
    a = _data(await _create(client, owner_headers, org.id, kind="section", title="A"))
    b = _data(
        await _create(client, owner_headers, org.id, kind="section", title="B", parent_id=a["id"])
    )
    resp = await _patch(client, owner_headers, org.id, a["id"], parent_id=b["id"])
    assert resp.status_code == 400
    assert _err(resp) == "KNOWLEDGE_NODE_CYCLE"


async def test_move_into_self_cycle(client, owner_headers, org):
    a = _data(await _create(client, owner_headers, org.id, kind="section", title="A"))
    resp = await _patch(client, owner_headers, org.id, a["id"], parent_id=a["id"])
    assert resp.status_code == 400
    assert _err(resp) == "KNOWLEDGE_NODE_CYCLE"


async def test_move_foreign_parent_404(client, owner_headers, org, org2):
    node = _data(await _create(client, owner_headers, org.id, kind="page", title="P"))
    foreign = _data(await _create(client, owner_headers, org2.id, kind="section", title="F"))
    resp = await _patch(client, owner_headers, org.id, node["id"], parent_id=foreign["id"])
    assert resp.status_code == 404
    assert _err(resp) == "KNOWLEDGE_NODE_NOT_FOUND"


async def test_move_reparents_and_appends(client, owner_headers, org):
    a = _data(await _create(client, owner_headers, org.id, kind="section", title="A"))
    b = _data(await _create(client, owner_headers, org.id, kind="section", title="B"))
    _data(
        await _create(
            client, owner_headers, org.id, kind="page", title="existing", parent_id=b["id"]
        )
    )
    moved = _data(await _patch(client, owner_headers, org.id, a["id"], parent_id=b["id"]))
    assert moved["parent_id"] == b["id"]
    assert moved["position"] == 1  # в конец сиблингов B


# --- M6 reorder --------------------------------------------------------------
async def test_reorder_changes_positions(client, owner_headers, org):
    c1 = _data(await _create(client, owner_headers, org.id, title="c1"))
    c2 = _data(await _create(client, owner_headers, org.id, title="c2"))
    c3 = _data(await _create(client, owner_headers, org.id, title="c3"))
    resp = await client.put(
        f"/api/v1/organizations/{org.id}/knowledge/nodes/reorder",
        headers=owner_headers,
        json={"parent_id": None, "ordered_ids": [c3["id"], c1["id"], c2["id"]]},
    )
    assert resp.status_code == 200
    items = _data(await _tree(client, owner_headers, org.id))["items"]
    assert [it["title"] for it in items] == ["c3", "c1", "c2"]


async def test_reorder_incomplete_set_422(client, owner_headers, org):
    c1 = _data(await _create(client, owner_headers, org.id, title="c1"))
    _data(await _create(client, owner_headers, org.id, title="c2"))
    resp = await client.put(
        f"/api/v1/organizations/{org.id}/knowledge/nodes/reorder",
        headers=owner_headers,
        json={"parent_id": None, "ordered_ids": [c1["id"]]},
    )
    assert resp.status_code == 422
    assert _err(resp) == "VALIDATION_ERROR"


async def test_reorder_foreign_id_404(client, owner_headers, org):
    c1 = _data(await _create(client, owner_headers, org.id, title="c1"))
    resp = await client.put(
        f"/api/v1/organizations/{org.id}/knowledge/nodes/reorder",
        headers=owner_headers,
        json={"parent_id": None, "ordered_ids": [c1["id"], str(uuid.uuid4())]},
    )
    assert resp.status_code == 404
    assert _err(resp) == "KNOWLEDGE_NODE_NOT_FOUND"


# --- A2 ACL валидация --------------------------------------------------------
async def test_access_role_not_in_org_404(client, owner_headers, org):
    node = _data(await _create(client, owner_headers, org.id, title="S"))
    resp = await _set_access(
        client,
        owner_headers,
        org.id,
        node["id"],
        rules=[{"subject_type": "role", "role_id": str(uuid.uuid4()), "effect": "allow"}],
    )
    assert resp.status_code == 404
    assert _err(resp) == "ROLE_NOT_FOUND"


async def test_access_member_not_in_org_404(client, owner_headers, org):
    node = _data(await _create(client, owner_headers, org.id, title="S"))
    resp = await _set_access(
        client,
        owner_headers,
        org.id,
        node["id"],
        rules=[{"subject_type": "member", "member_user_id": str(uuid.uuid4()), "effect": "allow"}],
    )
    assert resp.status_code == 404
    assert _err(resp) == "MEMBER_NOT_FOUND"


async def test_access_subject_mismatch_422(client, owner_headers, org):
    node = _data(await _create(client, owner_headers, org.id, title="S"))
    resp = await _set_access(
        client,
        owner_headers,
        org.id,
        node["id"],
        rules=[{"subject_type": "role", "member_user_id": str(uuid.uuid4()), "effect": "allow"}],
    )
    assert resp.status_code == 422


async def test_access_duplicate_subject_422(
    client, owner_headers, org, verified_user, employee_member
):
    node = _data(await _create(client, owner_headers, org.id, title="S"))
    uid = str(verified_user.id)
    resp = await _set_access(
        client,
        owner_headers,
        org.id,
        node["id"],
        rules=[
            {"subject_type": "member", "member_user_id": uid, "effect": "allow"},
            {"subject_type": "member", "member_user_id": uid, "effect": "deny"},
        ],
    )
    assert resp.status_code == 422
    assert _err(resp) == "VALIDATION_ERROR"


async def test_access_replace_is_bulk(client, owner_headers, org, verified_user, employee_member):
    node = _data(await _create(client, owner_headers, org.id, title="S"))
    uid = str(verified_user.id)
    await _set_access(
        client,
        owner_headers,
        org.id,
        node["id"],
        all_members=True,
        rules=[{"subject_type": "member", "member_user_id": uid, "effect": "allow"}],
    )
    # вторая замена полностью вытесняет первую
    data = _data(
        await _set_access(client, owner_headers, org.id, node["id"], all_members=False, rules=[])
    )
    assert data["all_members"] is False
    assert data["rules"] == []

    fetched = _data(
        await client.get(
            f"/api/v1/organizations/{org.id}/knowledge/nodes/{node['id']}/access",
            headers=owner_headers,
        )
    )
    assert fetched["rules"] == []


# --- M5 удаление поддерева ---------------------------------------------------
async def test_delete_subtree_cleans_files_and_cascades(
    client, owner_headers, org, db_session, mock_storage
):
    sec = _data(await _create(client, owner_headers, org.id, kind="section", title="S"))
    fid = await _upload_kb_file(client, owner_headers, org.id)
    page = _data(
        await _create(client, owner_headers, org.id, kind="page", title="P", parent_id=sec["id"])
    )
    _data(await _patch(client, owner_headers, org.id, page["id"], content=[_image_block(fid)]))
    await _set_access(client, owner_headers, org.id, page["id"], all_members=True)
    assert len(mock_storage) == 1

    resp = await client.delete(
        f"/api/v1/organizations/{org.id}/knowledge/nodes/{sec['id']}",
        headers=owner_headers,
    )
    assert resp.status_code == 200

    # файлы поддерева удалены из S3 и files
    assert len(mock_storage) == 0
    assert (
        await db_session.execute(select(File).where(File.id == uuid.UUID(fid)))
    ).scalar_one_or_none() is None
    # узлы, ACL и реестр ушли каскадом
    nodes = (
        (
            await db_session.execute(
                select(KnowledgeNode).where(KnowledgeNode.organization_id == org.id)
            )
        )
        .scalars()
        .all()
    )
    assert nodes == []
    access = (await db_session.execute(select(KnowledgeNodeAccess))).scalars().all()
    assert access == []
    registry = (await db_session.execute(select(KnowledgeNodeFile))).scalars().all()
    assert registry == []


async def test_employee_cannot_delete(client, auth_headers, owner_headers, org, employee_member):
    node = _data(await _create(client, owner_headers, org.id, title="S"))
    resp = await client.delete(
        f"/api/v1/organizations/{org.id}/knowledge/nodes/{node['id']}",
        headers=auth_headers,
    )
    assert resp.status_code == 403
    assert _err(resp) == "FORBIDDEN"
