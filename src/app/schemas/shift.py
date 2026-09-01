import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from src.app.models.shift import GeoFallbackReason
from src.app.schemas.overtime import OvertimeInfo


class PauseResponse(BaseModel):
    id: str = Field(description="UUID паузы")
    shift_id: str = Field(description="UUID смены")
    started_at: datetime = Field(description="Начало паузы")
    finished_at: datetime | None = Field(
        default=None, description="Конец паузы (null если активна)"
    )

    model_config = {"from_attributes": True}


class ShiftWorkLocation(BaseModel):
    """Денормализованная точка смены для отображения (текущее значение точки)."""

    id: str = Field(description="UUID рабочей точки")
    name: str = Field(description="Название точки")
    address: str | None = Field(default=None, description="Читаемый адрес точки")

    model_config = {"from_attributes": True}


class ShiftChecklistsSummary(BaseModel):
    """Сводка по чек-листам смены (checklist_reports). Только для орг-эндпоинтов."""

    total: int = Field(description="Всего экземпляров чек-листов в смене")
    completed: int = Field(description="Экземпляров со status=completed")
    required_total: int = Field(description="Обязательных экземпляров (is_required=true)")
    required_incomplete: int = Field(description="Обязательных экземпляров со status != completed")


class ShiftEarnings(BaseModel):
    """Заработок по одной смене (shift_history_earnings, ADR-005).

    Единственный источник правды — payroll-сервис (`payroll._calc_earnings` /
    `get_shift_earnings_map`); ничего здесь не пересчитывается на клиенте
    (ADR-005 п.9). `penalty_amount_minor`/`adjustment_amount_minor` — только
    штрафы/корректировки, привязанные к ЭТОЙ смене (`shift_id`); непривязанные
    видны лишь в итогах периода (`GET /organizations/{org_id}/my-earnings`).
    """

    currency: str = Field(description="Валюта суммы, сейчас всегда RUB")
    gross_amount_minor: int = Field(
        description="Начисление по смене в копейках (ADR-005 п.2/5), округлённое "
        "half-up. 0, если `has_rate=false` — это не факт заработка, а отсутствие ставки"
    )
    penalty_amount_minor: int = Field(
        description="Сумма активных штрафов, привязанных к этой смене (`penalties.shift_id`), "
        "в копейках. >= 0"
    )
    penalties_count: int = Field(description="Число активных штрафов, привязанных к смене")
    adjustment_amount_minor: int = Field(
        description="Знаковая сумма активных корректировок, привязанных к этой смене "
        "(`payroll_adjustments.shift_id`), в копейках. Может быть отрицательной"
    )
    adjustments_count: int = Field(description="Число активных корректировок, привязанных к смене")
    net_amount_minor: int = Field(
        description="gross − penalty + adjustment. Может быть отрицательным — валидное "
        "значение (ADR-005 п.1)"
    )
    overtime_seconds: int = Field(
        description="Согласованная переработка, уже учтённая в gross (для per_shift на "
        "сумму не влияет, но отображается)"
    )
    has_rate: bool = Field(
        description="false — на момент started_at у сотрудника не было действующей ставки. "
        "Тогда gross_amount_minor=0, но это ЗНАЧИТ «ставка не задана», а не «заработал 0» — "
        "клиент обязан отличать эти состояния (ADR-005 п.3)"
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
    work_location_id: str | None = Field(
        default=None,
        description="UUID точки, на которой открыта смена (null — не определена / "
        "персональная смена)",
    )
    work_location: ShiftWorkLocation | None = Field(
        default=None,
        description="Денормализованная точка смены {id, name, address} (null — нет точки)",
    )
    pauses: list[PauseResponse] = Field(description="Список пауз в смене")
    worked_seconds: int = Field(description="Отработанное время в секундах (за вычетом пауз)")
    has_incomplete_required_checklists: bool = Field(
        default=False,
        description="После завершения смены: был ли хотя бы один обязательный "
        "чек-лист не заполнен",
    )
    user_name: str | None = Field(
        default=None,
        description="Настоящее имя сотрудника (User.name). Заполнено в орг-контексте, "
        "в персональном — null",
    )
    display_name: str | None = Field(
        default=None,
        description="Имя сотрудника в этой организации (organization_members.display_name); "
        "null — не задано или персональный контекст (member_display_name)",
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
    checklists_summary: ShiftChecklistsSummary | None = Field(
        default=None,
        description="Сводка по чек-листам смены (total/completed/required_total/"
        "required_incomplete). Заполняется ТОЛЬКО в орг-эндпоинтах "
        "(GET /organizations/{org_id}/shifts и .../shifts/{shift_id}); в "
        "персональных эндпоинтах — всегда null (checklist_reports)",
    )
    work_schedule_id: str | None = Field(
        default=None,
        description="UUID графика работы (null — без графика / персональная смена)",
    )
    schedule_name: str | None = Field(
        default=None,
        description="Снимок имени графика на момент старта смены; null — без графика",
    )
    scheduled_start_at: datetime | None = Field(
        default=None, description="Снимок планового начала окна (UTC); null — без графика"
    )
    scheduled_end_at: datetime | None = Field(
        default=None, description="Снимок планового конца окна (UTC); null — без графика"
    )
    late_seconds: int | None = Field(
        default=None,
        description="Опоздание в секундах относительно scheduled_start_at, за вычетом "
        "допуска организации; null — без графика (work_schedules, R5)",
    )
    finish_reason: str | None = Field(
        default=None,
        description="manual | auto_schedule | null (активные и исторические смены до "
        "фичи work_schedules)",
    )
    overtime: OvertimeInfo | None = Field(
        default=None, description="Последняя заявка на переработку по смене; null — заявки нет"
    )
    is_manual: bool = Field(
        default=False,
        description="true — смена заведена админом (created_by_user_id IS NOT NULL), а не "
        "начата сотрудником (manual_time_entry)",
    )
    is_edited: bool = Field(
        default=False,
        description="true — смену когда-либо правил админ (edited_at IS NOT NULL)",
    )
    manual_note: str | None = Field(
        default=None,
        description="Комментарий/причина последней ручной операции; null — не задан",
    )
    edited_at: datetime | None = Field(
        default=None, description="Момент последней ручной правки; null — не правилась"
    )
    edited_by_name: str | None = Field(
        default=None,
        description="Имя админа, последним правившего смену. Заполнено только в "
        "орг-эндпоинтах; в персональных — всегда null",
    )
    created_by_name: str | None = Field(
        default=None,
        description="Имя админа, создавшего смену вручную. Заполнено только в "
        "орг-эндпоинтах; в персональных — всегда null",
    )
    is_deleted: bool = Field(
        default=False,
        description="true — смена удалена (soft-delete). В обычных выборках всегда false; "
        "возможно true только при include_deleted=true и в ответе DELETE",
    )
    geo_fallback: bool = Field(
        default=False,
        description="true — смена стартовала без геопроверки, по фото с камеры "
        "(shift_geo_photo_fallback). Derived: geo_fallback_reason IS NOT NULL",
    )
    geo_fallback_reason: str | None = Field(
        default=None,
        description="Машинный код гео-ошибки клиента на старте: GEO_PERMISSION_DENIED, "
        "GEO_PERMISSION_DENIED_FOREVER, GEO_SERVICE_DISABLED, GEO_UNAVAILABLE, "
        "GEO_UNSUPPORTED, GEO_INSECURE_CONTEXT. null — обычная смена",
    )
    geo_fallback_photo_file_id: str | None = Field(
        default=None,
        description="UUID файла-снимка (категория shift_geo_photo) — смотреть через "
        "GET /files/{file_id}. null — обычная смена либо фото уже удалено",
    )
    earnings: ShiftEarnings | None = Field(
        default=None,
        description="Заработок по этой смене (shift_history_earnings, ADR-005). null — "
        "персональная смена (organization_id=null) либо смена не в статусе finished "
        "(сумма ещё меняется). Заполняется только в GET /shifts и GET /shifts/{shift_id}",
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
    work_location_id: str | None = Field(
        default=None,
        description="UUID точки смены. При гео-проверке игнорируется (точку определяет "
        "сервер). При выключенной гео обязателен, если у организации включён "
        "`require_work_location`; иначе опционален",
    )
    work_schedule_id: str | None = Field(
        default=None,
        description="UUID графика работы. Игнорируется для персональных смен. Если "
        "передан — обязан быть в эффективном наборе сотрудника (иначе "
        "SCHEDULE_NOT_AVAILABLE). Если не передан — сервер подставляет автоматически "
        "(1 доступный) или требует выбора (require_schedule=true и >1/0 доступных)",
    )
    geo_fallback_photo_id: str | None = Field(
        default=None,
        description="UUID уже загруженного файла категории `shift_geo_photo` — снимок "
        "с камеры вместо координат, когда геолокация недоступна. Только вместе с "
        "`geo_fallback_reason`, только для организации с геопроверкой и только без "
        "координат; требует `work_location_id` (shift_geo_photo_fallback)",
    )
    geo_fallback_reason: GeoFallbackReason | None = Field(
        default=None,
        description="Машинный код гео-ошибки клиента: GEO_PERMISSION_DENIED, "
        "GEO_PERMISSION_DENIED_FOREVER, GEO_SERVICE_DISABLED, GEO_UNAVAILABLE, "
        "GEO_UNSUPPORTED, GEO_INSECURE_CONTEXT. Только вместе с `geo_fallback_photo_id`",
    )


# --- manual_time_entry: ручной ввод/правка/удаление смены администратором ---
class ManualPauseInput(BaseModel):
    """Пауза в ручном вводе — обе границы обязательны (незакрытых пауз не бывает)."""

    started_at: datetime = Field(description="Начало паузы (UTC)")
    finished_at: datetime = Field(description="Конец паузы (UTC)")


class ManualShiftCreate(BaseModel):
    user_id: uuid.UUID = Field(description="Сотрудник — действующий участник организации")
    started_at: datetime = Field(description="Начало смены (UTC)")
    finished_at: datetime = Field(
        description="Конец смены (UTC) — ручная смена создаётся сразу завершённой"
    )
    work_location_id: uuid.UUID | None = Field(
        default=None, description="UUID точки организации (архивные/удалённые допускаются)"
    )
    work_schedule_id: uuid.UUID | None = Field(
        default=None,
        description="UUID графика организации — снимок планового окна вычисляется той же "
        "логикой, что и PATCH .../shifts/{id}/schedule",
    )
    pauses: list[ManualPauseInput] = Field(
        default_factory=list, description="Паузы смены (опционально, по умолчанию пусто)"
    )
    note: str | None = Field(
        default=None, max_length=500, description="Комментарий/причина ручного ввода"
    )


class ManualShiftUpdate(BaseModel):
    """Правка смены вручную. Все поля опциональны — применяются только переданные."""

    started_at: datetime | None = Field(default=None)
    finished_at: datetime | None = Field(
        default=None,
        description="Для active/paused смены передача завершает её задним числом "
        "(status=finished, finish_reason=manual)",
    )
    work_location_id: uuid.UUID | None = Field(default=None, description="null снимает точку")
    pauses: list[ManualPauseInput] | None = Field(
        default=None,
        description="Полная замена списка пауз смены. Не передан — паузы не трогаются",
    )
    note: str | None = Field(default=None, max_length=500)


class ShiftDeletedResponse(BaseModel):
    deleted: bool = Field(description="Смена удалена (soft-delete)")
