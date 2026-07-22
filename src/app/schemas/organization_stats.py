from datetime import datetime

from pydantic import BaseModel, Field


class EmployeeStatsResponse(BaseModel):
    user_id: str = Field(description="UUID сотрудника")
    user_name: str = Field(description="Настоящее имя сотрудника (User.name)")
    user_email: str = Field(description="Email сотрудника")
    display_name: str | None = Field(
        default=None,
        description="Имя сотрудника в этой организации; null — не задано (member_display_name)",
    )
    shift_count: int = Field(description="Количество смен")
    total_worked_seconds: int = Field(description="Суммарное отработанное время")
    average_shift_seconds: int = Field(description="Среднее время смены")


class OrgStatsResponse(BaseModel):
    period: str | None = Field(
        default=None,
        description="Период: day, week, month. null при кастомном диапазоне date_from/date_to",
    )
    total_worked_seconds: int = Field(description="Суммарное время всех сотрудников")
    shift_count: int = Field(description="Общее количество смен")
    average_shift_seconds: int = Field(description="Среднее время смены")
    per_employee: list[EmployeeStatsResponse] = Field(
        description="Статистика по каждому сотруднику"
    )
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
