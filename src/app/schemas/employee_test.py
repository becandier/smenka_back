import uuid
from datetime import datetime

from pydantic import BaseModel, Field

# --- Импорт/создание шаблона --------------------------------------------------
# Поля намеренно без Pydantic-констрейнтов (ge/le/min_length) там, где ТЗ требует
# единый код TEST_TEMPLATE_INVALID для всех нарушений инвариантов шаблона — все
# проверки выполняет `services/employee_test.validate_template_payload`, чтобы не
# отдавать часть ошибок как общий VALIDATION_ERROR из FastAPI/Pydantic.


class TestQuestionOptionIn(BaseModel):
    text: str = Field(description="Текст варианта")
    is_correct: bool = Field(default=False)
    position: int | None = Field(default=None, description="Если не задан — порядок из массива")


class TestQuestionIn(BaseModel):
    text: str = Field(description="Формулировка вопроса")
    type: str = Field(description="single_choice | multiple_choice")
    points: int = Field(default=1, description="Баллы за полностью верный ответ, >=1")
    position: int | None = Field(default=None, description="Если не задан — порядок из массива")
    options: list[TestQuestionOptionIn] = Field(default_factory=list)


class TestTemplateCreate(BaseModel):
    title: str = Field(description="Название теста")
    description: str | None = Field(default=None, description="Описание/инструкция")
    pass_threshold_percent: int = Field(default=70, description="Порог зачёта, 0..100")
    max_attempts: int = Field(default=1, description="Сколько раз можно пройти, >=1")
    reveal_answers: bool = Field(default=True, description="Показывать верные ответы после сдачи")
    shuffle_questions: bool = Field(default=False, description="Подсказка клиенту перемешивать")
    questions: list[TestQuestionIn] = Field(default_factory=list)


class TestTemplateUpdate(BaseModel):
    """Частичное обновление меты; если `questions` передано — полная замена."""

    title: str | None = Field(default=None)
    description: str | None = Field(default=None)
    pass_threshold_percent: int | None = Field(default=None)
    max_attempts: int | None = Field(default=None)
    reveal_answers: bool | None = Field(default=None)
    shuffle_questions: bool | None = Field(default=None)
    questions: list[TestQuestionIn] | None = Field(default=None)


class TestValidateResponse(BaseModel):
    valid: bool = True
    question_count: int
    total_points: int


# --- Ответы: шаблон -----------------------------------------------------------
class TestQuestionOptionOut(BaseModel):
    id: str
    text: str
    is_correct: bool
    position: int


class TestQuestionOut(BaseModel):
    id: str
    text: str
    type: str
    points: int
    position: int
    options: list[TestQuestionOptionOut]


class TestTemplateDetail(BaseModel):
    id: str
    title: str
    description: str | None
    pass_threshold_percent: int
    max_attempts: int
    reveal_answers: bool
    shuffle_questions: bool
    is_deleted: bool = Field(description="Тест удалён (мягкое удаление)")
    deleted_at: datetime | None = Field(default=None, description="Момент удаления или null")
    question_count: int
    total_points: int
    created_at: datetime
    updated_at: datetime
    questions: list[TestQuestionOut]


class TestTemplateSummary(BaseModel):
    id: str
    title: str
    question_count: int
    total_points: int
    max_attempts: int
    pass_threshold_percent: int
    is_deleted: bool = Field(description="Тест удалён (мягкое удаление)")
    deleted_at: datetime | None = Field(default=None, description="Момент удаления или null")
    assignments_count: int
    created_at: datetime


class TestTemplateListResponse(BaseModel):
    items: list[TestTemplateSummary]
    total: int
    limit: int
    offset: int


class TestTemplateDeletedResponse(BaseModel):
    deleted: bool = Field(description="Шаблон теста удалён (мягкое удаление)")


# --- Назначения -----------------------------------------------------------------
class AssignmentCreateRequest(BaseModel):
    member_ids: list[uuid.UUID] = Field(description="organization_members.id сотрудников")
    due_at: datetime | None = Field(default=None, description="Дедлайн, опционально")


class MemberSummary(BaseModel):
    id: str = Field(description="organization_members.id")
    user_id: str
    user_name: str = Field(description="Настоящее имя (User.name)")
    display_name: str | None = Field(
        default=None,
        description="Имя сотрудника в этой организации; null — не задано (member_display_name)",
    )


class TemplateSummaryRef(BaseModel):
    id: str
    title: str


class TestAssignmentOut(BaseModel):
    id: str
    member: MemberSummary
    status: str = Field(description="assigned | in_progress | passed | failed")
    attempts_used: int
    max_attempts: int
    best_percent: int | None
    passed: bool
    due_at: datetime | None
    last_attempt_at: datetime | None
    last_attempt_id: str | None = Field(
        default=None,
        description=(
            "id последней сданной (submitted) попытки; null, если сдач ещё не было. "
            "По нему админка открывает GET .../test-attempts/{id} из реестра"
        ),
    )
    created_at: datetime
    template: TemplateSummaryRef | None = Field(
        default=None,
        description="Заполняется только в реестре GET .../test-assignments",
    )


