"""Тестирование сотрудников: шаблон → вопросы/варианты → назначение →
попытка-снимок с результатом. Повторяет паттерн чек-листов (шаблон → снимок,
см. `services/checklist_template.py`/`services/checklist_instance.py`).

Назначение теста создаёт уведомление `test_assigned` через `services/notification`
(зависимость фичи `notifications`, backend.md employee_tests).
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.logging import get_logger
from src.app.models.employee_test import (
    TestAssignment,
    TestAssignmentStatus,
    TestAttempt,
    TestAttemptOption,
    TestAttemptQuestion,
    TestAttemptStatus,
    TestQuestion,
    TestQuestionOption,
    TestQuestionType,
    TestTemplate,
)
from src.app.models.notification import Notification, NotificationType
from src.app.models.organization import OrganizationMember
from src.app.services import entitlements
from src.app.services import notification as notification_service
from src.app.services.common import ensure_admin_or_owner
from src.app.services.notification import NotificationInput
from src.app.services.organization import get_organization
from src.app.services.shift import ensure_utc

logger = get_logger(__name__)

_ADMIN_MESSAGE = "Нет прав для управления тестами"


class TestError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


# --- Тело questions/options на входе (без Pydantic-констрейнтов, см. schemas) --
@dataclass(slots=True)
class QuestionOptionInput:
    text: str
    is_correct: bool = False
    position: int | None = None


@dataclass(slots=True)
class QuestionInput:
    text: str
    type: str
    points: int = 1
    position: int | None = None
    options: list[QuestionOptionInput] = field(default_factory=list)


# --- Валидация инвариантов шаблона --------------------------------------------
def _parse_question_type(raw: str, idx: int) -> TestQuestionType:
    try:
        return TestQuestionType(raw)
    except ValueError:
        raise TestError(
            "TEST_TEMPLATE_INVALID",
            f"Вопрос {idx}: недопустимый тип {raw!r} (single_choice | multiple_choice)",
            422,
        ) from None


def validate_template_payload(
    *,
    pass_threshold_percent: int,
    max_attempts: int,
    questions: list[QuestionInput],
) -> tuple[int, int]:
    """Проверяет инварианты шаблона (см. backend.md employee_tests). Возвращает
    (question_count, total_points). Нарушение → TEST_TEMPLATE_INVALID (422)."""
    if not (0 <= pass_threshold_percent <= 100):
        raise TestError(
            "TEST_TEMPLATE_INVALID", "pass_threshold_percent должен быть в диапазоне 0..100", 422
        )
    if max_attempts < 1:
        raise TestError("TEST_TEMPLATE_INVALID", "max_attempts должен быть >= 1", 422)
    if not questions:
        raise TestError("TEST_TEMPLATE_INVALID", "Нужен минимум один вопрос", 422)

    total_points = 0
    for idx, q in enumerate(questions, start=1):
        if not q.text or not q.text.strip():
            raise TestError("TEST_TEMPLATE_INVALID", f"Вопрос {idx}: текст обязателен", 422)
        q_type = _parse_question_type(q.type, idx)
        if q.points < 1:
            raise TestError("TEST_TEMPLATE_INVALID", f"Вопрос {idx}: points должен быть >= 1", 422)
        if len(q.options) < 2:
            raise TestError(
                "TEST_TEMPLATE_INVALID", f"Вопрос {idx}: нужно минимум 2 варианта ответа", 422
            )
        correct_count = 0
        for opt in q.options:
            if not opt.text or not opt.text.strip():
                raise TestError(
                    "TEST_TEMPLATE_INVALID", f"Вопрос {idx}: текст варианта обязателен", 422
                )
            if opt.is_correct:
                correct_count += 1
        if q_type == TestQuestionType.single_choice and correct_count != 1:
            raise TestError(
                "TEST_TEMPLATE_INVALID",
                f"Вопрос {idx}: single_choice требует ровно один верный вариант",
                422,
            )
        if q_type == TestQuestionType.multiple_choice and correct_count < 1:
            raise TestError(
                "TEST_TEMPLATE_INVALID",
                f"Вопрос {idx}: multiple_choice требует хотя бы один верный вариант",
                422,
            )
        total_points += q.points
    return len(questions), total_points


def _build_template_questions(questions_in: list[QuestionInput]) -> list[TestQuestion]:
    built: list[TestQuestion] = []
    for idx, q in enumerate(questions_in):
        option_models = [
            TestQuestionOption(
                text=o.text,
                is_correct=o.is_correct,
                position=o.position if o.position is not None else oidx,
            )
            for oidx, o in enumerate(q.options)
        ]
        built.append(
            TestQuestion(
                text=q.text,
                type=TestQuestionType(q.type),
                points=q.points,
                position=q.position if q.position is not None else idx,
                options=option_models,
            )
        )
    return built


# --- Шаблоны -------------------------------------------------------------------
async def _get_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    *,
    with_questions: bool = False,
) -> TestTemplate:
    query = select(TestTemplate).where(
        TestTemplate.id == template_id,
        TestTemplate.organization_id == org_id,
    )
    if with_questions:
        query = query.options(
            selectinload(TestTemplate.questions).selectinload(TestQuestion.options)
        )
    result = await session.execute(query)
    template = result.scalar_one_or_none()
    if template is None:
        raise TestError("TEST_TEMPLATE_NOT_FOUND", "Шаблон теста не найден", 404)
    return template


async def create_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    title: str,
    description: str | None,
    pass_threshold_percent: int,
    max_attempts: int,
    reveal_answers: bool,
    shuffle_questions: bool,
    questions: list[QuestionInput],
) -> TestTemplate:
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)
    await entitlements.require_active_subscription(session, org, requester_id)

    if not title or not title.strip():
        raise TestError("TEST_TEMPLATE_INVALID", "title обязателен", 422)
    validate_template_payload(
        pass_threshold_percent=pass_threshold_percent,
        max_attempts=max_attempts,
        questions=questions,
    )

    template = TestTemplate(
        organization_id=org_id,
        title=title,
        description=description,
        pass_threshold_percent=pass_threshold_percent,
        max_attempts=max_attempts,
        reveal_answers=reveal_answers,
        shuffle_questions=shuffle_questions,
        questions=_build_template_questions(questions),
    )
    session.add(template)
    await session.flush()
    logger.info("test_template_created", org_id=str(org_id), template_id=str(template.id))
    return template


async def validate_template_only(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    pass_threshold_percent: int,
    max_attempts: int,
    questions: list[QuestionInput],
) -> tuple[int, int]:
    """Сухая проверка без записи в БД. Возвращает (question_count, total_points)."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)
    await entitlements.require_feature(session, org, entitlements.PlanFeature.test_import)
    return validate_template_payload(
        pass_threshold_percent=pass_threshold_percent,
        max_attempts=max_attempts,
        questions=questions,
    )


