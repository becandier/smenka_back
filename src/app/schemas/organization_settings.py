from pydantic import BaseModel, Field


class OrganizationSettingsResponse(BaseModel):
    organization_id: str = Field(description="UUID организации")
    geo_check_enabled: bool = Field(description="Геопроверка при начале смены")
    require_work_location: bool = Field(
        description="Требовать привязку точки к смене (влияет на старт при выключенной гео)"
    )
    max_pause_minutes: int | None = Field(
        default=None,
        description="Максимальная длительность паузы в минутах (null — без ограничений)",
    )
    max_pauses_per_shift: int | None = Field(
        default=None,
        description="Максимальное количество пауз за смену (null — без ограничений)",
    )
    auto_finish_by_schedule: bool = Field(
        description="Завершать смену автоматически в плановое время окончания графика "
        "(work_schedules)"
    )
    require_schedule: bool = Field(
        description="Требовать выбор графика при старте смены (work_schedules)"
    )
    late_tolerance_minutes: int = Field(
        description="Допуск по опозданию в минутах (0–120); опоздание в пределах "
        "допуска не показывается"
    )
    overtime_request_days: int = Field(
        description="Срок подачи заявки на переработку в днях (1–90) после завершения смены"
    )
    early_start_minutes: int = Field(
        description="За сколько минут до планового начала графика разрешено начать смену "
        "(0–240); 0 — строго не раньше начала (schedule_window_enforcement)"
    )
    checklist_grace_minutes: int = Field(
        description="Окно дозаполнения чек-листа после закрытия смены в минутах (0–240); "
        "0 — дозаполнение запрещено (checklist_grace_period)"
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
    max_pause_minutes: int | None = Field(
        default=None, ge=1, le=480, description="Макс. длительность паузы в минутах (1–480)"
    )
    max_pauses_per_shift: int | None = Field(
        default=None, ge=1, le=50, description="Макс. количество пауз за смену (1–50)"
    )
    auto_finish_by_schedule: bool | None = Field(
        default=None,
        description="Завершать смену автоматически в плановое время окончания графика",
    )
    require_schedule: bool | None = Field(
        default=None,
        description="Требовать выбор графика при старте смены. Нельзя включить без "
        "неархивных графиков организации",
    )
    late_tolerance_minutes: int | None = Field(
        default=None, ge=0, le=120, description="Допуск по опозданию в минутах (0–120)"
    )
    overtime_request_days: int | None = Field(
        default=None, ge=1, le=90, description="Срок подачи заявки на переработку в днях (1–90)"
    )
    early_start_minutes: int | None = Field(
        default=None,
        ge=0,
        le=240,
        description="За сколько минут до планового начала графика разрешено начать смену (0–240)",
    )
    checklist_grace_minutes: int | None = Field(
        default=None,
        ge=0,
        le=240,
        description="Окно дозаполнения чек-листа после закрытия смены в минутах (0–240); "
        "0 — дозаполнение запрещено",
    )
