import uuid
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.employee_test import (
    TestAssignment,
    TestAttempt,
    TestAttemptOption,
    TestAttemptQuestion,
    TestQuestion,
    TestQuestionOption,
    TestTemplate,
)
from src.app.models.organization import OrganizationMember
from src.app.models.user import User
from src.app.schemas.base import ApiResponse
from src.app.schemas.employee_test import (
    AssignmentBulkResponse,
    AssignmentCreateRequest,
    AssignmentDeletedResponse,
    AttemptReviewOption,
    AttemptReviewQuestion,
    MemberSummary,
    TemplateAssignmentsResponse,
    TemplateSummaryRef,
    TestAssignmentListResponse,
    TestAssignmentOut,
    TestAttemptReview,
    TestQuestionOptionOut,
    TestQuestionOut,
    TestTemplateCreate,
    TestTemplateDeletedResponse,
    TestTemplateDetail,
    TestTemplateListResponse,
    TestTemplateSummary,
    TestTemplateUpdate,
    TestValidateResponse,
)
from src.app.services import employee_test as test_service

router = APIRouter(prefix="/organizations/{org_id}", tags=["test-templates"])


# --- Сборка ответов -------------------------------------------------------------
def _questions_from_body(
    questions: list[Any],
) -> list[test_service.QuestionInput]:
    return [
        test_service.QuestionInput(
            text=q.text,
            type=q.type,
            points=q.points,
            position=q.position,
            options=[
                test_service.QuestionOptionInput(
                    text=o.text, is_correct=o.is_correct, position=o.position
                )
                for o in q.options
            ],
        )
        for q in questions
    ]


def _question_to_out(question: TestQuestion) -> TestQuestionOut:
    options: list[TestQuestionOption] = sorted(question.options, key=lambda o: o.position)
    return TestQuestionOut(
        id=str(question.id),
        text=question.text,
        type=question.type.value,
        points=question.points,
        position=question.position,
        options=[
            TestQuestionOptionOut(
                id=str(o.id), text=o.text, is_correct=o.is_correct, position=o.position
            )
            for o in options
        ],
    )


def _template_to_detail(template: TestTemplate) -> TestTemplateDetail:
    questions: list[TestQuestion] = sorted(template.questions, key=lambda q: q.position)
    return TestTemplateDetail(
        id=str(template.id),
        title=template.title,
        description=template.description,
        pass_threshold_percent=template.pass_threshold_percent,
        max_attempts=template.max_attempts,
        reveal_answers=template.reveal_answers,
        shuffle_questions=template.shuffle_questions,
        is_deleted=template.is_deleted,
        deleted_at=template.deleted_at,
        question_count=len(questions),
        total_points=sum(q.points for q in questions),
        created_at=template.created_at,
        updated_at=template.updated_at,
        questions=[_question_to_out(q) for q in questions],
    )


def _template_to_summary(
    template: TestTemplate,
    question_count: int,
    total_points: int,
    assignments_count: int,
) -> TestTemplateSummary:
    return TestTemplateSummary(
        id=str(template.id),
        title=template.title,
        question_count=question_count,
        total_points=total_points,
        max_attempts=template.max_attempts,
        pass_threshold_percent=template.pass_threshold_percent,
        is_deleted=template.is_deleted,
        deleted_at=template.deleted_at,
        assignments_count=assignments_count,
        created_at=template.created_at,
    )


async def _build_assignment_out_list(
    session: AsyncSession,
    assignments: list[TestAssignment],
    *,
    include_template: bool,
) -> list[TestAssignmentOut]:
    """Обогащает назначения именем/UUID сотрудника и метой шаблона без N+1."""
    if not assignments:
        return []
    member_ids = {a.member_id for a in assignments}
    members_result = await session.execute(
        select(
            OrganizationMember.id,
            OrganizationMember.user_id,
            OrganizationMember.display_name,
        ).where(OrganizationMember.id.in_(member_ids))
    )
    member_rows = members_result.all()
    user_by_member = {row.id: row.user_id for row in member_rows}
    display_name_by_member = {row.id: row.display_name for row in member_rows}
    user_ids = set(user_by_member.values())
    users_result = await session.execute(select(User.id, User.name).where(User.id.in_(user_ids)))
    name_by_user = dict(users_result.tuples().all())

    template_ids = {a.template_id for a in assignments}
    templates_result = await session.execute(
        select(TestTemplate.id, TestTemplate.title, TestTemplate.max_attempts).where(
            TestTemplate.id.in_(template_ids)
        )
    )
    template_rows = {row.id: (row.title, row.max_attempts) for row in templates_result.all()}

    last_attempt_ids = await test_service.get_latest_submitted_attempt_ids(
        session, [a.id for a in assignments]
    )

    items: list[TestAssignmentOut] = []
    for a in assignments:
        uid = user_by_member.get(a.member_id)
        user_name = name_by_user.get(uid, "Unknown") if uid is not None else "Unknown"
        title, max_attempts = template_rows.get(a.template_id, ("", 1))
        last_attempt_id = last_attempt_ids.get(a.id)
        items.append(
            TestAssignmentOut(
                id=str(a.id),
                member=MemberSummary(
                    id=str(a.member_id),
                    user_id=str(uid) if uid is not None else "",
                    user_name=user_name,
                    display_name=display_name_by_member.get(a.member_id),
                ),
                status=a.status.value,
                attempts_used=a.attempts_used,
                max_attempts=max_attempts,
                best_percent=a.best_percent,
                passed=a.passed,
                due_at=a.due_at,
                last_attempt_at=a.last_attempt_at,
                last_attempt_id=str(last_attempt_id) if last_attempt_id else None,
                created_at=a.created_at,
                template=(
                    TemplateSummaryRef(id=str(a.template_id), title=title)
                    if include_template
                    else None
                ),
            )
        )
    return items


