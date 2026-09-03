import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.employee_test import (
    TestAssignment,
    TestAttempt,
    TestAttemptStatus,
    TestQuestion,
)
from src.app.models.organization import Organization
from src.app.schemas.base import ApiResponse
from src.app.schemas.employee_test import (
    FillOption,
    FillQuestion,
    MyAttemptDetail,
    MyAttemptOptionResult,
    MyAttemptQuestionResult,
    MyAttemptSummary,
    MyOrgSummary,
    MyTestAssignmentDetail,
    MyTestAssignmentListResponse,
    MyTestAssignmentOut,
    MyTestTemplateSummary,
    SubmitRequest,
    SubmitResponse,
    SubmitResultQuestion,
    TestAttemptForFill,
)
from src.app.services import employee_test as test_service

router = APIRouter(prefix="/my", tags=["my-tests"])


# --- Сборка ответов -------------------------------------------------------------
async def _build_my_assignment_list(
    session: AsyncSession,
    assignments: list[TestAssignment],
) -> list[MyTestAssignmentOut]:
    if not assignments:
        return []
    template_ids = {a.template_id for a in assignments}
    q_counts_result = await session.execute(
        select(TestQuestion.template_id, func.count(TestQuestion.id))
        .where(TestQuestion.template_id.in_(template_ids))
        .group_by(TestQuestion.template_id)
    )
    q_counts = dict(q_counts_result.tuples().all())

    org_ids = {a.template.organization_id for a in assignments}
    orgs_result = await session.execute(
        select(Organization.id, Organization.name, Organization.timezone).where(
            Organization.id.in_(org_ids)
        )
    )
    # {organization_id: (name, timezone)} — соседний список смешивает несколько
    # организаций сотрудника, поэтому оба поля денормализуются построчно тем же
    # batch-запросом (без дополнительных обращений к БД, без N+1).
    org_info = {row[0]: (row[1], row[2]) for row in orgs_result.tuples().all()}

    items: list[MyTestAssignmentOut] = []
    for a in assignments:
        template = a.template
        org_name, org_timezone = org_info.get(template.organization_id, ("", None))
        items.append(
            MyTestAssignmentOut(
                id=str(a.id),
                template=MyTestTemplateSummary(
                    id=str(template.id),
                    title=template.title,
                    description=template.description,
                    question_count=q_counts.get(template.id, 0),
                    max_attempts=template.max_attempts,
                    pass_threshold_percent=template.pass_threshold_percent,
                    shuffle_questions=template.shuffle_questions,
                ),
                status=a.status.value,
                attempts_used=a.attempts_used,
                best_percent=a.best_percent,
                passed=a.passed,
                due_at=a.due_at,
                organization=MyOrgSummary(
                    id=str(template.organization_id),
                    name=org_name,
                ),
                organization_timezone=org_timezone,
            )
        )
    return items


async def _get_organization_timezone(
    session: AsyncSession, organization_id: uuid.UUID
) -> str | None:
    """Текущая IANA-зона одной организации, одним запросом (включая soft-deleted —
    историческая попытка/назначение продолжает иметь валидный контекст)."""
    result = await session.execute(
        select(Organization.timezone).where(Organization.id == organization_id)
    )
    return result.scalar_one_or_none()


def _attempt_to_fill(attempt: TestAttempt) -> TestAttemptForFill:
    questions = sorted(attempt.questions, key=lambda q: q.position)
    template = attempt.assignment.template
    return TestAttemptForFill(
        id=str(attempt.id),
        assignment_id=str(attempt.assignment_id),
        max_attempts=template.max_attempts,
        attempts_used=attempt.assignment.attempts_used,
        shuffle_questions=template.shuffle_questions,
        started_at=attempt.started_at,
        questions=[
            FillQuestion(
                id=str(q.id),
                text=q.text,
                type=q.type.value,
                points=q.points,
                position=q.position,
                options=[
                    FillOption(id=str(o.id), text=o.text, position=o.position)
                    for o in sorted(q.options, key=lambda o: o.position)
                ],
            )
            for q in questions
        ],
    )


def _attempt_to_my_detail(
    attempt: TestAttempt, *, reveal: bool, organization_timezone: str | None
) -> MyAttemptDetail:
    """`reveal` — показывать is_correct/awarded. При in_progress всегда False
    (независимо от reveal_answers шаблона — отвечать ещё рано)."""
    submitted = attempt.status == TestAttemptStatus.submitted
    show_correctness = reveal and submitted
    questions = sorted(attempt.questions, key=lambda q: q.position)
    return MyAttemptDetail(
        id=str(attempt.id),
        assignment_id=str(attempt.assignment_id),
        attempt_number=attempt.attempt_number,
        status=attempt.status.value,
        score=attempt.score if submitted else None,
        max_score=attempt.max_score,
        percent=attempt.percent if submitted else None,
        passed=attempt.passed if submitted else None,
        pass_threshold_percent=attempt.pass_threshold_percent,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        organization_timezone=organization_timezone,
        questions=[
            MyAttemptQuestionResult(
                id=str(q.id),
                text=q.text,
                type=q.type.value,
                points=q.points,
                position=q.position,
                awarded=(test_service.compute_awarded(q) if show_correctness else None),
                options=[
                    MyAttemptOptionResult(
                        id=str(o.id),
                        text=o.text,
                        position=o.position,
                        is_selected=o.is_selected if submitted else False,
                        is_correct=o.is_correct if show_correctness else None,
                    )
                    for o in sorted(q.options, key=lambda o: o.position)
                ],
            )
            for q in questions
        ],
    )


