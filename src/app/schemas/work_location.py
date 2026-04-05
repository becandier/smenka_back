from datetime import datetime

from pydantic import BaseModel, Field


class WorkLocationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="Название рабочей точки")
    latitude: float = Field(ge=-90, le=90, description="Широта (-90 до 90)")
    longitude: float = Field(ge=-180, le=180, description="Долгота (-180 до 180)")
    radius_meters: int = Field(
        default=100, ge=10, le=10000, description="Радиус зоны в метрах (10–10000, по умолчанию 100)"
    )


class WorkLocationUpdate(BaseModel):
    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="Новое название"
    )
    latitude: float | None = Field(default=None, ge=-90, le=90, description="Новая широта")
    longitude: float | None = Field(default=None, ge=-180, le=180, description="Новая долгота")
    radius_meters: int | None = Field(
        default=None, ge=10, le=10000, description="Новый радиус в метрах"
    )


class WorkLocationResponse(BaseModel):
    id: str = Field(description="UUID рабочей точки")
    organization_id: str = Field(description="UUID организации")
    name: str = Field(description="Название")
    latitude: float = Field(description="Широта")
    longitude: float = Field(description="Долгота")
    radius_meters: int = Field(description="Радиус зоны в метрах")
    created_at: datetime = Field(description="Дата создания")

    model_config = {"from_attributes": True}


class WorkLocationListResponse(BaseModel):
    items: list[WorkLocationResponse] = Field(description="Список рабочих точек")