def _attempt_review_option(option: TestAttemptOption) -> AttemptReviewOption:
    return AttemptReviewOption(
        id=str(option.id),
        text=option.text,
        is_correct=option.is_correct,
        is_selected=option.is_selected,
        position=option.position,
    )


def _attempt_review_question(question: TestAttemptQuestion) -> AttemptReviewQuestion:
    options = sorted(question.options, key=lambda o: o.position)
    return AttemptReviewQuestion(
        id=str(question.id),
        text=question.text,
        type=question.type.value,
        points=question.points,
        position=question.position,
        awarded=test_service.compute_awarded(question),
        options=[_attempt_review_option(o) for o in options],
    )


async def _build_attempt_review(session: AsyncSession, attempt: TestAttempt) -> TestAttemptReview:
    assignment = attempt.assignment
    template = assignment.template

    member_result = await session.execute(
        select(
            OrganizationMember.id,
            OrganizationMember.user_id,
            OrganizationMember.display_name,
        ).where(OrganizationMember.id == assignment.member_id)
    )
    member_row = member_result.one()
    user_name_result = await session.execute(
        select(User.name).where(User.id == member_row.user_id)
    )
    user_name = user_name_result.scalar_one_or_none() or "Unknown"

    questions = sorted(attempt.questions, key=lambda q: q.position)
    return TestAttemptReview(
        id=str(attempt.id),
        assignment_id=str(assignment.id),
        member=MemberSummary(
            id=str(member_row.id),
            user_id=str(member_row.user_id),
            user_name=user_name,
            display_name=member_row.display_name,
        ),
        template=TemplateSummaryRef(id=str(template.id), title=template.title),
        attempt_number=attempt.attempt_number,
        status=attempt.status.value,
        score=attempt.score,
        max_score=attempt.max_score,
        percent=attempt.percent,
        passed=attempt.passed,
        pass_threshold_percent=attempt.pass_threshold_percent,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        questions=[_attempt_review_question(q) for q in questions],
    )


