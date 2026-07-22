"""Тесты привязки чек-листов к рабочим точкам (checklist_work_location).

Покрывают матрицу из backend.md: 9 сценариев создания экземпляров на старте
смены (фильтр по точке, новый канал назначения «нет ролей + есть точки»,
взаимодействие с personal_add/personal_remove, архивные шаблоны, смена без
точки) + 7 сценариев API (валидация чужой org, права, идемпотентность, PUT с
пустым массивом, каскад удаления точки, фильтр эффективных чек-листов по
`work_location_id`).
"""

import uuid

from httpx import AsyncClient, Response
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.user import User


async def _create_user(
    db_session: AsyncSession,
    email: str,
    name: str = "User",
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("Test1234"),
        name=name,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _login_as(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


async def _setup_org_with_member(
    client: AsyncClient,
    db_session: AsyncSession,
    owner_headers: dict[str, str],
) -> dict:
    """Организация (owner = super_admin) + один сотрудник (employee, без роли)."""
    org_resp = await client.post(
        "/api/v1/organizations",
        headers=owner_headers,
        json={"name": "Cafe"},
    )
    org_id = org_resp.json()["data"]["id"]
    invite_code = org_resp.json()["data"]["invite_code"]

    member_user = await _create_user(db_session, "member@example.com")
    member_headers = await _login_as(client, "member@example.com")
    await client.post(
        f"/api/v1/organizations/join/{invite_code}",
        headers=member_headers,
    )
    return {
        "org_id": org_id,
        "member_user": member_user,
        "member_headers": member_headers,
    }


async def _make_template(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    name: str = "Открытие",
    type_: str = "shift_start",
    is_required: bool = True,
) -> str:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/checklist-templates",
        headers=headers,
        json={"name": name, "type": type_, "is_required": is_required},
    )
    return resp.json()["data"]["id"]


async def _make_location(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    name: str = "Точка",
) -> str:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/locations",
        headers=headers,
        json={"name": name, "latitude": 55.7558, "longitude": 37.6173, "radius_meters": 200},
    )
    return resp.json()["data"]["id"]


async def _make_role(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    name: str = "Бариста",
) -> str:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/roles",
        headers=headers,
        json={"name": name},
    )
    return resp.json()["data"]["id"]


async def _assign_role_to_member(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    user_id: str,
    role_id: str,
) -> None:
    resp = await client.patch(
        f"/api/v1/organizations/{org_id}/members/{user_id}/custom-role",
        headers=headers,
        json={"role_id": role_id},
    )
    assert resp.status_code == 200, resp.text


async def _assign_template_to_roles(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    template_id: str,
    role_ids: list[str],
) -> None:
    resp = await client.put(
        f"/api/v1/organizations/{org_id}/checklist-templates/{template_id}/roles",
        headers=headers,
        json={"role_ids": role_ids},
    )
    assert resp.status_code == 200, resp.text


async def _assign_template_to_locations(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    template_id: str,
    location_ids: list[str],
) -> Response:
    return await client.put(
        f"/api/v1/organizations/{org_id}/checklist-templates/{template_id}/locations",
        headers=headers,
        json={"location_ids": location_ids},
    )


async def _set_member_override(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    user_id: str,
    template_id: str,
    override_type: str,
) -> None:
    resp = await client.put(
        f"/api/v1/organizations/{org_id}/members/{user_id}/checklist-overrides",
        headers=headers,
        json={"overrides": [{"template_id": template_id, "type": override_type}]},
    )
    assert resp.status_code == 200, resp.text


