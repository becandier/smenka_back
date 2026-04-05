from typing import Any

from pydantic import BaseModel, Field


class ApiError(BaseModel):
    code: str = Field(description="Код ошибки (e.g. SHIFT_NOT_FOUND, FORBIDDEN)")
    message: str = Field(description="Человекочитаемое описание ошибки")
    validation: list[dict[str, str]] | None = Field(
        default=None, description="Детали ошибок валидации полей"
    )


class ApiResponse(BaseModel):
    data: Any | None = Field(default=None, description="Полезная нагрузка ответа (null при ошибке)")
    error: ApiError | None = Field(default=None, description="Описание ошибки (null при успехе)")

    @classmethod
    def success(cls, data: Any = None) -> "ApiResponse":
        return cls(data=data)

    @classmethod
    def fail(
        cls,
        code: str,
        message: str,
        validation: list[dict[str, str]] | None = None,
    ) -> "ApiResponse":
        return cls(error=ApiError(code=code, message=message, validation=validation))