async def list_templates(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    limit: int = 20,
    offset: int = 0,
    include_deleted: bool = False,
) -> tuple[list[tuple[TestTemplate, int, int, int]], int]:
    """Возвращает [(template, question_count, total_points, assignments_count)], total."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)

    conditions = [TestTemplate.organization_id == org_id]
    if not include_deleted:
        conditions.append(TestTemplate.is_deleted.is_(False))

    total = (
        await session.execute(select(func.count()).select_from(TestTemplate).where(*conditions))
    ).scalar_one()

    result = await session.execute(
        select(TestTemplate)
        .where(*conditions)
        .order_by(TestTemplate.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    templates = list(result.scalars().all())
    if not templates:
        return [], total

    template_ids = [t.id for t in templates]
    q_stats_result = await session.execute(
        select(
            TestQuestion.template_id,
            func.count(TestQuestion.id),
            func.coalesce(func.sum(TestQuestion.points), 0),
        )
        .where(TestQuestion.template_id.in_(template_ids))
        .group_by(TestQuestion.template_id)
    )
    q_stats = {row[0]: (row[1], int(row[2])) for row in q_stats_result.all()}

    a_counts_result = await session.execute(
        select(TestAssignment.template_id, func.count(TestAssignment.id))
        .where(TestAssignment.template_id.in_(template_ids))
        .group_by(TestAssignment.template_id)
    )
    a_counts = dict(a_counts_result.tuples().all())

    items = [(t, *q_stats.get(t.id, (0, 0)), a_counts.get(t.id, 0)) for t in templates]
    return items, total


async def get_template_detail(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> TestTemplate:
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)
    return await _get_template(session, org_id, template_id, with_questions=True)


async def update_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
    fields: dict[str, Any],
) -> TestTemplate:
    """`fields` — `model_dump(exclude_unset=True)` тела PATCH. Если ключ `questions`
    присутствует — полная замена набора вопросов (снимки уже сданных попыток не трогает)."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)
    await entitlements.require_active_subscription(session, org, requester_id)
    template = await _get_template(session, org_id, template_id, with_questions=True)
    if template.is_deleted:
        raise TestError(
            "TEST_TEMPLATE_DELETED", "Тест удалён — восстановите его, чтобы редактировать", 400
        )

    new_threshold = fields.get("pass_threshold_percent", template.pass_threshold_percent)
    new_max_attempts = fields.get("max_attempts", template.max_attempts)
    raw_questions = fields.get("questions")

    if "questions" in fields and raw_questions is not None:
        questions_in = [
            QuestionInput(
                text=q.text,
                type=q.type,
                points=q.points,
                position=q.position,
                options=[
                    QuestionOptionInput(text=o.text, is_correct=o.is_correct, position=o.position)
                    for o in q.options
                ],
            )
            for q in raw_questions
        ]
        validate_template_payload(
            pass_threshold_percent=new_threshold,
            max_attempts=new_max_attempts,
            questions=questions_in,
        )
    else:
        if not (0 <= new_threshold <= 100):
            raise TestError(
                "TEST_TEMPLATE_INVALID",
                "pass_threshold_percent должен быть в диапазоне 0..100",
                422,
            )
        if new_max_attempts < 1:
            raise TestError("TEST_TEMPLATE_INVALID", "max_attempts должен быть >= 1", 422)
        questions_in = None

    if "title" in fields:
        if not fields["title"] or not fields["title"].strip():
            raise TestError("TEST_TEMPLATE_INVALID", "title обязателен", 422)
        template.title = fields["title"]
    if "description" in fields:
        template.description = fields["description"]
    if "pass_threshold_percent" in fields and fields["pass_threshold_percent"] is not None:
        template.pass_threshold_percent = fields["pass_threshold_percent"]
    if "max_attempts" in fields and fields["max_attempts"] is not None:
        template.max_attempts = fields["max_attempts"]
    if "reveal_answers" in fields and fields["reveal_answers"] is not None:
        template.reveal_answers = fields["reveal_answers"]
    if "shuffle_questions" in fields and fields["shuffle_questions"] is not None:
        template.shuffle_questions = fields["shuffle_questions"]
    if questions_in is not None:
        template.questions = _build_template_questions(questions_in)

    await session.flush()
    logger.info("test_template_updated", org_id=str(org_id), template_id=str(template_id))
    return template