async def _archive_template(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    template_id: str,
) -> None:
    resp = await client.delete(
        f"/api/v1/organizations/{org_id}/checklist-templates/{template_id}",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def _start_shift(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    location_id: str | None = None,
) -> str:
    body: dict[str, str] = {"organization_id": org_id}
    if location_id is not None:
        body["work_location_id"] = location_id
    resp = await client.post("/api/v1/shifts/start", headers=headers, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _shift_checklist_names(
    client: AsyncClient,
    headers: dict[str, str],
    shift_id: str,
) -> set[str]:
    resp = await client.get(f"/api/v1/shifts/{shift_id}/checklists", headers=headers)
    assert resp.status_code == 200, resp.text
    return {item["name"] for item in resp.json()["data"]["items"]}


class TestInstanceCreationLocationFilter:
    """Матрица создания экземпляров чек-листов на старте смены (пункты 1-9)."""

    async def test_1_no_bindings_role_matched_created(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Регресс текущего поведения: шаблон без привязок к точкам всегда
        создаётся при совпадении роли."""
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        role_id = await _make_role(client, super_admin_headers, ctx["org_id"])
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_roles(client, super_admin_headers, ctx["org_id"], tpl, [role_id])
        await _assign_role_to_member(
            client, super_admin_headers, ctx["org_id"], str(ctx["member_user"].id), role_id
        )
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")

        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], loc_a)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == {"T1"}

    async def test_2_bound_to_a_shift_at_a_role_matched_created(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        role_id = await _make_role(client, super_admin_headers, ctx["org_id"])
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_roles(client, super_admin_headers, ctx["org_id"], tpl, [role_id])
        resp = await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        assert resp.status_code == 200
        await _assign_role_to_member(
            client, super_admin_headers, ctx["org_id"], str(ctx["member_user"].id), role_id
        )

        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], loc_a)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == {"T1"}

    async def test_3_bound_to_a_shift_at_b_role_matched_not_created(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        role_id = await _make_role(client, super_admin_headers, ctx["org_id"])
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        loc_b = await _make_location(client, super_admin_headers, ctx["org_id"], "B")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_roles(client, super_admin_headers, ctx["org_id"], tpl, [role_id])
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        await _assign_role_to_member(
            client, super_admin_headers, ctx["org_id"], str(ctx["member_user"].id), role_id
        )

        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], loc_b)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == set()

    async def test_4_bound_to_a_no_roles_shift_at_a_created_new_channel(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Новый канал назначения: шаблон без единой привязки к роли, но с
        привязкой к точке, достаётся сотруднику БЕЗ кастомной роли вообще."""
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        # member_user НЕ получает custom-role — role_id остаётся null.

        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], loc_a)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == {"T1"}

    async def test_5_bound_to_a_personal_add_shift_at_b_not_created(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Фильтр по точке бьёт и по personal_add (решение аналитика №1)."""
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        loc_b = await _make_location(client, super_admin_headers, ctx["org_id"], "B")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        await _set_member_override(
            client,
            super_admin_headers,
            ctx["org_id"],
            str(ctx["member_user"].id),
            tpl,
            "add",
        )

        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], loc_b)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == set()

    async def test_6_bound_to_a_personal_remove_shift_at_a_not_created(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """personal_remove блокирует шаблон и в новом канале «нет ролей + есть
        точки» — remove действует независимо от того, как шаблон достался бы
        сотруднику."""
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        await _set_member_override(
            client,
            super_admin_headers,
            ctx["org_id"],
            str(ctx["member_user"].id),
            tpl,
            "remove",
        )

        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], loc_a)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == set()

    async def test_7_shift_without_location_only_unbound_templates(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        role_id = await _make_role(client, super_admin_headers, ctx["org_id"])
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")

        tpl_unbound = await _make_template(
            client, super_admin_headers, ctx["org_id"], name="Unbound"
        )
        tpl_bound = await _make_template(client, super_admin_headers, ctx["org_id"], name="Bound")
        await _assign_template_to_roles(
            client, super_admin_headers, ctx["org_id"], tpl_unbound, [role_id]
        )
        await _assign_template_to_roles(
            client, super_admin_headers, ctx["org_id"], tpl_bound, [role_id]
        )
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl_bound, [loc_a]
        )
        await _assign_role_to_member(
            client, super_admin_headers, ctx["org_id"], str(ctx["member_user"].id), role_id
        )

        # Смена без точки: work_location_id не передаём.
        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], None)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == {"Unbound"}

    async def test_8_bound_to_two_locations_shift_at_second_created(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        role_id = await _make_role(client, super_admin_headers, ctx["org_id"])
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        loc_b = await _make_location(client, super_admin_headers, ctx["org_id"], "B")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_roles(client, super_admin_headers, ctx["org_id"], tpl, [role_id])
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a, loc_b]
        )
        await _assign_role_to_member(
            client, super_admin_headers, ctx["org_id"], str(ctx["member_user"].id), role_id
        )

        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], loc_b)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == {"T1"}

    async def test_9_archived_template_bound_to_location_not_created(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        role_id = await _make_role(client, super_admin_headers, ctx["org_id"])
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_roles(client, super_admin_headers, ctx["org_id"], tpl, [role_id])
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        await _assign_role_to_member(
            client, super_admin_headers, ctx["org_id"], str(ctx["member_user"].id), role_id
        )
        await _archive_template(client, super_admin_headers, ctx["org_id"], tpl)

        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], loc_a)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == set()


class TestTemplateLocationsApi:
    """PUT/GET .../checklist-templates/{id}/locations и /assignments (пункты 10, 12-14)."""

    async def test_10_foreign_location_rejected(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])

        other_org_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Other"}
        )
        other_org_id = other_org_resp.json()["data"]["id"]
        foreign_loc = await _make_location(client, super_admin_headers, other_org_id, "Foreign")

        resp = await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [foreign_loc]
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_LOCATION"

    async def test_12_forbidden_for_employee(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")

        resp = await _assign_template_to_locations(
            client, ctx["member_headers"], ctx["org_id"], tpl, [loc_a]
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_13_repeat_put_idempotent(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")

        resp1 = await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        assert resp1.status_code == 200
        resp2 = await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        assert resp2.status_code == 200
        assert resp2.json()["data"]["location_ids"] == [loc_a]

    async def test_14_empty_array_clears_bindings(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")

        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        resp = await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, []
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["location_ids"] == []

    async def test_assignments_view_includes_location_ids(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl}/assignments",
            headers=super_admin_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["location_ids"] == [loc_a]


class TestLocationTemplatesApi:
    """PUT/GET .../locations/{id}/checklist-templates (пункты 11-12)."""

    async def test_11_foreign_template_rejected(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")

        other_org_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Other"}
        )
        other_org_id = other_org_resp.json()["data"]["id"]
        foreign_tpl = await _make_template(client, super_admin_headers, other_org_id)

        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/locations/{loc_a}/checklist-templates",
            headers=super_admin_headers,
            json={"template_ids": [foreign_tpl]},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_TEMPLATE"

    async def test_12_forbidden_for_employee(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])

        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/locations/{loc_a}/checklist-templates",
            headers=ctx["member_headers"],
            json={"template_ids": [tpl]},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "FORBIDDEN"

    async def test_get_includes_archived_with_flag(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Архивные шаблоны включаются в выдачу — привязка видна админу."""
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )
        await _archive_template(client, super_admin_headers, ctx["org_id"], tpl)

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/locations/{loc_a}/checklist-templates",
            headers=super_admin_headers,
        )
        assert resp.status_code == 200
        items = resp.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["id"] == tpl
        assert items[0]["is_archived"] is True

    async def test_written_from_location_side_visible_from_template_side(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Симметрия: запись через .../locations/{id}/checklist-templates видна
        через .../checklist-templates/{id}/assignments (одна таблица связей)."""
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"])

        resp = await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/locations/{loc_a}/checklist-templates",
            headers=super_admin_headers,
            json={"template_ids": [tpl]},
        )
        assert resp.status_code == 200

        assignments = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl}/assignments",
            headers=super_admin_headers,
        )
        assert assignments.json()["data"]["location_ids"] == [loc_a]


class TestLocationDeletionCascade:
    """Пункт 15: удаление точки каскадно снимает привязки, шаблон остаётся."""

    async def test_delete_location_removes_binding_template_remains(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        tpl = await _make_template(client, super_admin_headers, ctx["org_id"], name="T1")
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl, [loc_a]
        )

        del_resp = await client.delete(
            f"/api/v1/organizations/{ctx['org_id']}/locations/{loc_a}",
            headers=super_admin_headers,
        )
        assert del_resp.status_code == 200

        assignments = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl}/assignments",
            headers=super_admin_headers,
        )
        assert assignments.json()["data"]["location_ids"] == []

        # Шаблон остаётся (не архивирован, не удалён).
        detail = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/checklist-templates/{tpl}",
            headers=super_admin_headers,
        )
        assert detail.status_code == 200
        assert detail.json()["data"]["is_archived"] is False

        # Побочный эффект (принят как есть, backend.md): шаблон без привязок
        # снова действует на всех точках — роль не нужна, канал location-only
        # уже не применяется (нет привязок), поэтому назначаем роль явно, чтобы
        # проверить, что шаблон по-прежнему полноценно назначаем.
        role_id = await _make_role(client, super_admin_headers, ctx["org_id"])
        await _assign_template_to_roles(client, super_admin_headers, ctx["org_id"], tpl, [role_id])
        await _assign_role_to_member(
            client, super_admin_headers, ctx["org_id"], str(ctx["member_user"].id), role_id
        )
        loc_b = await _make_location(client, super_admin_headers, ctx["org_id"], "B")
        shift_id = await _start_shift(client, ctx["member_headers"], ctx["org_id"], loc_b)
        names = await _shift_checklist_names(client, ctx["member_headers"], shift_id)
        assert names == {"T1"}


class TestEffectiveChecklistsLocationFilter:
    """Пункт 16: GET .../members/{user_id}/checklists?work_location_id=..."""

    async def test_16_filters_with_param_returns_all_without(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        loc_a = await _make_location(client, super_admin_headers, ctx["org_id"], "A")
        loc_b = await _make_location(client, super_admin_headers, ctx["org_id"], "B")

        tpl_unbound = await _make_template(
            client, super_admin_headers, ctx["org_id"], name="Unbound"
        )
        tpl_bound_a = await _make_template(
            client, super_admin_headers, ctx["org_id"], name="BoundA"
        )
        role_id = await _make_role(client, super_admin_headers, ctx["org_id"])
        await _assign_template_to_roles(
            client, super_admin_headers, ctx["org_id"], tpl_unbound, [role_id]
        )
        await _assign_template_to_roles(
            client, super_admin_headers, ctx["org_id"], tpl_bound_a, [role_id]
        )
        await _assign_template_to_locations(
            client, super_admin_headers, ctx["org_id"], tpl_bound_a, [loc_a]
        )
        await _assign_role_to_member(
            client, super_admin_headers, ctx["org_id"], str(ctx["member_user"].id), role_id
        )

        # Без параметра — весь набор, location_ids видны информационно.
        resp_all = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists",
            headers=super_admin_headers,
        )
        assert resp_all.status_code == 200
        items_all = resp_all.json()["data"]["items"]
        assert {i["id"] for i in items_all} == {tpl_unbound, tpl_bound_a}
        bound_item = next(i for i in items_all if i["id"] == tpl_bound_a)
        assert bound_item["location_ids"] == [loc_a]
        unbound_item = next(i for i in items_all if i["id"] == tpl_unbound)
        assert unbound_item["location_ids"] == []

        # С параметром = A: оба шаблона (unbound везде + bound_a на своей точке).
        resp_a = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists"
            f"?work_location_id={loc_a}",
            headers=super_admin_headers,
        )
        assert resp_a.status_code == 200
        assert {i["id"] for i in resp_a.json()["data"]["items"]} == {tpl_unbound, tpl_bound_a}

        # С параметром = B: только unbound (bound_a привязан только к A).
        resp_b = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists"
            f"?work_location_id={loc_b}",
            headers=super_admin_headers,
        )
        assert resp_b.status_code == 200
        assert {i["id"] for i in resp_b.json()["data"]["items"]} == {tpl_unbound}

    async def test_foreign_work_location_param_404(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_org_with_member(client, db_session, super_admin_headers)
        other_org_resp = await client.post(
            "/api/v1/organizations", headers=super_admin_headers, json={"name": "Other"}
        )
        other_org_id = other_org_resp.json()["data"]["id"]
        foreign_loc = await _make_location(client, super_admin_headers, other_org_id, "Foreign")

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/members/{ctx['member_user'].id}/checklists"
            f"?work_location_id={foreign_loc}",
            headers=super_admin_headers,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "WORK_LOCATION_NOT_FOUND"