class AssignmentBulkResponse(BaseModel):
    items: list[TestAssignmentOut]
    created: int = Field(description="Сколько новых назначений создано")
    updated: int = Field(description="Сколько существующих назначений обновлено (due_at)")


class TemplateAssignmentsResponse(BaseModel):
    items: list[TestAssignmentOut]


class TestAssignmentListResponse(BaseModel):
    items: list[TestAssignmentOut]
    total: int
    limit: int
    offset: int


class AssignmentDeletedResponse(BaseModel):
    deleted: bool = Field(description="Назначение снято")


# --- Обзор попытки (админ) -------------------------------------------------------
class AttemptReviewOption(BaseModel):
    id: str
    text: str
    is_correct: bool
    is_selected: bool
    position: int


class AttemptReviewQuestion(BaseModel):
    id: str
    text: str
    type: str
    points: int
    position: int
    awarded: int
    options: list[AttemptReviewOption]


class TestAttemptReview(BaseModel):
    id: str
    assignment_id: str
    member: MemberSummary
    template: TemplateSummaryRef
    attempt_number: int
    status: str
    score: int
    max_score: int
    percent: int
    passed: bool
    pass_threshold_percent: int
    started_at: datetime
    submitted_at: datetime | None
    questions: list[AttemptReviewQuestion]


# --- Сотрудник: мои назначения ---------------------------------------------------
class MyTestTemplateSummary(BaseModel):
    id: str
    title: str
    description: str | None
    question_count: int
    max_attempts: int
    pass_threshold_percent: int
    shuffle_questions: bool


class MyOrgSummary(BaseModel):
    id: str
    name: str


class MyTestAssignmentOut(BaseModel):
    id: str
    template: MyTestTemplateSummary
    status: str
    attempts_used: int
    best_percent: int | None
    passed: bool
    due_at: datetime | None
    organization: MyOrgSummary
    organization_timezone: str | None = Field(
        default=None,
        description=(
            "Текущая IANA-таймзона организации назначения; список смешивает "
            "несколько организаций сотрудника, поэтому зона указывается на каждом элементе"
        ),
    )


class MyTestAssignmentListResponse(BaseModel):
    items: list[MyTestAssignmentOut]
    total: int
    limit: int
    offset: int


class MyAttemptSummary(BaseModel):
    number: int
    percent: int
    passed: bool
    submitted_at: datetime | None


class MyTestAssignmentDetail(MyTestAssignmentOut):
    attempts: list[MyAttemptSummary]


# --- Прохождение: старт/просмотр попытки -----------------------------------------
class FillOption(BaseModel):
    id: str = Field(description="attempt_option_id")
    text: str
    position: int


class FillQuestion(BaseModel):
    id: str = Field(description="attempt_question_id")
    text: str
    type: str
    points: int
    position: int
    options: list[FillOption]


class TestAttemptForFill(BaseModel):
    id: str
    assignment_id: str
    max_attempts: int
    attempts_used: int
    shuffle_questions: bool
    started_at: datetime
    questions: list[FillQuestion]


class MyAttemptOptionResult(BaseModel):
    id: str
    text: str
    position: int
    is_selected: bool
    is_correct: bool | None = Field(default=None, description="null, если ответы скрыты")


class MyAttemptQuestionResult(BaseModel):
    id: str
    text: str
    type: str
    points: int
    position: int
    options: list[MyAttemptOptionResult]
    awarded: int | None = Field(default=None, description="null для in_progress/скрытых ответов")


class MyAttemptDetail(BaseModel):
    id: str
    assignment_id: str
    attempt_number: int
    status: str = Field(description="in_progress | submitted")
    score: int | None = None
    max_score: int
    percent: int | None = None
    passed: bool | None = None
    pass_threshold_percent: int
    started_at: datetime
    submitted_at: datetime | None
    questions: list[MyAttemptQuestionResult]
    organization_timezone: str | None = Field(
        default=None,
        description=(
            "Текущая IANA-таймзона организации назначения этой попытки; "
            "эндпоинт не scoped по {org_id} — зона нужна клиенту явно"
        ),
    )


# --- Сдача попытки ----------------------------------------------------------------
class AnswerIn(BaseModel):
    attempt_question_id: uuid.UUID
    selected_option_ids: list[uuid.UUID] = Field(default_factory=list)


class SubmitRequest(BaseModel):
    answers: list[AnswerIn] = Field(default_factory=list)


class SubmitResultQuestion(BaseModel):
    id: str
    text: str
    type: str
    points: int
    awarded: int
    options: list[MyAttemptOptionResult]


class SubmitResponse(BaseModel):
    score: int
    max_score: int
    percent: int
    passed: bool
    pass_threshold_percent: int
    attempts_used: int
    attempts_left: int
    reveal_answers: bool
    questions: list[SubmitResultQuestion] | None = None
