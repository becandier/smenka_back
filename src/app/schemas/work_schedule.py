import re
from datetime import datetime

from pydantic import BaseModel, Field

_HHMM_PATTERN = r"^([01]\d|2[0-3]):([0-5]\d)$"
_HHMM_RE = re.compile(_HHMM_PATTERN)


class WorkScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="Название графика")
    start_time: str = Field(
        pattern=_HHMM_PATTERN, description="Локальное время начала, формат HH:MM"
    )
    end_time: str = Field(pattern=_HHMM_PATTERN, description="Локальное время конца, формат HH:MM")


class WorkScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    start_time: str | None = Field(default=None, pattern=_HHMM_PATTERN)
    end_time: str | None = Field(default=None, pattern=_HHMM_PATTERN)
    is_archived: bool | None = Field(default=None, description="Архивный график не выдаётся")


class WorkScheduleResponse(BaseModel):
    id: str
    name: str
    start_time: str = Field(description="HH:MM, локальное время организации")
    end_time: str = Field(description="HH:MM, локальное время организации")
    duration_minutes: int = Field(description="Длительность графика в минутах")
    crosses_midnight: bool = Field(description="Ночной график, переходящий через полночь")
    is_archived: bool
    role_ids: list[str] = Field(default_factory=list, description="Роли, которым назначен")
    work_location_ids: list[str] = Field(
        default_factory=list, description="Точки, к которым привязан"
    )
    created_at: datetime


class WorkScheduleListResponse(BaseModel):
    items: list[WorkScheduleResponse]
    total: int


class ScheduleRoleAssignmentRequest(BaseModel):
    role_ids: list[str] = Field(
        description="UUIDы ролей. Передавайте полный список — PUT-семантика (замена)",
    )


class ScheduleLocationAssignmentRequest(BaseModel):
    work_location_ids: list[str] = Field(
        description=(
            "UUIDы рабочих точек. Передавайте полный список — PUT-семантика "
            "(замена). Пустой список снимает все привязки (график снова "
            "действует на всех точках)."
        ),
    )


class ScheduleMemberInfo(BaseModel):
    user_id: str
    user_name: str
    user_email: str


class ScheduleAssignmentsResponse(BaseModel):
    role_ids: list[str] = Field(description="Роли, которым назначен график")
    work_location_ids: list[str] = Field(description="Точки, к которым привязан график")
    personal_add: list[ScheduleMemberInfo] = Field(description="Сотрудники с личным add")
    personal_remove: list[ScheduleMemberInfo] = Field(description="Сотрудники с личным remove")


class ScheduleOverrideItem(BaseModel):
    schedule_id: str = Field(description="UUID графика")
    override_type: str = Field(description="add — добавить поверх назначения, remove — исключить")


class ScheduleMemberOverrideRequest(BaseModel):
    overrides: list[ScheduleOverrideItem] = Field(
        description="Полный список личных переопределений сотрудника (PUT-семантика)",
    )


class EffectiveScheduleResponse(BaseModel):
    """Строка `GET .../members/{user_id}/schedules` (проверка настройки в админке)."""

    id: str
    name: str
    start_time: str
    end_time: str
    duration_minutes: int
    crosses_midnight: bool
    is_archived: bool
    source: str = Field(description="global | location | role | personal_add")


class EffectiveSchedulesResponse(BaseModel):
    items: list[EffectiveScheduleResponse]


class MyScheduleItemResponse(BaseModel):
    """Строка `GET .../my-schedules` (мобилка: экран старта смены)."""

    id: str
    name: str
    start_time: str
    end_time: str
    duration_minutes: int
    crosses_midnight: bool
    next_start_at: datetime = Field(
        description="Плановое начало, если начать смену прямо сейчас (расчёт по R2 от now)"
    )
    next_end_at: datetime = Field(description="Плановый конец того же окна")
    is_current: bool = Field(description="now внутри планового окна")
    starts_in_minutes: int = Field(
        description="Минуты до планового начала; отрицательное — график уже идёт"
    )


class MySchedulesResponse(BaseModel):
    items: list[MyScheduleItemResponse]
    total: int
    require_schedule: bool = Field(
        description="Дублирует настройку организации — обязателен ли выбор графика"
    )


class ShiftScheduleChangeRequest(BaseModel):
    work_schedule_id: str | None = Field(
        description="UUID нового графика или null — снять график со смены (R7)"
    )
