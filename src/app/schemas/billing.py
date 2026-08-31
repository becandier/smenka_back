from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class BillingConfigResponse(BaseModel):
    enabled: bool
    mode: str
    provider: str


class BillingExtendOption(BaseModel):
    plan_code: str
    plan_name: str
    months: int
    base_amount_minor: int
    discount_percent: int
    amount_minor: int
    savings_minor: int
    monthly_minor: int


class BillingUpgradeOption(BaseModel):
    available: bool
    reason: str | None = None
    from_plan_code: str | None = None
    to_plan_code: str | None = None
    to_plan_name: str | None = None
    months_remaining: int | None = None
    amount_minor: int | None = None
    current_period_end: datetime | None = None


class BillingOptionsResponse(BaseModel):
    currency: str
    current_plan_code: str
    extend: list[BillingExtendOption]
    upgrade: BillingUpgradeOption


class BillingCheckoutRequest(BaseModel):
    kind: Literal["extend", "upgrade"]
    plan_code: str
    # Обязателен для kind=extend; игнорируется для kind=upgrade (считается сервером).
    months: int | None = Field(default=None, ge=1)


class BillingCheckoutResponse(BaseModel):
    payment_id: str
    confirmation_url: str | None
    amount_minor: int
    currency: str
    status: str


class BillingPaymentResponse(BaseModel):
    id: str
    kind: str
    plan_code: str
    plan_name: str
    months: int | None = None
    amount_minor: int
    currency: str
    status: str
    is_test: bool
    paid_at: datetime | None = None
    applied_at: datetime | None = None
    created_at: datetime


class BillingPaymentListResponse(BaseModel):
    items: list[BillingPaymentResponse]
    total: int
    limit: int
    offset: int


class AdminPaymentCreatedBy(BaseModel):
    id: str
    email: str | None = None
    name: str


class AdminPaymentRow(BillingPaymentResponse):
    organization_id: str
    organization_name: str
    created_by: AdminPaymentCreatedBy | None = None


class AdminPaymentTotals(BaseModel):
    succeeded_amount_minor: int
    count: int


class AdminPaymentListResponse(BaseModel):
    items: list[AdminPaymentRow]
    total: int
    limit: int
    offset: int
    totals: AdminPaymentTotals
