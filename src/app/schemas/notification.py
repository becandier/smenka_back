from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class NotificationOut(BaseModel):
    id: str
    type: str = Field(description="Строковый тип события, напр. test_assigned")
    title: str
    body: str | None = None
    payload: dict[str, Any] | None = Field(
        default=None,
        description="Машинные данные для перехода клиента (структура зависит от type)",
    )
    is_read: bool
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationOut]
    total: int
    limit: int
    offset: int


class UnreadCountResponse(BaseModel):
    count: int = Field(description="Число непрочитанных уведомлений текущего пользователя")


class ReadAllResponse(BaseModel):
    updated: int = Field(description="Сколько уведомлений было помечено прочитанными")
