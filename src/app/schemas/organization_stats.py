from pydantic import BaseModel, Field


class EmployeeStatsResponse(BaseModel):
    user_id: str = Field(description="UUID сотрудника")
    user_name: str = Field(description="Имя сотрудника")
    user_email: str = Field(description="Email сотрудника")
    shift_count: int = Field(description="Количество смен")
    total_worked_seconds: int = Field(description="Суммарное отработанное время")
    average_shift_seconds: int = Field(description="Среднее время смены")


class OrgStatsResponse(BaseModel):
    period: str = Field(description="Период: day, week, month")
    total_worked_seconds: int = Field(description="Суммарное время всех сотрудников")
    shift_count: int = Field(description="Общее количество смен")
    average_shift_seconds: int = Field(description="Среднее время смены")
    per_employee: list[EmployeeStatsResponse] = Field(
        description="Статистика по каждому сотруднику"
    )
