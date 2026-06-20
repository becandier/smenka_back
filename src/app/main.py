import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from src.app.api.v1.router import router as v1_router
from src.app.core.config import get_settings
from src.app.core.logging import get_logger, setup_logging
from src.app.core.rate_limit import limiter
from src.app.core.sentry import init_sentry
from src.app.schemas.base import ApiResponse
from src.app.services.admin import AdminError
from src.app.services.auth import AuthError
from src.app.services.checklist_template import ChecklistError
from src.app.services.common import AccessError
from src.app.services.organization import OrgError
from src.app.services.organization_role import RoleError
from src.app.services.payroll import PayrollError
from src.app.services.shift import ShiftError

settings = get_settings()

setup_logging(
    json_logs=settings.app_env == "production",
    log_level="DEBUG" if settings.debug else "INFO",
)
logger = get_logger(__name__)

# Sentry инициализируется до создания приложения; при пустом DSN — no-op.
init_sentry()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("app_started")
    yield
    logger.info("app_stopped")


app = FastAPI(
    title="Smenka API",
    version="0.1.0",
    redirect_slashes=False,
    description=(
        "API для учёта рабочего времени (shift tracking).\n"
        "\n"
        "## Режимы работы\n"
        "\n"
        "- **Персональный** — любой авторизованный пользователь может трекать рабочее время для "
        "себя.\n"
        "- **Организационный** — владелец создаёт организацию, приглашает сотрудников, "
        "настраивает правила (геопроверка, лимиты пауз, автозавершение).\n"
        "\n"
        "## Роли в организации\n"
        "\n"
        "| Роль | Описание | Может трекать время | Управление |\n"
        "|------|----------|---------------------|------------|\n"
        "| **Owner** | Создатель организации | Нет | Полное: настройки, участники, статистика, "
        "смены сотрудников |\n"
        "| **Admin** | Участник с расширенными правами | Да | Рабочие точки, просмотр смен и "
        "статистики |\n"
        "| **Employee** | Обычный участник | Да | Только свои смены |\n"
        "\n"
        "> **Важно:** Owner НЕ является участником организации и не может трекать в ней время. "
        "Это управленческая роль.\n"
        "\n"
        "## Формат ответов\n"
        "\n"
        "Все ответы обёрнуты в единую структуру:\n"
        "```json\n"
        '{"data": <payload>, "error": null}\n'
        "```\n"
        "При ошибке:\n"
        "```json\n"
        '{"data": null, "error": {"code": "ERROR_CODE", "message": "Описание '
        'ошибки"}}\n'
        "```\n"
        "\n"
        "## Авторизация\n"
        "\n"
        "Используется JWT Bearer-токен. Получите `access_token` через `/auth/login` и передавайте "
        "в заголовке:\n"
        "```\n"
        "Authorization: Bearer <access_token>\n"
        "```\n"
    ),
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
            "description": (
                "Персональные смены: начало, пауза, возобновление, завершение, "
                "история и статистика."
            ),
        },
        {
            "name": "organizations",
            "description": (
                "CRUD организаций, инвайт-коды, управление участниками, "
                "настройки, смены и статистика сотрудников."
            ),
        },
        {
            "name": "organization-roles",
            "description": (
                "Кастомные роли организации (бариста, кассир и т.п.) и их назначение участникам."
            ),
        },
        {
            "name": "checklist-templates",
            "description": "Шаблоны чек-листов организации (открытие/закрытие смены) и их пункты.",
        },
        {
            "name": "checklist-assignments",
            "description": (
                "Назначение шаблонов ролям и личные переопределения для "
                "сотрудников. Вычисление эффективных чек-листов."
            ),
        },
        {
            "name": "checklist-overrides",
            "description": (
                "Гранулярные операции над личными overrides: upsert и удаление "
                "по паре (шаблон, сотрудник), список overrides сотрудника."
            ),
        },
        {
            "name": "checklist-instances",
            "description": "Экземпляры чек-листов смены (снимки): просмотр и заполнение пунктов.",
        },
        {
            "name": "work-locations",
            "description": (
                "Рабочие точки организации. Используются для геопроверки при начале смены."
            ),
        },
        {
            "name": "payroll",
            "description": (
                "Ставки участников (история с effective_from) и расчёт "
                "зарплаты: отчёт по организации и личный заработок."
            ),
        },
        {
            "name": "admin",
            "description": (
                "Платформенные эндпоинты super_admin: пользователи, обзор "
                "организаций, сводная статистика."
            ),
        },
    ],
)


