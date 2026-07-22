from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from src.app.schemas.organization_role import RoleResponse
from src.app.schemas.payroll import CurrentRateResponse


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255, description="Название организации")


class OrganizationUpdate(BaseModel):
    # Опционально (exclude_unset) — начиная с work_schedules можно обновить
    # только `timezone`, не указывая `name` заново (админка шлёт их отдельными
    # запросами, см. backend.md). Передача только `name`, как раньше, работает
    # без изменений — обратная совместимость сохранена.
    name: str | None = Field(
        default=None,
        description="Новое название (обрезается по краям, 1–255 символов)",
    )
    timezone: str | None = Field(
        default=None,
        description="IANA-имя таймзоны организации (напр. Europe/Moscow). "
        "Должно резолвиться через zoneinfo, иначе 400 INVALID_TIMEZONE",
    )

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Trim по краям применяем ДО проверок: пустое после trim и >255 символов → 422.
        stripped = value.strip()
        if not stripped:
            raise ValueError("Название не может быть пустым")
        if len(stripped) > 255:
            raise ValueError("Название не может превышать 255 символов")
        return stripped


class OrganizationResponse(BaseModel):
    id: str = Field(description="UUID организации")
    name: str = Field(description="Название")
    owner_id: str = Field(description="UUID владельца")
    invite_code: str = Field(description="Инвайт-код для присоединения")
    is_deleted: bool = Field(description="Помечена как удалённая")
    geo_check_enabled: bool = Field(description="Геопроверка при начале смены")
    require_work_location: bool = Field(
        default=False,
        description="Требовать привязку точки к смене (для активации обязательного "
        "выбора точки на клиенте при выключенной гео)",
    )
    timezone: str = Field(
        description="IANA-имя таймзоны организации (work_schedules) — по нему считаются "
        "плановые окна графиков и отчёты"
    )
    created_at: datetime = Field(description="Дата создания")
    my_role: str | None = Field(
        default=None,
        description="Роль текущего пользователя: owner, admin или employee. null только в "
        "/organizations/all для super_admin, не состоящего в организации.",
    )
    my_custom_role: RoleResponse | None = Field(
        default=None,
        description="Кастомная роль текущего пользователя (у owner всегда null)",
    )

    model_config = {"from_attributes": True}


class OrganizationListResponse(BaseModel):
    items: list[OrganizationResponse] = Field(description="Список организаций")


class MemberResponse(BaseModel):
    id: str = Field(description="UUID записи об участии")
    organization_id: str = Field(description="UUID организации")
    user_id: str = Field(description="UUID пользователя")
    user_name: str = Field(description="Настоящее имя участника (User.name)")
    user_email: str = Field(description="Email участника")
    display_name: str | None = Field(
        default=None,
        description="Имя участника в этой организации (задаётся owner/admin через "
        "PATCH .../members/{member_user_id}); null — используется user_name",
    )
    role: str = Field(description="Системная роль: admin или employee")
    custom_role: RoleResponse | None = Field(
        default=None,
        description="Кастомная роль организации (или null)",
    )
    joined_at: datetime = Field(description="Дата присоединения")
    current_rate: CurrentRateResponse | None = Field(
        default=None,
        description="Действующая ставка участника (видна только owner/admin; "
        "null, если ставок нет, все ставки в будущем или у читателя нет прав)",
    )

    model_config = {"from_attributes": True}


class MemberListResponse(BaseModel):
    items: list[MemberResponse] = Field(description="Список участников")


class JoinResponse(BaseModel):
    organization_id: str = Field(description="UUID организации")
    organization_name: str = Field(description="Название организации")
    role: str = Field(description="Назначенная роль (employee)")
    geo_check_enabled: bool = Field(description="Геопроверка при начале смены")
    require_work_location: bool = Field(
        default=False, description="Требовать привязку точки к смене"
    )


class InviteCodeResponse(BaseModel):
    invite_code: str = Field(description="Новый инвайт-код")


class MemberRoleUpdate(BaseModel):
    role: str = Field(description="Новая роль: admin или employee")


class MemberDisplayNameUpdate(BaseModel):
    display_name: str | None = Field(
        description="Имя участника в этой организации; null или пустая строка — "
        "сброс на настоящее имя (User.name). Нормализация и лимит 1–100 символов "
        "— на сервере (400 INVALID_DISPLAY_NAME)",
    )
