from datetime import datetime

from pydantic import BaseModel, Field


class PauseResponse(BaseModel):
    id: str
    shift_id: str
    started_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class ShiftResponse(BaseModel):
    id: str
    user_id: str
    organization_id: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    pauses: list[PauseResponse]
    worked_seconds: int

    model_config = {"from_attributes": True}


class ShiftListResponse(BaseModel):
    items: list[ShiftResponse]
    total: int
    limit: int
    offset: int


class ShiftStatsResponse(BaseModel):
    period: str
    total_worked_seconds: int
    shift_count: int
    average_shift_seconds: int


class ShiftStartRequest(BaseModel):
    organization_id: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
