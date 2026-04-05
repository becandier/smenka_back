import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.app.api.v1.router import router as v1_router
from src.app.core.config import get_settings
from src.app.core.logging import get_logger, setup_logging
from src.app.schemas.base import ApiResponse
from src.app.services.auth import AuthError
from src.app.services.organization import OrgError
from src.app.services.shift import ShiftError

settings = get_settings()

setup_logging(
    json_logs=settings.app_env == "production",
    log_level="DEBUG" if settings.debug else "INFO",
)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("app_started")
    yield
    logger.info("app_stopped")


app = FastAPI(
    title="Smenka API",
    version="0.1.0",
    description="""API для учёта рабочего времени (shift tracking).

## Режимы работы

- **Персональный** — любой авторизованный пользователь может трекать рабочее время для себя.
- **Организационный** — владелец создаёт организацию, приглашает сотрудников, настраивает правила (геопроверка, лимиты пауз, автозавершение).

## Роли в организации

| Роль | Описание | Может трекать время | Управление |
|------|----------|---------------------|------------|
| **Owner** | Создатель организации | Нет | Полное: настройки, участники, статистика, смены сотрудников |
| **Admin** | Участник с расширенными правами | Да | Рабочие точки, просмотр смен и статистики |
| **Employee** | Обычный участник | Да | Только свои смены |

> **Важно:** Owner НЕ является участником организации и не может трекать в ней время. Это управленческая роль.

## Формат ответов

Все ответы обёрнуты в единую структуру:
```json
{"data": <payload>, "error": null}
```
При ошибке:
```json
{"data": null, "error": {"code": "ERROR_CODE", "message": "Описание ошибки"}}
```

## Авторизация

Используется JWT Bearer-токен. Получите `access_token` через `/auth/login` и передавайте в заголовке:
```
Authorization: Bearer <access_token>
```
""",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_tags=[
        {
            "name": "auth",
            "description": "Регистрация, верификация email, вход, обновление токенов, выход.",
        },
        {
            "name": "users",
            "description": "Профиль текущего пользователя.",
        },
        {
            "name": "shifts",
            "description": "Персональные смены: начало, пауза, возобновление, завершение, история и статистика.",
        },
        {
            "name": "organizations",
            "description": "CRUD организаций, инвайт-коды, управление участниками, настройки, смены и статистика сотрудников.",
        },
        {
            "name": "work-locations",
            "description": "Рабочие точки организации. Используются для геопроверки при начале смены.",
        },
    ],
)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = round((time.monotonic() - start) * 1000, 2)
    logger.info(
        "request_completed",
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    return response


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(
            code=exc.detail if isinstance(exc.detail, str) else "ERROR",
            message=str(exc.detail),
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    validation_errors = [
        {"field": ".".join(str(loc) for loc in err["loc"]), "message": err["msg"]}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=ApiResponse.fail(
            code="VALIDATION_ERROR",
            message="Ошибка валидации",
            validation=validation_errors,
        ).model_dump(),
    )


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(exc.code, exc.message).model_dump(),
    )


@app.exception_handler(ShiftError)
async def shift_error_handler(request: Request, exc: ShiftError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(exc.code, exc.message).model_dump(),
    )


@app.exception_handler(OrgError)
async def org_error_handler(request: Request, exc: OrgError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(exc.code, exc.message).model_dump(),
    )


app.include_router(v1_router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
