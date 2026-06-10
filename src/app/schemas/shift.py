from datetime import datetime

from pydantic import BaseModel, Field


class PauseResponse(BaseModel):
    id: str = Field(description="UUID паузы")
    shift_id: str = Field(description="UUID смены")
    started_at: datetime = Field(description="Начало паузы")
    finished_at: datetime | None = Field(
        default=None, description="Конец паузы (null если активна)"
    )

    model_config = {"from_attributes": True}


class ShiftResponse(BaseModel):
    id: str = Field(description="UUID смены")
    user_id: str = Field(description="UUID пользователя")
    organization_id: str | None = Field(
        default=None, description="UUID организации (null для персональной смены)"
    )
    started_at: datetime = Field(description="Начало смены")
    finished_at: datetime | None = Field(
        default=None, description="Конец смены (null если активна)"
    )
    status: str = Field(description="Статус: active, paused, finished")
    pauses: list[PauseResponse] = Field(description="Список пауз в смене")
    worked_seconds: int = Field(description="Отработанное время в секундах (за вычетом пауз)")
    has_incomplete_required_checklists: bool = Field(
        default=False,
        description="После завершения смены: был ли хотя бы один обязательный "
        "чек-лист не заполнен",
    )
    user_name: str | None = Field(
        default=None,
        description="Имя сотрудника (User.name). Заполнено в орг-контексте, в персональном — null",
    )
    user_email: str | None = Field(
        default=None,
        description="Email сотрудника (User.email). Заполнено в орг-контексте, "
        "в персональном — null",
    )
    role: str | None = Field(
        default=None,
        description="Системная роль в org: admin | employee. null, если участник "
        "исключён из org или персональный контекст",
    )
    custom_role_name: str | None = Field(
        default=None,
        description="Плоское имя кастомной роли (OrganizationRole.name). null, если "
        "кастомная роль не назначена / участник исключён / персональный контекст",
    )

    model_config = {"from_attributes": True}


class ShiftListResponse(BaseModel):
    items: list[ShiftResponse] = Field(description="Список смен")
    total: int = Field(description="Общее количество смен (без учёта пагинации)")
    limit: int = Field(description="Размер страницы")
    offset: int = Field(description="Смещение")


class ShiftStatsResponse(BaseModel):
    period: str | None = Field(
        default=None,
        description="Период: day, week, month. null при кастомном диапазоне date_from/date_to",
    )
    total_worked_seconds: int = Field(description="Суммарное отработанное время за период")
    shift_count: int = Field(description="Количество смен за период")
    average_shift_seconds: int = Field(description="Среднее время одной смены")
    range_from: datetime | None = Field(
        default=None,
        description="Фактически применённая нижняя граница окна (UTC). "
        "Пресет → вычисленное начало, кастом → переданный date_from",
    )
    range_to: datetime | None = Field(
        default=None,
        description="Фактически применённая верхняя граница окна (UTC). "
        "Пресет → момент сервера «сейчас», кастом → переданный date_to",
    )


class ShiftStartRequest(BaseModel):
    organization_id: str | None = Field(
        default=None, description="UUID организации (не указывать для персональной смены)"
    )
    latitude: float | None = Field(
        default=None,
        ge=-90,
        le=90,
        description="Широта (обязательно при геопроверке организации)",
    )
    longitude: float | None = Field(
        default=None,
        ge=-180,
        le=180,
        description="Долгота (обязательно при геопроверке организации)",
    )