# --- Шаблоны ---------------------------------------------------------------------
@router.post(
    "/test-templates",
    status_code=201,
    summary="Создать шаблон теста",
    description=(
        "Тело — вложенная структура с вопросами/вариантами; это же тело — формат "
        "импорта (см. docs/tasks/employee_tests/import-format.md). Owner/admin."
    ),
)
async def create_template(
    org_id: uuid.UUID,
    body: TestTemplateCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    template = await test_service.create_template(
        session,
        org_id,
        user.id,
        title=body.title,
        description=body.description,
        pass_threshold_percent=body.pass_threshold_percent,
        max_attempts=body.max_attempts,
        reveal_answers=body.reveal_answers,
        shuffle_questions=body.shuffle_questions,
        questions=_questions_from_body(body.questions),
    )
    await session.commit()
    return ApiResponse.success(_template_to_detail(template).model_dump(mode="json"))


@router.post(
    "/test-templates/validate",
    summary="Сухая проверка шаблона (без создания)",
    description="Та же схема, что и create. Для кнопки «Проверить JSON» в админке.",
)
async def validate_template(
    org_id: uuid.UUID,
    body: TestTemplateCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    question_count, total_points = await test_service.validate_template_only(
        session,
        org_id,
        user.id,
        pass_threshold_percent=body.pass_threshold_percent,
        max_attempts=body.max_attempts,
        questions=_questions_from_body(body.questions),
    )
    return ApiResponse.success(
        TestValidateResponse(
            valid=True, question_count=question_count, total_points=total_points
        ).model_dump()
    )


@router.get(
    "/test-templates",
    summary="Список шаблонов тестов",
    description=(
        "Owner/admin. По умолчанию удалённые скрыты; include_deleted=true — показать и их."
    ),
)
async def list_templates(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    include_deleted: bool = Query(False, description="Включить удалённые шаблоны"),
) -> ApiResponse:
    items, total = await test_service.list_templates(
        session, org_id, user.id, limit=limit, offset=offset, include_deleted=include_deleted
    )
    return ApiResponse.success(
        TestTemplateListResponse(
            items=[_template_to_summary(t, qc, tp, ac) for t, qc, tp, ac in items],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/test-templates/{template_id}",
    summary="Детали шаблона с вопросами",
    description="С is_correct — этот эндпоинт только для owner/admin.",
)
async def get_template_detail(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    template = await test_service.get_template_detail(session, org_id, template_id, user.id)
    return ApiResponse.success(_template_to_detail(template).model_dump(mode="json"))


@router.patch(
    "/test-templates/{template_id}",
    summary="Обновить шаблон",
    description=(
        "Обновляет мету и/или полностью заменяет questions (если поле передано). "
        "Не трогает существующие попытки (снимки). Архивный шаблон — 400."
    ),
)
async def update_template(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    body: TestTemplateUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    fields: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"questions"})
    # questions нарочно НЕ проходит через model_dump — сервис ждёт вложенные
    # Pydantic-объекты (атрибуты .text/.type/...), не dict-снимок.
    if "questions" in body.model_fields_set:
        fields["questions"] = body.questions
    template = await test_service.update_template(session, org_id, template_id, user.id, fields)
    await session.commit()
    return ApiResponse.success(_template_to_detail(template).model_dump(mode="json"))


@router.delete(
    "/test-templates/{template_id}",
    summary="Удалить шаблон теста",
    description=(
        "Удаляет шаблон (мягкое удаление). Назначения и попытки сотрудников сохраняются "
        "и продолжают показываться. Удалённый шаблон нельзя редактировать и нельзя "
        "назначить. Повторный вызов на уже удалённом — 404."
    ),
)
async def delete_template(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await test_service.delete_template(session, org_id, template_id, user.id)
    await session.commit()
    return ApiResponse.success(TestTemplateDeletedResponse(deleted=True).model_dump())


@router.post(
    "/test-templates/{template_id}/restore",
    summary="Восстановить удалённый шаблон теста",
    description="Возвращает шаблон в работу. На неудалённом — 409 TEST_TEMPLATE_NOT_DELETED.",
)
async def restore_template(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    template = await test_service.restore_template(session, org_id, template_id, user.id)
    await session.commit()
    return ApiResponse.success(_template_to_detail(template).model_dump(mode="json"))


# --- Назначения --------------------------------------------------------------------
@router.post(
    "/test-templates/{template_id}/assignments",
    status_code=201,
    summary="Назначить тест сотрудникам",
    description=(
        "Upsert по (template_id, member_id): существующее назначение обновляет "
        "только due_at, результаты не сбрасываются. Для новых — создаёт "
        "уведомление test_assigned в той же транзакции."
    ),
)
async def assign_template(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    body: AssignmentCreateRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    assignments, created, updated = await test_service.assign_template(
        session,
        org_id,
        template_id,
        user.id,
        member_ids=body.member_ids,
        due_at=body.due_at,
    )
    await session.commit()
    items = await _build_assignment_out_list(session, assignments, include_template=False)
    return ApiResponse.success(
        AssignmentBulkResponse(items=items, created=created, updated=updated).model_dump(
            mode="json"
        )
    )


@router.get(
    "/test-templates/{template_id}/assignments",
    summary="Назначения одного теста",
)
async def list_template_assignments(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    assignments = await test_service.list_template_assignments(
        session, org_id, template_id, user.id
    )
    items = await _build_assignment_out_list(session, assignments, include_template=False)
    return ApiResponse.success(TemplateAssignmentsResponse(items=items).model_dump(mode="json"))


@router.get(
    "/test-assignments",
    summary="Реестр результатов по всей организации",
)
async def list_org_assignments(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    template_id: uuid.UUID | None = Query(None),
    member_id: uuid.UUID | None = Query(None),
    status: str | None = Query(None, description="assigned | in_progress | passed | failed"),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    assignments, total = await test_service.list_org_assignments(
        session,
        org_id,
        user.id,
        template_id=template_id,
        member_id=member_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    items = await _build_assignment_out_list(session, assignments, include_template=True)
    return ApiResponse.success(
        TestAssignmentListResponse(
            items=items, total=total, limit=limit, offset=offset
        ).model_dump(mode="json")
    )


@router.delete(
    "/test-assignments/{assignment_id}",
    summary="Снять назначение",
    description="Только если ни одной сданной попытки — иначе 409 TEST_ASSIGNMENT_HAS_ATTEMPTS.",
)
async def delete_assignment(
    org_id: uuid.UUID,
    assignment_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await test_service.delete_assignment(session, org_id, assignment_id, user.id)
    await session.commit()
    return ApiResponse.success(AssignmentDeletedResponse(deleted=True).model_dump())


@router.get(
    "/test-attempts/{attempt_id}",
    summary="Детали попытки для админа",
    description="Вопросы-снимки, выбор сотрудника, верные ответы, баллы по вопросам.",
)
async def get_attempt_review(
    org_id: uuid.UUID,
    attempt_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    attempt = await test_service.get_attempt_review(session, org_id, attempt_id, user.id)
    review = await _build_attempt_review(session, attempt)
    return ApiResponse.success(review.model_dump(mode="json"))
