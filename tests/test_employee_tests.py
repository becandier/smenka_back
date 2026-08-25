# tests/test_employee_tests.py
"""Фича employee_tests: тестирование сотрудников (шаблон → назначение → попытка)."""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.employee_test import (
    TestAssignment,
    TestAttempt,
    TestAttemptQuestion,
    TestTemplate,
)
from src.app.models.notification import Notification
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.user import User


def _data(resp: Any) -> Any:
    return resp.json()["data"]


def _err(resp: Any) -> str:
    return resp.json()["error"]["code"]


# --- Helpers -------------------------------------------------------------------
async def _create_user(db_session: AsyncSession, email: str, name: str = "User") -> User:
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
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


TWO_QUESTION_BODY: dict[str, Any] = {
    "title": "Т.Б. на рабочем месте",
    "description": "Базовый инструктаж",
    "pass_threshold_percent": 50,
    "max_attempts": 2,
    "reveal_answers": True,
    "shuffle_questions": False,
    "questions": [
        {
            "text": "Что делать при пожаре?",
            "type": "single_choice",
            "points": 1,
            "options": [
                {"text": "Вызвать 101", "is_correct": True},
                {"text": "Игнорировать", "is_correct": False},
            ],
        },
        {
            "text": "Какие СИЗ обязательны?",
            "type": "multiple_choice",
            "points": 1,
            "options": [
                {"text": "Каска", "is_correct": True},
                {"text": "Перчатки", "is_correct": True},
                {"text": "Сандалии", "is_correct": False},
            ],
        },
    ],
}


async def _create_template(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: uuid.UUID,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/test-templates",
        headers=headers,
        json=body or TWO_QUESTION_BODY,
    )
    assert resp.status_code == 201, resp.text
    return _data(resp)


async def _assign(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: uuid.UUID,
    template_id: str,
    member_ids: list[str],
    due_at: str | None = None,
) -> Any:
    return await client.post(
        f"/api/v1/organizations/{org_id}/test-templates/{template_id}/assignments",
        headers=headers,
        json={"member_ids": member_ids, "due_at": due_at},
    )


def _option_id(fill_question: dict[str, Any], text: str) -> str:
    for opt in fill_question["options"]:
        if opt["text"] == text:
            return opt["id"]
    raise AssertionError(f"option {text!r} not found")


async def _submit_first_correct(
    client: AsyncClient, headers: dict[str, str], assignment_id: str
) -> None:
    """Стартует попытку и сдаёт только первый вопрос верно (50% при пороге 50 →
    статус назначения `passed`). Для наполнения реестра разными статусами."""
    fill = _data(
        await client.post(f"/api/v1/my/test-assignments/{assignment_id}/attempts", headers=headers)
    )
    fire_q = next(q for q in fill["questions"] if q["text"] == "Что делать при пожаре?")
    resp = await client.post(
        f"/api/v1/my/test-attempts/{fill['id']}/submit",
        headers=headers,
        json={
            "answers": [
                {
                    "attempt_question_id": fire_q["id"],
                    "selected_option_ids": [_option_id(fire_q, "Вызвать 101")],
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text


# --- Fixtures --------------------------------------------------------------------
@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "owner@example.com", "Owner")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    return await _login_as(client, "owner@example.com")


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "admin@example.com", "Admin")


@pytest.fixture
async def admin_headers(admin_user: User, client: AsyncClient) -> dict[str, str]:
    return await _login_as(client, "admin@example.com")


@pytest.fixture
async def employee_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "employee@example.com", "Employee")


@pytest.fixture
async def employee_headers(employee_user: User, client: AsyncClient) -> dict[str, str]:
    return await _login_as(client, "employee@example.com")


@pytest.fixture
async def emp2_user(db_session: AsyncSession) -> User:
    return await _create_user(db_session, "emp2@example.com", "Employee Two")


@pytest.fixture
async def emp2_headers(emp2_user: User, client: AsyncClient) -> dict[str, str]:
    return await _login_as(client, "emp2@example.com")


@pytest.fixture
async def org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="Tests Org", owner_id=owner.id)
    db_session.add(organization)
    await db_session.commit()
    return organization


