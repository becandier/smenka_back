from typing import Any

from pydantic import BaseModel


class ApiError(BaseModel):
    code: str
    message: str
    validation: list[dict[str, str]] | None = None


class ApiResponse(BaseModel):
    data: Any | None = None
    error: ApiError | None = None

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
