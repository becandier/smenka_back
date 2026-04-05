from pydantic import BaseModel, Field


class OrganizationSettingsResponse(BaseModel):
    organization_id: str = Field(description="UUID организации")
    geo_check_enabled: bool = Field(description="Геопроверка при начале смены")
    auto_finish_hours: int = Field(description="Часы до автозавершения смены (по умолчанию 16)")
    max_pause_minutes: int | None = Field(
        default=None, description="Максимальная длительность паузы в минутах (null — без ограничений)"
    )
    max_pauses_per_shift: int | None = Field(
        default=None,
        description="Максимальное количество пауз за смену (null — без ограничений)",
    )

    model_config = {"from_attributes": True}


class OrganizationSettingsUpdate(BaseModel):
    geo_check_enabled: bool | None = Field(
        default=None, description="Включить/выключить геопроверку"
    )
    auto_finish_hours: int | None = Field(
        default=None, ge=1, le=48, description="Часы до автозавершения (1–48)"
    )
    max_pause_minutes: int | None = Field(
        default=None, ge=1, le=480, description="Макс. длительность паузы в минутах (1–480)"
    )
    max_pauses_per_shift: int | None = Field(
        default=None, ge=1, le=50, description="Макс. количество пауз за смену (1–50)"
    )
