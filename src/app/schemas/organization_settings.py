from pydantic import BaseModel, Field


class OrganizationSettingsResponse(BaseModel):
    organization_id: str = Field(description="UUID организации")
    geo_check_enabled: bool = Field(description="Геопроверка при начале смены")
    require_work_location: bool = Field(
        description="Требовать привязку точки к смене (влияет на старт при выключенной гео)"
    )
    auto_finish_hours: int | None = Field(
        description="Часы до автозавершения смены (null — отключено, по умолчанию 16)"
    )
    max_pause_minutes: int | None = Field(
        default=None,
        description="Максимальная длительность паузы в минутах (null — без ограничений)",
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
    require_work_location: bool | None = Field(
        default=None,
        description="Требовать привязку точки к смене. Нельзя включить без рабочих точек",
    )
    auto_finish_hours: int | None = Field(
        default=None, ge=1, le=48, description="Часы до автозавершения (1–48, null — отключить)"
    )
    max_pause_minutes: int | None = Field(
        default=None, ge=1, le=480, description="Макс. длительность паузы в минутах (1–480)"
    )
    max_pauses_per_shift: int | None = Field(
        default=None, ge=1, le=50, description="Макс. количество пауз за смену (1–50)"
    )
