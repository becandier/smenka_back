from pydantic import BaseModel


class EmployeeStatsResponse(BaseModel):
    user_id: str
    user_name: str
    user_email: str
    shift_count: int
    total_worked_seconds: int
    average_shift_seconds: int


class OrgStatsResponse(BaseModel):
    period: str
    total_worked_seconds: int
    shift_count: int
    average_shift_seconds: int
    per_employee: list[EmployeeStatsResponse]