@pytest.fixture
async def admin_member(
    db_session: AsyncSession, org: Organization, admin_user: User
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id, user_id=admin_user.id, role=MemberRole.admin
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def employee_member(
    db_session: AsyncSession, org: Organization, employee_user: User
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id, user_id=employee_user.id, role=MemberRole.employee
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def emp2_member(
    db_session: AsyncSession, org: Organization, emp2_user: User
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id, user_id=emp2_user.id, role=MemberRole.employee
    )
    db_session.add(member)
    await db_session.commit()
    return member


# --- Инварианты шаблона ------------------------------------------------------------
class TestTemplateInvariants:
    async def test_create_success(self, client: AsyncClient, owner_headers, org):
        data = await _create_template(client, owner_headers, org.id)
        assert data["title"] == TWO_QUESTION_BODY["title"]
        assert data["question_count"] == 2
        assert data["total_points"] == 2
        assert data["is_deleted"] is False
        assert len(data["questions"]) == 2
        assert data["questions"][0]["options"][0]["is_correct"] is True

    async def test_no_questions(self, client: AsyncClient, owner_headers, org):
        body = {**TWO_QUESTION_BODY, "questions": []}
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers, json=body
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"

    async def test_single_choice_needs_exactly_one_correct(
        self, client: AsyncClient, owner_headers, org
    ):
        body = {
            **TWO_QUESTION_BODY,
            "questions": [
                {
                    "text": "Q",
                    "type": "single_choice",
                    "options": [
                        {"text": "A", "is_correct": True},
                        {"text": "B", "is_correct": True},
                    ],
                }
            ],
        }
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers, json=body
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"

    async def test_multiple_choice_needs_at_least_one_correct(
        self, client: AsyncClient, owner_headers, org
    ):
        body = {
            **TWO_QUESTION_BODY,
            "questions": [
                {
                    "text": "Q",
                    "type": "multiple_choice",
                    "options": [
                        {"text": "A", "is_correct": False},
                        {"text": "B", "is_correct": False},
                    ],
                }
            ],
        }
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers, json=body
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"

    async def test_needs_at_least_two_options(self, client: AsyncClient, owner_headers, org):
        body = {
            **TWO_QUESTION_BODY,
            "questions": [
                {
                    "text": "Q",
                    "type": "single_choice",
                    "options": [{"text": "A", "is_correct": True}],
                }
            ],
        }
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers, json=body
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"

    async def test_invalid_question_type(self, client: AsyncClient, owner_headers, org):
        body = {
            **TWO_QUESTION_BODY,
            "questions": [
                {
                    "text": "Q",
                    "type": "essay",
                    "options": [
                        {"text": "A", "is_correct": True},
                        {"text": "B", "is_correct": False},
                    ],
                }
            ],
        }
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers, json=body
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"

    async def test_pass_threshold_out_of_range(self, client: AsyncClient, owner_headers, org):
        body = {**TWO_QUESTION_BODY, "pass_threshold_percent": 150}
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers, json=body
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"

    async def test_max_attempts_below_one(self, client: AsyncClient, owner_headers, org):
        body = {**TWO_QUESTION_BODY, "max_attempts": 0}
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers, json=body
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"

    async def test_empty_title(self, client: AsyncClient, owner_headers, org):
        body = {**TWO_QUESTION_BODY, "title": "   "}
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers, json=body
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"

    async def test_employee_forbidden(
        self, client: AsyncClient, employee_headers, employee_member, org
    ):
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates",
            headers=employee_headers,
            json=TWO_QUESTION_BODY,
        )
        assert resp.status_code == 403


class TestValidateEndpoint:
    async def test_valid(self, client: AsyncClient, owner_headers, org):
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates/validate",
            headers=owner_headers,
            json=TWO_QUESTION_BODY,
        )
        assert resp.status_code == 200
        data = _data(resp)
        assert data == {"valid": True, "question_count": 2, "total_points": 2}

    async def test_does_not_persist(self, client: AsyncClient, owner_headers, org, db_session):
        await client.post(
            f"/api/v1/organizations/{org.id}/test-templates/validate",
            headers=owner_headers,
            json=TWO_QUESTION_BODY,
        )
        result = await db_session.execute(
            select(TestTemplate).where(TestTemplate.organization_id == org.id)
        )
        assert result.scalar_one_or_none() is None

    async def test_invalid(self, client: AsyncClient, owner_headers, org):
        body = {**TWO_QUESTION_BODY, "questions": []}
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates/validate",
            headers=owner_headers,
            json=body,
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"


# --- CRUD шаблона --------------------------------------------------------------------
class TestTemplateCrud:
    async def test_list_with_counts(self, client: AsyncClient, owner_headers, org):
        await _create_template(client, owner_headers, org.id)
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers
        )
        data = _data(resp)
        assert data["total"] == 1
        item = data["items"][0]
        assert item["question_count"] == 2
        assert item["total_points"] == 2
        assert item["assignments_count"] == 0

    async def test_list_include_deleted_filter(self, client: AsyncClient, owner_headers, org):
        tpl = await _create_template(client, owner_headers, org.id)
        await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )
        active = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-templates",
                headers=owner_headers,
            )
        )
        with_deleted = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-templates",
                headers=owner_headers,
                params={"include_deleted": "true"},
            )
        )
        assert active["total"] == 0
        assert with_deleted["total"] == 1

    async def test_get_detail_shows_is_correct(self, client: AsyncClient, owner_headers, org):
        tpl = await _create_template(client, owner_headers, org.id)
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}", headers=owner_headers
        )
        data = _data(resp)
        assert any(o["is_correct"] for o in data["questions"][0]["options"])

    async def test_not_found(self, client: AsyncClient, owner_headers, org):
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/test-templates/{uuid.uuid4()}",
            headers=owner_headers,
        )
        assert resp.status_code == 404
        assert _err(resp) == "TEST_TEMPLATE_NOT_FOUND"

    async def test_other_org_forbidden(
        self, client: AsyncClient, owner_headers, org, db_session, employee_user
    ):
        """org_id из чужой организации — доступ рубится раньше поиска шаблона
        (owner org #1 не owner/admin/super_admin org #2) → 403, не 404."""
        tpl = await _create_template(client, owner_headers, org.id)
        other_owner = await _create_user(db_session, "otherowner@example.com")
        other_org = Organization(name="Other", owner_id=other_owner.id)
        db_session.add(other_org)
        await db_session.commit()
        resp = await client.get(
            f"/api/v1/organizations/{other_org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )
        assert resp.status_code == 403

    async def test_update_meta_only(self, client: AsyncClient, owner_headers, org):
        tpl = await _create_template(client, owner_headers, org.id)
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
            json={"title": "Новое название", "max_attempts": 5},
        )
        assert resp.status_code == 200
        data = _data(resp)
        assert data["title"] == "Новое название"
        assert data["max_attempts"] == 5
        assert data["question_count"] == 2  # вопросы не тронуты

    async def test_update_replaces_questions(self, client: AsyncClient, owner_headers, org):
        tpl = await _create_template(client, owner_headers, org.id)
        new_body = {
            "questions": [
                {
                    "text": "Единственный вопрос",
                    "type": "single_choice",
                    "options": [
                        {"text": "X", "is_correct": True},
                        {"text": "Y", "is_correct": False},
                    ],
                }
            ]
        }
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
            json=new_body,
        )
        data = _data(resp)
        assert data["question_count"] == 1
        assert data["questions"][0]["text"] == "Единственный вопрос"

    async def test_update_invalid_questions_rejected(
        self, client: AsyncClient, owner_headers, org
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
            json={"questions": []},
        )
        assert resp.status_code == 422
        assert _err(resp) == "TEST_TEMPLATE_INVALID"

    async def test_update_deleted_forbidden(self, client: AsyncClient, owner_headers, org):
        tpl = await _create_template(client, owner_headers, org.id)
        await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
            json={"title": "X"},
        )
        assert resp.status_code == 400
        assert _err(resp) == "TEST_TEMPLATE_DELETED"


