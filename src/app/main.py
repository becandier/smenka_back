from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.app.api.v1.router import router as v1_router
from src.app.core.config import get_settings
from src.app.schemas.base import ApiResponse
from src.app.services.auth import AuthError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


settings = get_settings()

app = FastAPI(
    title="Smenka API",
    version="0.1.0",
    description="API для учёта рабочего времени",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
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


app.include_router(v1_router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
