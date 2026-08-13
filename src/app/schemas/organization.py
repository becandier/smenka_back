from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from src.app.core.security import validate_login_format, validate_required_text
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
        return validate_required_text(value, noun="Название")


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
    overtime_request_days: int = Field(
        description="Срок подачи заявки на переработку в днях (work_schedules), read-only — "
        "денормализовано из OrganizationSettings, чтобы employee (без доступа к /settings) "
        "мог скрыть кнопку подачи заявки после истечения срока"
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
    user_email: str = Field(
        description="Email участника; пустая строка, если email не задан "
        "(admin_created_accounts) — тип остаётся str для обратной совместимости"
    )
    user_login: str | None = Field(
        default=None,
        description="Логин участника (admin_created_accounts); null, если не задан",
    )
    password_managed: bool = Field(
        default=False,
        description="true, если учётка заведена этой организацией "
        "(users.created_by_org_id == org_id) — UI показывает «Сменить пароль» и "
        "разрешает править логин (admin_created_accounts)",
    )
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


class MemberUpdateRequest(BaseModel):
    """Partial-обновление участника: переданные ключи правятся, остальные не трогаются.

    `display_name` — управленческий атрибут (owner/admin/super_admin, независимо
    от происхождения учётки). `login` — только для учёток, заведённых ЭТОЙ
    организацией (`users.created_by_org_id`), иначе 403 PASSWORD_RESET_NOT_ALLOWED
    (admin_created_accounts).
    """

    display_name: str | None = Field(
        default=None,
        description="Имя участника в этой организации; null или пустая строка — "
        "сброс на настоящее имя (User.name). Нормализация и лимит 1–100 символов "
        "— на сервере (400 INVALID_DISPLAY_NAME). Ключ отсутствует — не менять.",
    )
    login: str | None = Field(
        default=None,
        description="Новый логин учётки: 3–32 символа, латиница/цифры/._-, "
        "регистронезависимая уникальность по платформе; занят → 409 LOGIN_TAKEN. "
        "Ключ отсутствует/null — не менять (admin_created_accounts).",
    )

    @field_validator("login")
    @classmethod
    def _validate_login(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        return validate_login_format(v)