class TestDeleteAndRestoreTemplate:
    async def test_delete_hides_and_restore_shows_again(
        self, client: AsyncClient, owner_headers, org
    ):
        tpl = await _create_template(client, owner_headers, org.id)

        deleted = await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )
        assert deleted.status_code == 200
        assert _data(deleted) == {"deleted": True}

        listed = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers
            )
        )
        assert listed["total"] == 0

        with_deleted = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-templates",
                headers=owner_headers,
                params={"include_deleted": "true"},
            )
        )
        assert with_deleted["items"][0]["is_deleted"] is True

        restored = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}/restore",
            headers=owner_headers,
        )
        assert restored.status_code == 200
        data = _data(restored)
        assert data["is_deleted"] is False
        assert data["deleted_at"] is None

        listed_again = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-templates", headers=owner_headers
            )
        )
        assert listed_again["total"] == 1

    async def test_delete_twice_returns_404(self, client: AsyncClient, owner_headers, org):
        tpl = await _create_template(client, owner_headers, org.id)
        await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )
        resp = await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )
        assert resp.status_code == 404
        assert _err(resp) == "TEST_TEMPLATE_NOT_FOUND"

    async def test_restore_not_deleted_returns_409(self, client: AsyncClient, owner_headers, org):
        tpl = await _create_template(client, owner_headers, org.id)
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}/restore",
            headers=owner_headers,
        )
        assert resp.status_code == 409
        assert _err(resp) == "TEST_TEMPLATE_NOT_DELETED"


