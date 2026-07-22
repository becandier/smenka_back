from datetime import datetime

from pydantic import BaseModel, Field


class OvertimeCreateRequest(BaseModel):
    minutes: int = Field(ge=1, le=1440, description="Переработка в минутах (1–1440)")
    comment: str = Field(min_length=1, max_length=500, description="Обязательный комментарий")


class OvertimeReviewRequest(BaseModel):
    status: str = Field(description="approved | rejected")
    review_comment: str | None = Field(
        default=None, max_length=500, description="Комментарий администратора"
    )


class OvertimeInfo(BaseModel):
    """Встраивается в `ShiftResponse.overtime` (последняя заявка смены)."""

    id: str
    minutes: int
    status: str = Field(description="pending | approved | rejected")
    comment: str
    review_comment: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


class OrgOvertimeRequestUser(BaseModel):
    id: str
    user_name: str
    display_name: str | None = Field(
        default=None, description="Имя сотрудника в организации (member_display_name)"
    )
    email: str


class OrgOvertimeRequestShift(BaseModel):
    started_at: datetime
    finished_at: datetime | None
    scheduled_start_at: datetime | None
    scheduled_end_at: datetime | None
    schedule_name: str | None
    work_location_name: str | None


class OrgOvertimeRequestResponse(BaseModel):
    id: str
    shift_id: str
    minutes: int
    comment: str
    status: str
    review_comment: str | None
    reviewed_at: datetime | None
    created_at: datetime
    user: OrgOvertimeRequestUser
    shift: OrgOvertimeRequestShift


class OrgOvertimeRequestListResponse(BaseModel):
    items: list[OrgOvertimeRequestResponse]
    total: int
    limit: int
    offset: int


class OvertimeReviewResponse(BaseModel):
    """Ответ `PATCH .../overtime-requests/{request_id}`."""

    id: str
    shift_id: str
    minutes: int
    comment: str
    status: str
    review_comment: str | None
    reviewed_at: datetime | None
    created_at: datetime