# --- Мои назначения ---------------------------------------------------------------
@router.get(
    "/test-assignments",
    summary="Мои назначенные тесты",
    description="По всем моим организациям либо одной (organization_id).",
)
async def list_my_assignments(
    user: CurrentUserDep,
    session: SessionDep,
    organization_id: uuid.UUID | None = Query(None),
    status: str | None = Query(None, description="assigned | in_progress | passed | failed"),
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    assignments, total = await test_service.list_my_assignments(
        session,
        user.id,
        organization_id=organization_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    items = await _build_my_assignment_list(session, assignments)
    return ApiResponse.success(
        MyTestAssignmentListResponse(
            items=items, total=total, limit=limit, offset=offset
        ).model_dump(mode="json")
    )


@router.get(
    "/test-assignments/{assignment_id}",
    summary="Детали назначения + список моих попыток",
)
async def get_my_assignment_detail(
    assignment_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    assignment, attempts = await test_service.get_my_assignment_detail(
        session, user.id, assignment_id
    )
    base = (await _build_my_assignment_list(session, [assignment]))[0]
    detail = MyTestAssignmentDetail(
        **base.model_dump(),
        attempts=[
            MyAttemptSummary(
                number=a.attempt_number,
                percent=a.percent,
                passed=a.passed,
                submitted_at=a.submitted_at,
            )
            for a in attempts
        ],
    )
    return ApiResponse.success(detail.model_dump(mode="json"))


@router.post(
    "/test-assignments/{assignment_id}/attempts",
    status_code=201,
    summary="Начать новую попытку",
    description=(
        "Снимок текущего шаблона (вопросы+варианты без is_correct). Если уже есть "
        "открытая in_progress попытка — возвращает её же, не плодит дубли."
    ),
)
async def start_attempt(
    assignment_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    attempt = await test_service.start_attempt(session, user.id, assignment_id)
    await session.commit()
    return ApiResponse.success(_attempt_to_fill(attempt).model_dump(mode="json"))


@router.get(
    "/test-attempts/{attempt_id}",
    summary="Моя попытка",
    description=(
        "in_progress — без is_correct. submitted — итоги; is_correct/is_selected/awarded "
        "только если у шаблона reveal_answers=true."
    ),
)
async def get_my_attempt(
    attempt_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    attempt = await test_service.get_my_attempt(session, user.id, attempt_id)
    reveal = attempt.assignment.template.reveal_answers
    organization_timezone = await _get_organization_timezone(
        session, attempt.assignment.template.organization_id
    )
    return ApiResponse.success(
        _attempt_to_my_detail(
            attempt, reveal=reveal, organization_timezone=organization_timezone
        ).model_dump(mode="json")
    )


@router.post(
    "/test-attempts/{attempt_id}/submit",
    summary="Сдать попытку",
    description=(
        "Непереданные вопросы = без выбора (0 баллов). Грейдинг all-or-nothing, "
        "денорм test_assignments обновляется в той же транзакции."
    ),
)
async def submit_attempt(
    attempt_id: uuid.UUID,
    body: SubmitRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    answers: list[tuple[uuid.UUID, list[uuid.UUID]]] = [
        (a.attempt_question_id, a.selected_option_ids) for a in body.answers
    ]
    attempt = await test_service.submit_attempt(session, user.id, attempt_id, answers)
    await session.commit()

    template = attempt.assignment.template
    reveal = template.reveal_answers
    questions_out: list[SubmitResultQuestion] | None = None
    if reveal:
        questions_out = [
            SubmitResultQuestion(
                id=str(q.id),
                text=q.text,
                type=q.type.value,
                points=q.points,
                awarded=test_service.compute_awarded(q),
                options=[
                    MyAttemptOptionResult(
                        id=str(o.id),
                        text=o.text,
                        position=o.position,
                        is_selected=o.is_selected,
                        is_correct=o.is_correct,
                    )
                    for o in sorted(q.options, key=lambda o: o.position)
                ],
            )
            for q in sorted(attempt.questions, key=lambda q: q.position)
        ]

    attempts_left = max(0, template.max_attempts - attempt.assignment.attempts_used)
    return ApiResponse.success(
        SubmitResponse(
            score=attempt.score,
            max_score=attempt.max_score,
            percent=attempt.percent,
            passed=attempt.passed,
            pass_threshold_percent=attempt.pass_threshold_percent,
            attempts_used=attempt.assignment.attempts_used,
            attempts_left=attempts_left,
            reveal_answers=reveal,
            questions=questions_out,
        ).model_dump(mode="json")
    )
