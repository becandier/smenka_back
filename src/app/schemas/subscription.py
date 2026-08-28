from datetime import datetime

from pydantic import BaseModel, Field


class PlanLimits(BaseModel):
    max_employees: int | None = None
    max_locations: int | None = None


class PlanFeatures(BaseModel):
    fines: bool
    test_import: bool


class PlanResponse(BaseModel):
    code: str
    name: str
    price_minor: int
    currency: str
    limits: PlanLimits
    features: PlanFeatures
    sort_order: int


class PlanListResponse(BaseModel):
    items: list[PlanResponse]


class SubscriptionResponse(BaseModel):
    """Состояние подписки организации — эффективные значения (в `trialing`
    `limits`/`features` берутся от `premium`)."""

    plan_code: str
    plan_name: str
    status: str = Field(
        description="Эффективный статус: trialing/active/past_due/suspended/canceled"
    )
    trial_ends_at: datetime | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    grace_ends_at: datetime | None = None
    days_left: int | None = None
    is_read_only: bool
    limits: PlanLimits
    usage: dict[str, int]
    features: PlanFeatures
    price_minor: int
    currency: str


class AdminSubscriptionUsage(BaseModel):
    employees: int
    locations: int


class AdminSubscriptionRow(BaseModel):
    organization_id: str
    organization_name: str
    owner_email: str
    owner_login: str | None = None
    plan_code: str
    plan_name: str
    status: str
    trial_ends_at: datetime | None = None
    current_period_end: datetime | None = None
    grace_ends_at: datetime | None = None
    days_left: int | None = None
    limits: PlanLimits = Field(
        description="Эффективные лимиты (как в п.2 ТЗ): в trialing — от premium, "
        "независимо от plan_code"
    )
    usage: AdminSubscriptionUsage
    note: str | None = None
    updated_at: datetime


class AdminSubscriptionListResponse(BaseModel):
    items: list[AdminSubscriptionRow]
    total: int
    limit: int
    offset: int


class SubscriptionPatchRequest(BaseModel):
    plan_code: str | None = None
    status: str | None = None
    trial_ends_at: datetime | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    note: str | None = None


class SubscriptionExtendRequest(BaseModel):
    months: int = Field(ge=1, le=24)
    plan_code: str | None = None
    amount_minor: int | None = Field(default=None, ge=0)
    note: str | None = None


class SubscriptionEventActor(BaseModel):
    id: str
    email: str | None = None
    name: str


class SubscriptionEventResponse(BaseModel):
    id: str
    type: str
    from_plan_code: str | None = None
    to_plan_code: str | None = None
    from_status: str | None = None
    to_status: str | None = None
    period_end_before: datetime | None = None
    period_end_after: datetime | None = None
    months: int | None = None
    amount_minor: int | None = None
    note: str | None = None
    actor: SubscriptionEventActor | None = None
    created_at: datetime


class SubscriptionEventListResponse(BaseModel):
    items: list[SubscriptionEventResponse]
    total: int
    limit: int
    offset: int


class SubscriptionSummaryByStatus(BaseModel):
    trialing: int
    active: int
    past_due: int
    suspended: int
    canceled: int


class SubscriptionSummaryResponse(BaseModel):
    by_status: SubscriptionSummaryByStatus
    by_plan: dict[str, int]
    mrr_minor: int
    expiring_in_7_days: int
