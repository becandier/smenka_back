import json
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
            "в сериализованном виде. Содержимое сервер не разбирает."
        ),
    )

    @field_validator("value")
    @classmethod
    def _check_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            size = serialized_size(value)
        except ValueError as exc:
            raise ValueError("Значение должно быть корректным JSON (без NaN/Infinity)") from exc
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
