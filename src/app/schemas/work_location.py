from datetime import datetime

from pydantic import BaseModel, Field


class WorkLocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    radius_meters: int = Field(default=100, ge=10, le=10000)


class WorkLocationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_meters: int | None = Field(default=None, ge=10, le=10000)


class WorkLocationResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    latitude: float
    longitude: float
    radius_meters: int
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkLocationListResponse(BaseModel):
    items: list[WorkLocationResponse]