async def delete_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> TestTemplate:
    """Мягкое удаление. Повторный вызов на уже удалённом шаблоне — 404 (шаблон
    существует, но проверка ниже отклоняет его явно, чтобы отличать от
    восстановления)."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)
    await entitlements.require_active_subscription(session, org, requester_id)
    template = await _get_template(session, org_id, template_id, with_questions=True)
    if template.is_deleted:
        raise TestError("TEST_TEMPLATE_NOT_FOUND", "Шаблон теста не найден", 404)

    template.is_deleted = True
    template.deleted_at = datetime.now(UTC)
    template.deleted_by_user_id = requester_id
    await session.flush()
    logger.info(
        "test_template_deleted",
        org_id=str(org_id),
        template_id=str(template_id),
        deleted_by=str(requester_id),
    )
    return template


async def restore_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> TestTemplate:
    """Восстановление удалённого шаблона. На не-удалённом — 409 TEST_TEMPLATE_NOT_DELETED."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)
    await entitlements.require_active_subscription(session, org, requester_id)
    template = await _get_template(session, org_id, template_id, with_questions=True)
    if not template.is_deleted:
        raise TestError("TEST_TEMPLATE_NOT_DELETED", "Шаблон теста не удалён", 409)

    template.is_deleted = False
    template.deleted_at = None
    template.deleted_by_user_id = None
    await session.flush()
    logger.info(
        "test_template_restored",
        org_id=str(org_id),
        template_id=str(template_id),
    )
    return template


