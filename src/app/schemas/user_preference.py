import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Формат ключа (ТЗ table_column_settings): первый символ — [a-z0-9], дальше
# до 119 символов из [a-z0-9._-]. Первый сегмент до точки — пространство имён
# клиента (`admin.*`, `mobile.*`), сервер его не проверяет.
PREFERENCE_KEY_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,119}$"
PREFERENCE_KEY_MAX_LENGTH = 120
# Префикс фильтра GET: 1..120 символов из алфавита ключа.
PREFERENCE_PREFIX_PATTERN = r"^[a-z0-9._-]{1,120}$"
PREFERENCE_VALUE_MAX_BYTES = 8192
PREFERENCE_VALUE_MAX_DEPTH = 32
_NUL_MESSAGE = "Значение не должно содержать нулевой символ (\\u0000)"


def check_structure(value: dict[str, Any]) -> None:
    """Глубина вложенности и нулевой символ — итеративно, с ранним выходом.

    Глубина: сам `value` — уровень 1, каждый вложенный объект или массив +1.
    Без ограничения значение глубже ~255 уровней проходит размер, сохраняется,
    а затем не сериализуется pydantic-core в ответ (и ломает все GET). Обход —
    стеком, без рекурсии, чтобы сама проверка не упала на глубоком значении.

    `"\\x00"` в строке или ключе на любой глубине Python пропускает, а
    PostgreSQL JSONB отвергает (`unsupported Unicode escape sequence`).
    Проверяется символ в распарсенных строках, а не подстрока `\\u0000` в
    сериализованном JSON — литерал «обратный слэш + u0000» допустим.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, str):
            if "\x00" in node:
                raise ValueError(_NUL_MESSAGE)
            continue
        if not isinstance(node, dict | list):
            continue
        if depth > PREFERENCE_VALUE_MAX_DEPTH:
            raise ValueError(f"Вложенность значения больше {PREFERENCE_VALUE_MAX_DEPTH} уровней")
        if isinstance(node, dict):
            if any("\x00" in key for key in node):
                raise ValueError(_NUL_MESSAGE)
            items: Iterable[Any] = node.values()
        else:
            items = node
        stack.extend((item, depth + 1) for item in items)


def serialized_size(value: dict[str, Any]) -> int:
    """Размер значения в байтах UTF-8 компактного JSON (без пробелов-разделителей).

    `allow_nan=False`: парсер тела запроса принимает `NaN`/`Infinity`, но это не
    JSON и PostgreSQL JSONB их отвергает — поднимается `ValueError` (→ 422).
    """
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(serialized.encode("utf-8"))


class PreferenceUpdate(BaseModel):
    value: dict[str, Any] = Field(
        description=(
            "JSON-объект (не массив, не скаляр, не null), не больше 8192 байт "
            "в сериализованном виде, вложенность не больше 32 уровней (сам объект — "
            "уровень 1), без нулевого символа \\u0000 в строках и ключах, без "
            "NaN/Infinity. Содержимое сервер не разбирает."
        ),
    )

    @field_validator("value")
    @classmethod
    def _check_value(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Структура — до json.dumps: глубокое значение иначе уронило бы сериализацию.
        check_structure(value)
        try:
            size = serialized_size(value)
        except ValueError as exc:
            # NaN/Infinity (allow_nan=False) и одиночные суррогаты (encode utf-8).
            raise ValueError("Значение должно быть корректным JSON") from exc
        if size > PREFERENCE_VALUE_MAX_BYTES:
            raise ValueError(
                f"Значение больше {PREFERENCE_VALUE_MAX_BYTES} байт в сериализованном виде"
            )
        return value


class PreferenceResponse(BaseModel):
    key: str
    value: dict[str, Any]
    updated_at: datetime = Field(description="Время записи на сервере, UTC")


class PreferencesResponse(BaseModel):
    preferences: dict[str, dict[str, Any]] = Field(
        description="Настройки текущего пользователя: ключ → значение"
    )


class PreferenceDeleteResponse(BaseModel):
    deleted: int = Field(description="1 — ключ удалён, 0 — ключа не было")