# Rate-limit (slowapi): объект limiter доступен приложению; декораторы
# @limiter.limit(...) навешаны на auth-эндпоинты, обработчик RateLimitExceeded —
# ниже (оборачивает в конверт {data,error}).
app.state.limiter = limiter


@app.middleware("http")
async def logging_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
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


# CORS: добавляется после logging-middleware, чтобы оказаться внешним слоем
# (Starlette применяет middleware в обратном порядке добавления) и корректно
# обрабатывать preflight-запросы браузерных клиентов — веб-админки и веб-версии
# мобилки (flutter build web).
# allow_credentials=False: клиенты шлют JWT в заголовке Authorization, не в cookie.
# Включать True только при переходе на httpOnly-cookie + CSRF (отдельная задача).
# При credentials=False wildcard allow_headers/allow_methods=["*"] валиден и
# покрывает Authorization/Content-Type и методы GET/POST/PATCH/PUT/DELETE/OPTIONS.
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(
            code=exc.detail if isinstance(exc.detail, str) else "ERROR",
            message=str(exc.detail),
        ).model_dump(),
    )


def _retry_after_seconds(exc: RateLimitExceeded) -> int:
    """Сколько секунд до сброса окна лимита (длина окна сработавшего лимита)."""
    limit = getattr(exc, "limit", None)
    item = getattr(limit, "limit", None)
    get_expiry = getattr(item, "get_expiry", None)
    if callable(get_expiry):
        try:
            return int(get_expiry())
        except (TypeError, ValueError):
            return 60
    return 60


@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(_retry_after_seconds(exc))},
        content=ApiResponse.fail(
            "RATE_LIMIT_EXCEEDED",
            "Слишком много запросов, попробуйте позже",
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


@app.exception_handler(RoleError)
async def role_error_handler(request: Request, exc: RoleError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(exc.code, exc.message).model_dump(),
    )


@app.exception_handler(ChecklistError)
async def checklist_error_handler(
    request: Request,
    exc: ChecklistError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(exc.code, exc.message).model_dump(),
    )


@app.exception_handler(AccessError)
async def access_error_handler(request: Request, exc: AccessError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(exc.code, exc.message).model_dump(),
    )


@app.exception_handler(AdminError)
async def admin_error_handler(request: Request, exc: AdminError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(exc.code, exc.message).model_dump(),
    )


@app.exception_handler(PayrollError)
async def payroll_error_handler(request: Request, exc: PayrollError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(exc.code, exc.message).model_dump(),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Глобальный перехват необработанных исключений.

    Доменные ошибки и RequestValidationError обрабатываются своими хендлерами
    (ожидаемые 4xx) и сюда не попадают. Здесь — программные/инфраструктурные 500:
    логируем через structlog (repr), полный стек шлём в Sentry (если включён),
    клиенту отдаём неизменный конверт {data,error} со статусом 500.
    """
    logger.error(
        "unhandled_exception",
        method=request.method,
        path=request.url.path,
        error=repr(exc),
    )
    if settings.sentry_dsn:
        sentry_sdk.capture_exception(exc)
    return JSONResponse(
        status_code=500,
        content=ApiResponse.fail("ERROR", "Внутренняя ошибка сервера").model_dump(),
    )


app.include_router(v1_router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