# --- Назначения ------------------------------------------------------------------
def _parse_assignment_status(raw: str) -> TestAssignmentStatus:
    try:
        return TestAssignmentStatus(raw)
    except ValueError:
        raise TestError("VALIDATION_ERROR", f"Недопустимый статус: {raw!r}", 422) from None


async def _get_org_assignment(
    session: AsyncSession,
    org_id: uuid.UUID,
    assignment_id: uuid.UUID,
) -> TestAssignment:
    result = await session.execute(
        select(TestAssignment)
        .join(TestTemplate, TestAssignment.template_id == TestTemplate.id)
        .where(TestAssignment.id == assignment_id, TestTemplate.organization_id == org_id)
    )
    assignment = result.scalar_one_or_none()
    if assignment is None:
        raise TestError("TEST_ASSIGNMENT_NOT_FOUND", "Назначение не найдено", 404)
    return assignment


async def assign_template(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    member_ids: list[uuid.UUID],
    due_at: datetime | None,
) -> tuple[list[TestAssignment], int, int]:
    """Upsert `(template_id, member_id)`. Для НОВОГО назначения создаёт
    уведомление `test_assigned` в той же транзакции (`notifications`).
    Возвращает (assignments, created, updated)."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)
    await entitlements.require_active_subscription(session, org, requester_id)
    template = await _get_template(session, org_id, template_id)
    if template.is_deleted:
        raise TestError(
            "TEST_TEMPLATE_DELETED", "Тест удалён — восстановите его, чтобы редактировать", 400
        )

    if not member_ids:
        return [], 0, 0

    unique_member_ids = list(dict.fromkeys(member_ids))
    members_result = await session.execute(
        select(OrganizationMember.id, OrganizationMember.user_id).where(
            OrganizationMember.id.in_(unique_member_ids),
            OrganizationMember.organization_id == org_id,
        )
    )
    member_rows = {row.id: row.user_id for row in members_result.all()}
    missing = set(unique_member_ids) - set(member_rows.keys())
    if missing:
        raise TestError("MEMBER_NOT_FOUND", "Участник не найден", 404)

    normalized_due_at = ensure_utc(due_at) if due_at is not None else None

    existing_result = await session.execute(
        select(TestAssignment).where(
            TestAssignment.template_id == template_id,
            TestAssignment.member_id.in_(unique_member_ids),
        )
    )
    existing_by_member = {a.member_id: a for a in existing_result.scalars().all()}

    assignments: list[TestAssignment] = []
    new_assignments: list[TestAssignment] = []
    created = 0
    updated = 0

    for member_id in unique_member_ids:
        existing = existing_by_member.get(member_id)
        if existing is not None:
            existing.due_at = normalized_due_at
            existing.assigned_by_user_id = requester_id
            assignments.append(existing)
            updated += 1
            continue

        assignment = TestAssignment(
            template_id=template_id,
            member_id=member_id,
            assigned_by_user_id=requester_id,
            due_at=normalized_due_at,
        )
        session.add(assignment)
        assignments.append(assignment)
        new_assignments.append(assignment)
        created += 1

    await session.flush()  # id новых assignment нужен в payload уведомления

    if new_assignments:
        notif_items = [
            NotificationInput(
                user_id=member_rows[a.member_id],
                type=NotificationType.test_assigned.value,
                title=f"Вам назначен тест «{template.title}»",
                body=None,
                payload={
                    "assignment_id": str(a.id),
                    "test_template_id": str(template.id),
                    "test_title": template.title,
                    "due_at": a.due_at.isoformat() if a.due_at else None,
                },
                organization_id=org_id,
            )
            for a in new_assignments
        ]
        await notification_service.bulk_create_notifications(session, notif_items)

    logger.info(
        "test_assignments_upserted",
        org_id=str(org_id),
        template_id=str(template_id),
        created=created,
        updated=updated,
    )
    return assignments, created, updated


async def list_template_assignments(
    session: AsyncSession,
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[TestAssignment]:
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)
    await _get_template(session, org_id, template_id)

    result = await session.execute(
        select(TestAssignment)
        .where(TestAssignment.template_id == template_id)
        .order_by(TestAssignment.created_at.desc())
    )
    return list(result.scalars().all())


async def list_org_assignments(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    template_id: uuid.UUID | None = None,
    member_id: uuid.UUID | None = None,
    status: str | None = None,
    include_deleted: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[TestAssignment], int]:
    """`include_deleted=False` (по умолчанию) скрывает назначения удалённых
    (soft-delete) шаблонов — и в выдаче, и в `total` (см. backend.md
    test_assignment_unassign). `True` — показывает все, чтобы админ мог найти
    и разобрать назначения удалённого теста."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)

    conditions = [TestTemplate.organization_id == org_id]
    if not include_deleted:
        conditions.append(TestTemplate.is_deleted.is_(False))
    if template_id is not None:
        conditions.append(TestAssignment.template_id == template_id)
    if member_id is not None:
        conditions.append(TestAssignment.member_id == member_id)
    if status is not None:
        conditions.append(TestAssignment.status == _parse_assignment_status(status))

    total = (
        await session.execute(
            select(func.count())
            .select_from(TestAssignment)
            .join(TestTemplate, TestAssignment.template_id == TestTemplate.id)
            .where(*conditions)
        )
    ).scalar_one()

    result = await session.execute(
        select(TestAssignment)
        .join(TestTemplate, TestAssignment.template_id == TestTemplate.id)
        .where(*conditions)
        .order_by(TestAssignment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total


async def get_latest_submitted_attempt_ids(
    session: AsyncSession,
    assignment_ids: list[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """`assignment_id` → id последней сданной (`submitted`) попытки. Для
    `TestAssignmentOut.last_attempt_id` — по нему админка открывает
    `GET .../test-attempts/{id}` из реестра назначений (то же `DISTINCT ON`,
    что и `services/overtime.get_latest_overtime_for_shifts`)."""
    if not assignment_ids:
        return {}
    result = await session.execute(
        select(TestAttempt.assignment_id, TestAttempt.id)
        .distinct(TestAttempt.assignment_id)
        .where(
            TestAttempt.assignment_id.in_(assignment_ids),
            TestAttempt.status == TestAttemptStatus.submitted,
        )
        .order_by(TestAttempt.assignment_id, TestAttempt.attempt_number.desc())
    )
    return dict(result.tuples().all())


async def delete_assignment(
    session: AsyncSession,
    org_id: uuid.UUID,
    assignment_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> None:
    """Безвозвратное снятие назначения — при любом `status`/`attempts_used`
    (см. backend.md test_assignment_unassign; было заблокировано при
    `attempts_used > 0`, ограничение снято владельцем). Попытки и их
    снимки вопросов/вариантов уходят каскадом (FK `ON DELETE CASCADE`,
    миграция `9632b96364fd`) вместе со строкой `test_assignments`. В той же
    транзакции удаляется уведомление `test_assigned` этого назначения —
    иначе у сотрудника в ленте остаётся тап в 404. Новых уведомлений не
    создаём, история не сохраняется."""
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)
    await entitlements.require_active_subscription(session, org, requester_id)
    assignment = await _get_org_assignment(session, org_id, assignment_id)

    await session.execute(
        delete(Notification).where(
            Notification.type == NotificationType.test_assigned.value,
            Notification.payload["assignment_id"].astext == str(assignment_id),
        )
    )
    await session.delete(assignment)
    await session.flush()
    logger.info("test_assignment_deleted", org_id=str(org_id), assignment_id=str(assignment_id))


async def get_attempt_review(
    session: AsyncSession,
    org_id: uuid.UUID,
    attempt_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> TestAttempt:
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(session, org, requester_id, message=_ADMIN_MESSAGE)

    result = await session.execute(
        select(TestAttempt)
        .join(TestAssignment, TestAttempt.assignment_id == TestAssignment.id)
        .join(TestTemplate, TestAssignment.template_id == TestTemplate.id)
        .where(TestAttempt.id == attempt_id, TestTemplate.organization_id == org_id)
        .options(
            selectinload(TestAttempt.questions).selectinload(TestAttemptQuestion.options),
            selectinload(TestAttempt.assignment).selectinload(TestAssignment.template),
        )
    )
    attempt = result.scalar_one_or_none()
    if attempt is None:
        raise TestError("TEST_ATTEMPT_NOT_FOUND", "Попытка не найдена", 404)
    return attempt


# --- Прохождение (сотрудник) ------------------------------------------------------
async def _get_my_assignment(
    session: AsyncSession,
    user_id: uuid.UUID,
    assignment_id: uuid.UUID,
    *,
    with_template: bool = False,
) -> TestAssignment:
    query = (
        select(TestAssignment)
        .join(OrganizationMember, TestAssignment.member_id == OrganizationMember.id)
        .where(TestAssignment.id == assignment_id, OrganizationMember.user_id == user_id)
    )
    if with_template:
        query = query.options(selectinload(TestAssignment.template))
    result = await session.execute(query)
    assignment = result.scalar_one_or_none()
    if assignment is None:
        raise TestError("TEST_ASSIGNMENT_NOT_FOUND", "Назначение не найдено", 404)
    return assignment


async def list_my_assignments(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    organization_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[TestAssignment], int]:
    """Мои назначения (по всем моим организациям либо одной). Пагинировано:
    возвращает (страница, total). Сортировка стабильна — `created_at DESC, id DESC`
    (вторичный ключ гарантирует детерминированный порядок при равных `created_at`,
    чтобы страницы не перемешивались). Назначения удалённых (soft-delete) шаблонов
    не отдаются — ни в `items`, ни в `total` (см. backend.md test_assignment_unassign):
    для сотрудника такого назначения не существует."""
    conditions = [OrganizationMember.user_id == user_id, TestTemplate.is_deleted.is_(False)]
    if organization_id is not None:
        conditions.append(TestTemplate.organization_id == organization_id)
    if status is not None:
        conditions.append(TestAssignment.status == _parse_assignment_status(status))

    total = (
        await session.execute(
            select(func.count())
            .select_from(TestAssignment)
            .join(OrganizationMember, TestAssignment.member_id == OrganizationMember.id)
            .join(TestTemplate, TestAssignment.template_id == TestTemplate.id)
            .where(*conditions)
        )
    ).scalar_one()

    result = await session.execute(
        select(TestAssignment)
        .join(OrganizationMember, TestAssignment.member_id == OrganizationMember.id)
        .join(TestTemplate, TestAssignment.template_id == TestTemplate.id)
        .where(*conditions)
        .options(selectinload(TestAssignment.template))
        .order_by(TestAssignment.created_at.desc(), TestAssignment.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total


async def get_my_assignment_detail(
    session: AsyncSession,
    user_id: uuid.UUID,
    assignment_id: uuid.UUID,
) -> tuple[TestAssignment, list[TestAttempt]]:
    """Назначение удалённого (soft-delete) шаблона отдаёт `404
    TEST_ASSIGNMENT_NOT_FOUND` — для сотрудника такого назначения не
    существует (см. backend.md test_assignment_unassign)."""
    assignment = await _get_my_assignment(session, user_id, assignment_id, with_template=True)
    if assignment.template.is_deleted:
        raise TestError("TEST_ASSIGNMENT_NOT_FOUND", "Назначение не найдено", 404)
    attempts_result = await session.execute(
        select(TestAttempt)
        .where(TestAttempt.assignment_id == assignment.id)
        .order_by(TestAttempt.attempt_number)
    )
    return assignment, list(attempts_result.scalars().all())


def _build_attempt_questions(template_questions: list[TestQuestion]) -> list[TestAttemptQuestion]:
    built: list[TestAttemptQuestion] = []
    for q in template_questions:
        option_models = [
            TestAttemptOption(
                template_option_id=o.id,
                text=o.text,
                is_correct=o.is_correct,
                position=o.position,
            )
            for o in q.options
        ]
        built.append(
            TestAttemptQuestion(
                template_question_id=q.id,
                text=q.text,
                type=q.type,
                points=q.points,
                position=q.position,
                options=option_models,
            )
        )
    return built


async def start_attempt(
    session: AsyncSession,
    user_id: uuid.UUID,
    assignment_id: uuid.UUID,
) -> TestAttempt:
    assignment = await _get_my_assignment(session, user_id, assignment_id, with_template=True)
    template = assignment.template
    if template.is_deleted:
        raise TestError(
            "TEST_TEMPLATE_DELETED", "Тест удалён — восстановите его, чтобы редактировать", 400
        )
    if assignment.passed:
        raise TestError("TEST_ALREADY_PASSED", "Тест уже сдан успешно", 409)
    if assignment.attempts_used >= template.max_attempts:
        raise TestError("TEST_ATTEMPTS_EXHAUSTED", "Исчерпаны все попытки", 409)

    existing_result = await session.execute(
        select(TestAttempt)
        .where(
            TestAttempt.assignment_id == assignment.id,
            TestAttempt.status == TestAttemptStatus.in_progress,
        )
        .options(selectinload(TestAttempt.questions).selectinload(TestAttemptQuestion.options))
    )
    existing = existing_result.scalars().first()
    if existing is not None:
        # Не плодим параллельные попытки — отдаём уже открытую (см. backend.md).
        # `assignment` уже несёт eager-loaded `.template` — присваиваем явно,
        # чтобы вызывающий код (сериализация ответа) не словил lazy-load под
        # async-сессией (MissingGreenlet) на `existing.assignment.template`.
        existing.assignment = assignment
        return existing

    # Блокируем строку назначения на время подсчёта следующего attempt_number —
    # сериализует конкурентные старты по одному assignment без catch/retry на
    # IntegrityError (UNIQUE(assignment_id, attempt_number) остаётся defense-in-depth).
    await session.execute(
        select(TestAssignment.id).where(TestAssignment.id == assignment.id).with_for_update()
    )

    template_full = await _get_template(
        session, template.organization_id, template.id, with_questions=True
    )
    max_score = sum(q.points for q in template_full.questions)

    max_number_result = await session.execute(
        select(func.coalesce(func.max(TestAttempt.attempt_number), 0)).where(
            TestAttempt.assignment_id == assignment.id
        )
    )
    next_number = max_number_result.scalar_one() + 1

    attempt = TestAttempt(
        # relationship, не assignment_id — чтобы attempt.assignment.template было
        # доступно синхронно (без lazy-load под async-сессией) в ответе роутера.
        assignment=assignment,
        attempt_number=next_number,
        max_score=max_score,
        pass_threshold_percent=template_full.pass_threshold_percent,
        questions=_build_attempt_questions(template_full.questions),
    )
    session.add(attempt)
    if assignment.status == TestAssignmentStatus.assigned:
        assignment.status = TestAssignmentStatus.in_progress

    await session.flush()
    logger.info(
        "test_attempt_started",
        assignment_id=str(assignment.id),
        attempt_id=str(attempt.id),
        attempt_number=next_number,
    )
    return attempt


async def _get_my_attempt_full(
    session: AsyncSession,
    user_id: uuid.UUID,
    attempt_id: uuid.UUID,
) -> TestAttempt:
    result = await session.execute(
        select(TestAttempt)
        .join(TestAssignment, TestAttempt.assignment_id == TestAssignment.id)
        .join(OrganizationMember, TestAssignment.member_id == OrganizationMember.id)
        .where(TestAttempt.id == attempt_id, OrganizationMember.user_id == user_id)
        .options(
            selectinload(TestAttempt.questions).selectinload(TestAttemptQuestion.options),
            selectinload(TestAttempt.assignment).selectinload(TestAssignment.template),
        )
    )
    attempt = result.scalar_one_or_none()
    if attempt is None:
        raise TestError("TEST_ATTEMPT_NOT_FOUND", "Попытка не найдена", 404)
    return attempt


async def get_my_attempt(
    session: AsyncSession,
    user_id: uuid.UUID,
    attempt_id: uuid.UUID,
) -> TestAttempt:
    return await _get_my_attempt_full(session, user_id, attempt_id)


def compute_awarded(question: TestAttemptQuestion) -> int:
    """All-or-nothing: полные баллы, если множество выбранных == множеству верных."""
    selected = {o.id for o in question.options if o.is_selected}
    correct = {o.id for o in question.options if o.is_correct}
    return question.points if selected == correct else 0


async def submit_attempt(
    session: AsyncSession,
    user_id: uuid.UUID,
    attempt_id: uuid.UUID,
    answers: list[tuple[uuid.UUID, list[uuid.UUID]]],
) -> TestAttempt:
    """`answers` — список (attempt_question_id, selected_option_ids)."""
    attempt = await _get_my_attempt_full(session, user_id, attempt_id)
    if attempt.status != TestAttemptStatus.in_progress:
        raise TestError("TEST_ATTEMPT_ALREADY_SUBMITTED", "Попытка уже сдана", 409)

    questions_by_id = {q.id: q for q in attempt.questions}
    answers_by_question: dict[uuid.UUID, set[uuid.UUID]] = {}
    for question_id, raw_selected_ids in answers:
        if question_id not in questions_by_id:
            raise TestError(
                "VALIDATION_ERROR", "attempt_question_id не принадлежит этой попытке", 422
            )
        answers_by_question[question_id] = set(raw_selected_ids)

    score = 0
    for question in attempt.questions:
        selected_id_set = answers_by_question.get(question.id, set())
        valid_option_ids = {o.id for o in question.options}
        if not selected_id_set.issubset(valid_option_ids):
            raise TestError(
                "VALIDATION_ERROR", "selected_option_ids не принадлежат вопросу снимка", 422
            )
        for option in question.options:
            option.is_selected = option.id in selected_id_set
        score += compute_awarded(question)

    percent = round(score / attempt.max_score * 100)
    passed = percent >= attempt.pass_threshold_percent
    now = datetime.now(UTC)

    attempt.score = score
    attempt.percent = percent
    attempt.passed = passed
    attempt.status = TestAttemptStatus.submitted
    attempt.submitted_at = now

    assignment = attempt.assignment
    assignment.attempts_used += 1
    assignment.best_percent = (
        percent if assignment.best_percent is None else max(assignment.best_percent, percent)
    )
    assignment.passed = assignment.passed or passed
    assignment.last_attempt_at = now
    if assignment.passed:
        assignment.status = TestAssignmentStatus.passed
    elif assignment.attempts_used >= assignment.template.max_attempts:
        assignment.status = TestAssignmentStatus.failed
    else:
        assignment.status = TestAssignmentStatus.in_progress

    await session.flush()
    logger.info(
        "test_attempt_submitted",
        attempt_id=str(attempt.id),
        assignment_id=str(assignment.id),
        percent=percent,
        passed=passed,
    )
    return attempt