# --- Назначения ------------------------------------------------------------------
class TestAssignments:
    async def test_assign_creates_notification(
        self,
        client: AsyncClient,
        owner_headers,
        org,
        employee_member,
        employee_user,
        db_session: AsyncSession,
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        resp = await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        assert resp.status_code == 201, resp.text
        data = _data(resp)
        assert data["created"] == 1
        assert data["updated"] == 0
        assert data["items"][0]["member"]["user_id"] == str(employee_user.id)
        assert data["items"][0]["status"] == "assigned"

        result = await db_session.execute(
            select(Notification).where(Notification.user_id == employee_user.id)
        )
        notifications = result.scalars().all()
        assert len(notifications) == 1
        assert notifications[0].type == "test_assigned"
        assert notifications[0].organization_id == org.id
        assert notifications[0].payload["test_template_id"] == tpl["id"]

    async def test_assign_upsert_updates_due_at_not_results(
        self, client: AsyncClient, owner_headers, org, employee_member, db_session
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        first = _data(
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        )
        assignment_id = first["items"][0]["id"]

        second = await _assign(
            client,
            owner_headers,
            org.id,
            tpl["id"],
            [str(employee_member.id)],
            due_at="2027-01-01T00:00:00Z",
        )
        assert second.status_code == 201
        data = _data(second)
        assert data["created"] == 0
        assert data["updated"] == 1
        assert data["items"][0]["id"] == assignment_id
        assert data["items"][0]["due_at"].startswith("2027-01-01")

        # уведомление создано только один раз (для нового назначения)
        result = await db_session.execute(select(Notification))
        assert len(result.scalars().all()) == 1

    async def test_assign_deleted_template_rejected(
        self, client: AsyncClient, owner_headers, org, employee_member
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )
        resp = await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        assert resp.status_code == 400
        assert _err(resp) == "TEST_TEMPLATE_DELETED"

    async def test_assign_unknown_member_not_found(self, client: AsyncClient, owner_headers, org):
        tpl = await _create_template(client, owner_headers, org.id)
        resp = await _assign(client, owner_headers, org.id, tpl["id"], [str(uuid.uuid4())])
        assert resp.status_code == 404
        assert _err(resp) == "MEMBER_NOT_FOUND"

    async def test_list_template_assignments(
        self, client: AsyncClient, owner_headers, org, employee_member, emp2_member
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        await _assign(
            client,
            owner_headers,
            org.id,
            tpl["id"],
            [str(employee_member.id), str(emp2_member.id)],
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}/assignments",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        assert len(_data(resp)["items"]) == 2

    async def test_org_registry_filters(
        self, client: AsyncClient, owner_headers, org, employee_member, emp2_member
    ):
        tpl1 = await _create_template(client, owner_headers, org.id)
        tpl2 = await _create_template(
            client, owner_headers, org.id, {**TWO_QUESTION_BODY, "title": "Второй тест"}
        )
        await _assign(client, owner_headers, org.id, tpl1["id"], [str(employee_member.id)])
        await _assign(client, owner_headers, org.id, tpl2["id"], [str(emp2_member.id)])

        all_resp = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-assignments", headers=owner_headers
            )
        )
        assert all_resp["total"] == 2
        assert all_resp["items"][0]["template"] is not None

        by_template = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-assignments",
                headers=owner_headers,
                params={"template_id": tpl1["id"]},
            )
        )
        assert by_template["total"] == 1

        by_member = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-assignments",
                headers=owner_headers,
                params={"member_id": str(emp2_member.id)},
            )
        )
        assert by_member["total"] == 1

        by_status = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-assignments",
                headers=owner_headers,
                params={"status": "assigned"},
            )
        )
        assert by_status["total"] == 2

    async def test_delete_assignment_without_attempts(
        self, client: AsyncClient, owner_headers, org, employee_member, db_session: AsyncSession
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        assignment = _data(
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        )["items"][0]
        resp = await client.delete(
            f"/api/v1/organizations/{org.id}/test-assignments/{assignment['id']}",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        assert _data(resp) == {"deleted": True}

        row = await db_session.execute(
            select(TestAssignment).where(TestAssignment.id == uuid.UUID(assignment["id"]))
        )
        assert row.scalar_one_or_none() is None

    async def test_delete_assignment_in_progress_attempt_cascades(
        self,
        client: AsyncClient,
        owner_headers,
        employee_headers,
        org,
        employee_member,
        db_session: AsyncSession,
    ):
        """Снятие назначения с открытой (in_progress) попыткой удаляет её и снимки
        вопросов молча — никакого статуса «отозван» (backend.md test_assignment_unassign)."""
        tpl = await _create_template(client, owner_headers, org.id)
        assignment = _data(
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        )["items"][0]
        fill = _data(
            await client.post(
                f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
                headers=employee_headers,
            )
        )
        attempt_id = uuid.UUID(fill["id"])

        resp = await client.delete(
            f"/api/v1/organizations/{org.id}/test-assignments/{assignment['id']}",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        assert _data(resp) == {"deleted": True}

        attempt_row = await db_session.execute(
            select(TestAttempt).where(TestAttempt.id == attempt_id)
        )
        assert attempt_row.scalar_one_or_none() is None
        question_rows = await db_session.execute(
            select(TestAttemptQuestion).where(TestAttemptQuestion.attempt_id == attempt_id)
        )
        assert question_rows.scalars().all() == []

    async def test_delete_assignment_submitted_passed_cascades(
        self,
        client: AsyncClient,
        owner_headers,
        employee_headers,
        org,
        employee_member,
        db_session: AsyncSession,
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        assignment = _data(
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        )["items"][0]
        await _submit_first_correct(client, employee_headers, assignment["id"])

        resp = await client.delete(
            f"/api/v1/organizations/{org.id}/test-assignments/{assignment['id']}",
            headers=owner_headers,
        )
        assert resp.status_code == 200
        assert _data(resp) == {"deleted": True}

        assignment_row = await db_session.execute(
            select(TestAssignment).where(TestAssignment.id == uuid.UUID(assignment["id"]))
        )
        assert assignment_row.scalar_one_or_none() is None
        attempt_rows = await db_session.execute(
            select(TestAttempt).where(TestAttempt.assignment_id == uuid.UUID(assignment["id"]))
        )
        assert attempt_rows.scalars().all() == []

    async def test_delete_assignment_removes_only_its_notification(
        self,
        client: AsyncClient,
        owner_headers,
        org,
        employee_member,
        employee_user,
        db_session: AsyncSession,
    ):
        tpl1 = await _create_template(client, owner_headers, org.id)
        tpl2 = await _create_template(
            client, owner_headers, org.id, {**TWO_QUESTION_BODY, "title": "Второй тест"}
        )
        assignment1 = _data(
            await _assign(client, owner_headers, org.id, tpl1["id"], [str(employee_member.id)])
        )["items"][0]
        assignment2 = _data(
            await _assign(client, owner_headers, org.id, tpl2["id"], [str(employee_member.id)])
        )["items"][0]

        # уведомление ДРУГОГО типа с тем же assignment_id в payload — не должно задеться.
        db_session.add(
            Notification(
                user_id=employee_user.id,
                organization_id=org.id,
                type="shift_manual_changed",
                title="Другое уведомление",
                payload={"assignment_id": assignment1["id"]},
            )
        )
        await db_session.commit()

        resp = await client.delete(
            f"/api/v1/organizations/{org.id}/test-assignments/{assignment1['id']}",
            headers=owner_headers,
        )
        assert resp.status_code == 200

        remaining = (
            (await db_session.execute(select(Notification).order_by(Notification.created_at)))
            .scalars()
            .all()
        )
        assert len(remaining) == 2
        by_type = {n.type: n for n in remaining}
        assert by_type["test_assigned"].payload["assignment_id"] == assignment2["id"]
        assert by_type["shift_manual_changed"].payload["assignment_id"] == assignment1["id"]

    async def test_delete_assignment_repeat_returns_404(
        self, client: AsyncClient, owner_headers, org, employee_member
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        assignment = _data(
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        )["items"][0]
        first = await client.delete(
            f"/api/v1/organizations/{org.id}/test-assignments/{assignment['id']}",
            headers=owner_headers,
        )
        assert first.status_code == 200

        second = await client.delete(
            f"/api/v1/organizations/{org.id}/test-assignments/{assignment['id']}",
            headers=owner_headers,
        )
        assert second.status_code == 404
        assert _err(second) == "TEST_ASSIGNMENT_NOT_FOUND"

    async def test_delete_assignment_wrong_org_returns_404(
        self,
        client: AsyncClient,
        owner: User,
        owner_headers,
        org,
        employee_member,
        db_session: AsyncSession,
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        assignment = _data(
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        )["items"][0]

        other_org = Organization(name="Другая организация", owner_id=owner.id)
        db_session.add(other_org)
        await db_session.commit()

        resp = await client.delete(
            f"/api/v1/organizations/{other_org.id}/test-assignments/{assignment['id']}",
            headers=owner_headers,
        )
        assert resp.status_code == 404
        assert _err(resp) == "TEST_ASSIGNMENT_NOT_FOUND"

    async def test_delete_assignment_employee_forbidden(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        assignment = _data(
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        )["items"][0]
        resp = await client.delete(
            f"/api/v1/organizations/{org.id}/test-assignments/{assignment['id']}",
            headers=employee_headers,
        )
        assert resp.status_code == 403

    async def test_reassign_after_delete_creates_fresh_assignment(
        self,
        client: AsyncClient,
        owner_headers,
        employee_headers,
        org,
        employee_member,
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        assignment = _data(
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        )["items"][0]
        await _submit_first_correct(client, employee_headers, assignment["id"])

        await client.delete(
            f"/api/v1/organizations/{org.id}/test-assignments/{assignment['id']}",
            headers=owner_headers,
        )

        resp = await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        assert resp.status_code == 201, resp.text
        data = _data(resp)
        assert data["created"] == 1
        assert data["updated"] == 0
        new_assignment = data["items"][0]
        assert new_assignment["id"] != assignment["id"]
        assert new_assignment["attempts_used"] == 0
        assert new_assignment["status"] == "assigned"
        assert new_assignment["passed"] is False

    async def test_org_registry_hides_deleted_template_by_default(
        self, client: AsyncClient, owner_headers, org, employee_member, emp2_member
    ):
        tpl1 = await _create_template(client, owner_headers, org.id)
        tpl2 = await _create_template(
            client, owner_headers, org.id, {**TWO_QUESTION_BODY, "title": "Второй тест"}
        )
        await _assign(client, owner_headers, org.id, tpl1["id"], [str(employee_member.id)])
        await _assign(client, owner_headers, org.id, tpl2["id"], [str(emp2_member.id)])

        await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl1['id']}",
            headers=owner_headers,
        )

        default_view = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-assignments", headers=owner_headers
            )
        )
        assert default_view["total"] == 1
        assert default_view["items"][0]["template"]["id"] == tpl2["id"]

        with_deleted = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-assignments",
                headers=owner_headers,
                params={"include_deleted": "true"},
            )
        )
        assert with_deleted["total"] == 2
        assert {item["template"]["id"] for item in with_deleted["items"]} == {
            tpl1["id"],
            tpl2["id"],
        }

    async def test_restore_template_returns_assignments_to_both_registries(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])

        await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )
        assert (
            _data(await client.get("/api/v1/my/test-assignments", headers=employee_headers))[
                "total"
            ]
            == 0
        )
        assert (
            _data(
                await client.get(
                    f"/api/v1/organizations/{org.id}/test-assignments", headers=owner_headers
                )
            )["total"]
            == 0
        )

        restore_resp = await client.post(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}/restore",
            headers=owner_headers,
        )
        assert restore_resp.status_code == 200

        assert (
            _data(await client.get("/api/v1/my/test-assignments", headers=employee_headers))[
                "total"
            ]
            == 1
        )
        assert (
            _data(
                await client.get(
                    f"/api/v1/organizations/{org.id}/test-assignments", headers=owner_headers
                )
            )["total"]
            == 1
        )

    async def test_employee_cannot_manage_assignments(
        self, client: AsyncClient, employee_headers, employee_member, org, owner_headers
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        resp = await _assign(
            client, employee_headers, org.id, tpl["id"], [str(employee_member.id)]
        )
        assert resp.status_code == 403


# --- Прохождение попытки ------------------------------------------------------------
class TestAttemptLifecycle:
    async def _setup_assignment(
        self, client, owner_headers, org, member, body=None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        tpl = await _create_template(client, owner_headers, org.id, body)
        assign_resp = await _assign(client, owner_headers, org.id, tpl["id"], [str(member.id)])
        assignment = _data(assign_resp)["items"][0]
        return tpl, assignment

    async def test_start_attempt_hides_is_correct(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        resp = await client.post(
            f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
            headers=employee_headers,
        )
        assert resp.status_code == 201
        data = _data(resp)
        assert data["assignment_id"] == assignment["id"]
        assert len(data["questions"]) == 2
        for q in data["questions"]:
            for opt in q["options"]:
                assert "is_correct" not in opt

    async def test_start_attempt_idempotent_returns_same_open_attempt(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        first = _data(
            await client.post(
                f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
                headers=employee_headers,
            )
        )
        second = _data(
            await client.post(
                f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
                headers=employee_headers,
            )
        )
        assert first["id"] == second["id"]

    async def test_start_attempt_deleted_template(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        tpl, assignment = await self._setup_assignment(client, owner_headers, org, employee_member)
        await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )
        resp = await client.post(
            f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
            headers=employee_headers,
        )
        assert resp.status_code == 400
        assert _err(resp) == "TEST_TEMPLATE_DELETED"

    async def test_other_user_cannot_see_assignment(
        self, client: AsyncClient, owner_headers, emp2_headers, org, employee_member, emp2_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        resp = await client.get(
            f"/api/v1/my/test-assignments/{assignment['id']}", headers=emp2_headers
        )
        assert resp.status_code == 404
        assert _err(resp) == "TEST_ASSIGNMENT_NOT_FOUND"

    async def _start_and_submit(
        self,
        client: AsyncClient,
        headers: dict[str, str],
        assignment_id: str,
        answers: dict[str, str | list[str]],
    ) -> Any:
        """`answers` — {question_text: option_text | [option_text, ...]}."""
        fill = _data(
            await client.post(
                f"/api/v1/my/test-assignments/{assignment_id}/attempts", headers=headers
            )
        )
        payload = []
        for q in fill["questions"]:
            if q["text"] not in answers:
                continue
            wanted = answers[q["text"]]
            wanted_list = [wanted] if isinstance(wanted, str) else wanted
            selected = [_option_id(q, w) for w in wanted_list]
            payload.append({"attempt_question_id": q["id"], "selected_option_ids": selected})
        return await client.post(
            f"/api/v1/my/test-attempts/{fill['id']}/submit",
            headers=headers,
            json={"answers": payload},
        )

    async def test_submit_all_correct_passes(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        resp = await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {
                "Что делать при пожаре?": "Вызвать 101",
                "Какие СИЗ обязательны?": ["Каска", "Перчатки"],
            },
        )
        assert resp.status_code == 200
        data = _data(resp)
        assert data["score"] == 2
        assert data["max_score"] == 2
        assert data["percent"] == 100
        assert data["passed"] is True
        assert data["attempts_used"] == 1
        assert data["attempts_left"] == 1
        assert data["reveal_answers"] is True
        assert data["questions"] is not None
        assert data["questions"][0]["options"][0]["is_correct"] is not None

    async def test_submit_partial_multiple_choice_all_or_nothing(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        """multiple_choice: выбрать только один из двух верных → 0 баллов за вопрос."""
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        resp = await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {
                "Что делать при пожаре?": "Вызвать 101",
                "Какие СИЗ обязательны?": ["Каска"],  # неполный набор
            },
        )
        data = _data(resp)
        assert data["score"] == 1  # только первый вопрос засчитан
        assert data["percent"] == 50
        assert data["passed"] is True  # порог 50

    async def test_submit_wrong_answer_fails(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        resp = await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {"Что делать при пожаре?": "Игнорировать"},
        )
        data = _data(resp)
        assert data["score"] == 0
        assert data["percent"] == 0
        assert data["passed"] is False

    async def test_unanswered_question_scores_zero(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        resp = await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {"Что делать при пожаре?": "Вызвать 101"},  # второй вопрос не отвечен
        )
        data = _data(resp)
        assert data["score"] == 1
        assert data["percent"] == 50

    async def test_submit_invalid_option_id_rejected(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        fill = _data(
            await client.post(
                f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
                headers=employee_headers,
            )
        )
        resp = await client.post(
            f"/api/v1/my/test-attempts/{fill['id']}/submit",
            headers=employee_headers,
            json={
                "answers": [
                    {
                        "attempt_question_id": fill["questions"][0]["id"],
                        "selected_option_ids": [str(uuid.uuid4())],
                    }
                ]
            },
        )
        assert resp.status_code == 422
        assert _err(resp) == "VALIDATION_ERROR"

    async def test_submit_twice_rejected(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        fill = _data(
            await client.post(
                f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
                headers=employee_headers,
            )
        )
        resp1 = await client.post(
            f"/api/v1/my/test-attempts/{fill['id']}/submit",
            headers=employee_headers,
            json={"answers": []},
        )
        assert resp1.status_code == 200
        resp2 = await client.post(
            f"/api/v1/my/test-attempts/{fill['id']}/submit",
            headers=employee_headers,
            json={"answers": []},
        )
        assert resp2.status_code == 409
        assert _err(resp2) == "TEST_ATTEMPT_ALREADY_SUBMITTED"

    async def test_denorm_status_passed_then_already_passed_guard(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {
                "Что делать при пожаре?": "Вызвать 101",
                "Какие СИЗ обязательны?": ["Каска", "Перчатки"],
            },
        )
        detail = _data(
            await client.get(
                f"/api/v1/my/test-assignments/{assignment['id']}", headers=employee_headers
            )
        )
        assert detail["status"] == "passed"
        assert detail["passed"] is True
        assert detail["best_percent"] == 100
        assert len(detail["attempts"]) == 1

        resp = await client.post(
            f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
            headers=employee_headers,
        )
        assert resp.status_code == 409
        assert _err(resp) == "TEST_ALREADY_PASSED"

    async def test_denorm_status_failed_after_exhausting_attempts(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        body = {**TWO_QUESTION_BODY, "max_attempts": 1}
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member, body
        )
        await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {"Что делать при пожаре?": "Игнорировать"},
        )
        detail = _data(
            await client.get(
                f"/api/v1/my/test-assignments/{assignment['id']}", headers=employee_headers
            )
        )
        assert detail["status"] == "failed"
        assert detail["passed"] is False

        resp = await client.post(
            f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
            headers=employee_headers,
        )
        assert resp.status_code == 409
        assert _err(resp) == "TEST_ATTEMPTS_EXHAUSTED"

    async def test_best_percent_is_max_across_attempts(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        body = {**TWO_QUESTION_BODY, "max_attempts": 2, "pass_threshold_percent": 100}
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member, body
        )
        await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {"Что делать при пожаре?": "Вызвать 101"},  # 50%, не сдан
        )
        await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {
                "Что делать при пожаре?": "Вызвать 101",
                "Какие СИЗ обязательны?": ["Каска", "Перчатки"],
            },  # 100%, сдан
        )
        detail = _data(
            await client.get(
                f"/api/v1/my/test-assignments/{assignment['id']}", headers=employee_headers
            )
        )
        assert detail["best_percent"] == 100
        assert detail["attempts_used"] == 2
        assert detail["passed"] is True
        assert detail["status"] == "passed"

    async def test_reveal_answers_false_hides_correctness(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        body = {**TWO_QUESTION_BODY, "reveal_answers": False}
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member, body
        )
        resp = await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {"Что делать при пожаре?": "Вызвать 101"},
        )
        data = _data(resp)
        assert data["reveal_answers"] is False
        assert data["questions"] is None

        # id попытки нет в SubmitResponse — проверяем результат через my-assignments detail
        detail = _data(
            await client.get(
                f"/api/v1/my/test-assignments/{assignment['id']}", headers=employee_headers
            )
        )
        assert detail["attempts"][0]["percent"] == 50

    async def test_snapshot_independent_of_later_template_edit(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        tpl, assignment = await self._setup_assignment(client, owner_headers, org, employee_member)
        fill = _data(
            await client.post(
                f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
                headers=employee_headers,
            )
        )
        # Меняем шаблон ПОСЛЕ старта попытки: правильный ответ и порог другие.
        await client.patch(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
            json={
                "pass_threshold_percent": 100,
                "questions": [
                    {
                        "text": "Другой вопрос",
                        "type": "single_choice",
                        "options": [
                            {"text": "X", "is_correct": True},
                            {"text": "Y", "is_correct": False},
                        ],
                    }
                ],
            },
        )
        # Снимок попытки — старые 2 вопроса, старый ответ "Вызвать 101" верный.
        payload = [
            {
                "attempt_question_id": fill["questions"][0]["id"],
                "selected_option_ids": [_option_id(fill["questions"][0], "Вызвать 101")],
            }
        ]
        resp = await client.post(
            f"/api/v1/my/test-attempts/{fill['id']}/submit",
            headers=employee_headers,
            json={"answers": payload},
        )
        assert resp.status_code == 200
        data = _data(resp)
        # Порог снимка (50), не новый (100) — 50% проходит.
        assert data["pass_threshold_percent"] == 50
        assert data["percent"] == 50
        assert data["passed"] is True

    async def test_admin_attempt_review(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        submit_resp = await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {"Что делать при пожаре?": "Вызвать 101"},
        )
        assert submit_resp.status_code == 200

        detail = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-assignments",
                headers=owner_headers,
            )
        )
        assert detail["total"] == 1
        registry_item = detail["items"][0]

        # Реестр отдаёт last_attempt_id — по нему открывается разбор попытки
        # (без него из GET .../test-assignments нет пути к GET .../test-attempts/{id}).
        my_detail = _data(
            await client.get(
                f"/api/v1/my/test-assignments/{assignment['id']}", headers=employee_headers
            )
        )
        assert len(my_detail["attempts"]) == 1
        assert registry_item["last_attempt_id"] is not None

        review = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-attempts/{registry_item['last_attempt_id']}",
                headers=owner_headers,
            )
        )
        assert review["assignment_id"] == assignment["id"]
        assert review["status"] == "submitted"
        assert review["percent"] == 50

    async def test_last_attempt_id_null_before_any_submission(
        self, client: AsyncClient, owner_headers, org, employee_member
    ):
        _tpl, _assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        items = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-assignments", headers=owner_headers
            )
        )["items"]
        assert items[0]["last_attempt_id"] is None

    async def test_last_attempt_id_tracks_latest_submitted_attempt(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        """При нескольких попытках last_attempt_id указывает на ПОСЛЕДНЮЮ сданную,
        а не на первую — реестр обязан открывать разбор самой свежей попытки."""
        body = {**TWO_QUESTION_BODY, "max_attempts": 2, "pass_threshold_percent": 100}
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member, body
        )
        await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {"Что делать при пожаре?": "Вызвать 101"},  # 50%, первая попытка
        )
        first_attempt_id = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-templates/{_tpl['id']}/assignments",
                headers=owner_headers,
            )
        )["items"][0]["last_attempt_id"]

        await self._start_and_submit(
            client,
            employee_headers,
            assignment["id"],
            {
                "Что делать при пожаре?": "Вызвать 101",
                "Какие СИЗ обязательны?": ["Каска", "Перчатки"],
            },  # 100%, вторая попытка
        )
        second_item = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-templates/{_tpl['id']}/assignments",
                headers=owner_headers,
            )
        )["items"][0]
        assert second_item["last_attempt_id"] is not None
        assert second_item["last_attempt_id"] != first_attempt_id

        review = _data(
            await client.get(
                f"/api/v1/organizations/{org.id}/test-attempts/{second_item['last_attempt_id']}",
                headers=owner_headers,
            )
        )
        assert review["percent"] == 100  # разбор именно последней (успешной) попытки

    async def test_employee_cannot_review_others_attempt_via_admin_endpoint(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        # employee не admin/owner — не может дёргать /test-attempts/{id} (админский эндпоинт)
        _tpl, assignment = await self._setup_assignment(
            client, owner_headers, org, employee_member
        )
        fill = _data(
            await client.post(
                f"/api/v1/my/test-assignments/{assignment['id']}/attempts",
                headers=employee_headers,
            )
        )
        resp = await client.get(
            f"/api/v1/organizations/{org.id}/test-attempts/{fill['id']}",
            headers=employee_headers,
        )
        assert resp.status_code == 403


# --- Мои назначения: пагинированный реестр сотрудника -------------------------------
class TestMyAssignmentsList:
    """GET /my/test-assignments — пагинированный конверт {items, total, limit, offset}."""

    async def test_empty_list_envelope(
        self, client: AsyncClient, employee_headers, employee_member
    ):
        resp = await client.get("/api/v1/my/test-assignments", headers=employee_headers)
        assert resp.status_code == 200
        assert _data(resp) == {"items": [], "total": 0, "limit": 20, "offset": 0}

    async def test_single_item_envelope_and_shape(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])

        data = _data(await client.get("/api/v1/my/test-assignments", headers=employee_headers))
        assert data["total"] == 1
        assert data["limit"] == 20
        assert data["offset"] == 0
        assert len(data["items"]) == 1

        item = data["items"][0]
        assert item["status"] == "assigned"
        assert item["attempts_used"] == 0
        assert item["best_percent"] is None
        assert item["passed"] is False
        assert item["template"]["id"] == tpl["id"]
        assert item["template"]["question_count"] == 2
        assert item["template"]["max_attempts"] == TWO_QUESTION_BODY["max_attempts"]
        assert item["template"]["pass_threshold_percent"] == 50
        assert item["template"]["shuffle_questions"] is False
        assert item["organization"] == {"id": str(org.id), "name": "Tests Org"}

    async def test_pagination_offset_limit_total_stable(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        # UNIQUE(template_id, member_id) → пять разных шаблонов на одного сотрудника.
        for i in range(5):
            tpl = await _create_template(
                client, owner_headers, org.id, {**TWO_QUESTION_BODY, "title": f"Тест {i}"}
            )
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])

        seen: list[str] = []
        for offset, expected_len in ((0, 2), (2, 2), (4, 1)):
            page = _data(
                await client.get(
                    "/api/v1/my/test-assignments",
                    headers=employee_headers,
                    params={"limit": 2, "offset": offset},
                )
            )
            assert page["total"] == 5  # total — не длина страницы
            assert page["limit"] == 2
            assert page["offset"] == offset
            assert len(page["items"]) == expected_len
            seen.extend(item["id"] for item in page["items"])

        # Страницы не перекрываются и покрывают все назначения ровно один раз.
        assert len(seen) == 5
        assert len(set(seen)) == 5

    async def test_offset_beyond_total_returns_empty_page(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        page = _data(
            await client.get(
                "/api/v1/my/test-assignments",
                headers=employee_headers,
                params={"limit": 20, "offset": 20},
            )
        )
        assert page["total"] == 1
        assert page["items"] == []

    async def test_limit_over_max_rejected(
        self, client: AsyncClient, employee_headers, employee_member
    ):
        resp = await client.get(
            "/api/v1/my/test-assignments", headers=employee_headers, params={"limit": 51}
        )
        assert resp.status_code == 422
        assert _err(resp) == "VALIDATION_ERROR"

    async def test_filter_org_and_status_with_pagination(
        self,
        client: AsyncClient,
        owner,
        owner_headers,
        employee_headers,
        employee_user,
        org,
        employee_member,
        db_session: AsyncSession,
    ):
        # org #1: одно назначение остаётся assigned, второе доводим до passed.
        tpl_a = await _create_template(
            client, owner_headers, org.id, {**TWO_QUESTION_BODY, "title": "A"}
        )
        await _assign(client, owner_headers, org.id, tpl_a["id"], [str(employee_member.id)])
        tpl_b = await _create_template(
            client, owner_headers, org.id, {**TWO_QUESTION_BODY, "title": "B"}
        )
        assign_b = _data(
            await _assign(client, owner_headers, org.id, tpl_b["id"], [str(employee_member.id)])
        )["items"][0]
        await _submit_first_correct(client, employee_headers, assign_b["id"])

        # org #2 (тот же сотрудник — член двух организаций): своё assigned-назначение.
        org2 = Organization(name="Org Two", owner_id=owner.id)
        db_session.add(org2)
        await db_session.commit()
        member2 = OrganizationMember(
            organization_id=org2.id, user_id=employee_user.id, role=MemberRole.employee
        )
        db_session.add(member2)
        await db_session.commit()
        tpl_c = await _create_template(
            client, owner_headers, org2.id, {**TWO_QUESTION_BODY, "title": "C"}
        )
        await _assign(client, owner_headers, org2.id, tpl_c["id"], [str(member2.id)])

        # Без фильтра — все три назначения по двум организациям.
        all_data = _data(await client.get("/api/v1/my/test-assignments", headers=employee_headers))
        assert all_data["total"] == 3

        # Фильтр по организации сужает до её назначений.
        org1_data = _data(
            await client.get(
                "/api/v1/my/test-assignments",
                headers=employee_headers,
                params={"organization_id": str(org.id)},
            )
        )
        assert org1_data["total"] == 2
        assert all(it["organization"]["id"] == str(org.id) for it in org1_data["items"])

        # Фильтр организация + статус + пагинация вместе.
        assigned = _data(
            await client.get(
                "/api/v1/my/test-assignments",
                headers=employee_headers,
                params={
                    "organization_id": str(org.id),
                    "status": "assigned",
                    "limit": 1,
                    "offset": 0,
                },
            )
        )
        assert assigned["total"] == 1
        assert assigned["limit"] == 1
        assert assigned["offset"] == 0
        assert len(assigned["items"]) == 1
        assert assigned["items"][0]["template"]["id"] == tpl_a["id"]
        assert assigned["items"][0]["status"] == "assigned"

    async def test_deleted_template_assignment_hidden_from_list(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        """Назначение мягко удалённого шаблона не отдаётся ни в items, ни в total
        (backend.md test_assignment_unassign) — для сотрудника его как будто нет."""
        tpl1 = await _create_template(client, owner_headers, org.id)
        tpl2 = await _create_template(
            client, owner_headers, org.id, {**TWO_QUESTION_BODY, "title": "Второй тест"}
        )
        await _assign(client, owner_headers, org.id, tpl1["id"], [str(employee_member.id)])
        await _assign(client, owner_headers, org.id, tpl2["id"], [str(employee_member.id)])

        await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl1['id']}",
            headers=owner_headers,
        )

        data = _data(await client.get("/api/v1/my/test-assignments", headers=employee_headers))
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["template"]["id"] == tpl2["id"]

    async def test_deleted_template_assignment_detail_not_found(
        self, client: AsyncClient, owner_headers, employee_headers, org, employee_member
    ):
        tpl = await _create_template(client, owner_headers, org.id)
        assignment = _data(
            await _assign(client, owner_headers, org.id, tpl["id"], [str(employee_member.id)])
        )["items"][0]

        await client.delete(
            f"/api/v1/organizations/{org.id}/test-templates/{tpl['id']}",
            headers=owner_headers,
        )

        resp = await client.get(
            f"/api/v1/my/test-assignments/{assignment['id']}", headers=employee_headers
        )
        assert resp.status_code == 404
        assert _err(resp) == "TEST_ASSIGNMENT_NOT_FOUND"
