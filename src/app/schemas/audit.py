from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class AuditLogEntry(BaseModel):
    id: str = Field(description="UUID записи аудита")
    organization_id: str | None = Field(description="UUID организации (null для платформенных)")
    actor_user_id: str | None = Field(description="UUID инициатора (null = системное действие)")
    actor_name: str = Field(description='Имя инициатора или "Система" для авто-действий')
    action: str = Field(description="Машинный код действия, напр. settings.update")
    resource_type: str = Field(description="Тип объекта: organization/member/settings/...")
    resource_id: str | None = Field(description="UUID затронутого объекта")
    summary: dict[str, Any] | None = Field(description="Краткий контекст/дифф без секретов")
    ip_address: str | None = Field(description="IP инициатора (IPv4/IPv6)")
    created_at: datetime = Field(description="Момент записи (UTC)")


class AuditLogListResponse(BaseModel):
    items: list[AuditLogEntry]
    total: int = Field(description="Всего записей под фильтр")
    limit: int
    offset: int
