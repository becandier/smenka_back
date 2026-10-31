"""Каталог разделов веб-кабинета организации для профилей доступа (ADR-006).

Единый источник правды: валидация `permissions` профиля, вычисление
эффективных прав полного администратора (все разделы на максимальном уровне),
сообщения об ошибке недостатка прав и `GET /access-permissions` для клиентов.

Новый раздел кабинета добавляется одной записью в `ACCESS_SECTIONS`; порядок
записей — порядок, в котором клиент строит конструктор прав.
"""

from dataclasses import dataclass
from typing import Literal

AccessLevel = Literal["view", "manage"]

LEVEL_VIEW: AccessLevel = "view"
LEVEL_MANAGE: AccessLevel = "manage"

# `manage` покрывает `view`: чем больше ранг, тем шире доступ.
_LEVEL_RANK: dict[str, int] = {LEVEL_VIEW: 1, LEVEL_MANAGE: 2}

_LEVEL_TITLES: dict[str, str] = {LEVEL_VIEW: "просмотр", LEVEL_MANAGE: "управление"}


@dataclass(frozen=True)
class AccessSection:
    code: str
    group: str
    levels: tuple[AccessLevel, ...]
    title: str

    @property
    def max_level(self) -> AccessLevel:
        return self.levels[-1]


ACCESS_SECTIONS: tuple[AccessSection, ...] = (
    AccessSection("shifts", "shifts", (LEVEL_VIEW, LEVEL_MANAGE), "Смены"),
    AccessSection("overtime", "shifts", (LEVEL_VIEW, LEVEL_MANAGE), "Переработки"),
    AccessSection("schedules", "shifts", (LEVEL_VIEW, LEVEL_MANAGE), "Графики работы"),
    AccessSection("members", "staff", (LEVEL_VIEW, LEVEL_MANAGE), "Сотрудники"),
    AccessSection("roles", "staff", (LEVEL_VIEW, LEVEL_MANAGE), "Роли"),
    AccessSection("checklist_instances", "checklists", (LEVEL_VIEW,), "Заполненные чек-листы"),
    AccessSection(
        "checklist_templates", "checklists", (LEVEL_VIEW, LEVEL_MANAGE), "Шаблоны чек-листов"
    ),
    AccessSection("tests", "tests", (LEVEL_VIEW, LEVEL_MANAGE), "Тесты"),
    AccessSection("payroll", "finance", (LEVEL_VIEW, LEVEL_MANAGE), "Зарплата"),
    AccessSection("penalties", "finance", (LEVEL_VIEW, LEVEL_MANAGE), "Штрафы"),
    AccessSection("stats", "organization", (LEVEL_VIEW,), "Статистика"),
    AccessSection("knowledge", "organization", (LEVEL_VIEW, LEVEL_MANAGE), "База знаний"),
    AccessSection("work_locations", "organization", (LEVEL_VIEW, LEVEL_MANAGE), "Точки"),
    AccessSection("invites", "organization", (LEVEL_VIEW, LEVEL_MANAGE), "Приглашения"),
    AccessSection("settings", "organization", (LEVEL_VIEW, LEVEL_MANAGE), "Настройки"),
    AccessSection("audit", "organization", (LEVEL_VIEW,), "Журнал аудита"),
)

SECTIONS_BY_CODE: dict[str, AccessSection] = {s.code: s for s in ACCESS_SECTIONS}


def full_permissions() -> dict[str, str]:
    """Права полного администратора: все разделы каталога на максимальном уровне."""
    return {s.code: s.max_level for s in ACCESS_SECTIONS}


def level_covers(granted: str | None, required: str) -> bool:
    """True, если выданный уровень покрывает требуемый (`manage` ⊇ `view`)."""
    if granted is None:
        return False
    return _LEVEL_RANK.get(granted, 0) >= _LEVEL_RANK[required]


def validate_permissions(raw: dict[str, str]) -> dict[str, str]:
    """Проверить набор прав профиля по каталогу; ValueError — для Pydantic (422)."""
    if not raw:
        raise ValueError("Профиль доступа должен содержать хотя бы один раздел")
    for code, level in raw.items():
        section = SECTIONS_BY_CODE.get(code)
        if section is None:
            raise ValueError(f"Неизвестный раздел: {code}")
        if level not in section.levels:
            allowed = ", ".join(section.levels)
            raise ValueError(f"Недопустимый уровень «{level}» для раздела {code}: {allowed}")
    # Порядок ключей — как в каталоге: стабильный JSONB и ответ API.
    return {s.code: raw[s.code] for s in ACCESS_SECTIONS if s.code in raw}


def forbidden_message(section: str, level: str) -> str:
    """Текст 403 FORBIDDEN при нехватке права профиля (backend.md, «Ошибка недостатка прав»)."""
    entry = SECTIONS_BY_CODE.get(section)
    title = entry.title if entry is not None else section
    return f"Недостаточно прав: требуется «{title}» — {_LEVEL_TITLES.get(level, level)}"
