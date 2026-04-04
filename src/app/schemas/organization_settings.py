from pydantic import BaseModel, Field


class OrganizationSettingsResponse(BaseModel):
    organization_id: str
    geo_check_enabled: bool
    auto_finish_hours: int
    max_pause_minutes: int | None
    max_pauses_per_shift: int | None

    model_config = {"from_attributes": True}


class OrganizationSettingsUpdate(BaseModel):
    geo_check_enabled: bool | None = None
    auto_finish_hours: int | None = Field(default=None, ge=1, le=48)
    max_pause_minutes: int | None = Field(default=None, ge=1, le=480)
    max_pauses_per_shift: int | None = Field(default=None, ge=1, le=50)
