from typing import Literal

from fastapi import APIRouter, Query, Request

from src.app.api.deps import SessionDep
from src.app.core.config import get_settings
from src.app.core.rate_limit import limiter
from src.app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    ResendCodeRequest,
    ResendCodeResponse,
    TokenResponse,
    VerifyRequest,
)
from src.app.schemas.base import ApiResponse
from src.app.schemas.oauth import (
    OAuthAppleRequest,
    OAuthConfigResponse,
    OAuthGoogleRequest,
)
from src.app.services import auth as auth_service
from src.app.services import email as email_service
from src.app.services import oauth as oauth_service

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


@router.post(
    "/register",
    status_code=201,
    summary="Регистрация",
    description=(
        "Создаёт нового пользователя. На указанный email отправляется 4-значный код "
        "подтверждения (TTL 15 мин). До подтверждения email вход невозможен."
    ),
)
@limiter.limit(settings.register_rate_limit)
async def register(request: Request, body: RegisterRequest, session: SessionDep) -> ApiResponse:
    user, code = await auth_service.register(
        session,
        body.email,
        body.password,
        body.name,
    )
    # Commit до отправки письма: при сбое SMTP пользователь уже создан и сможет
    # запросить код повторно (deliver вернёт EMAIL_SEND_FAILED).
    await session.commit()
    response_code = await email_service.deliver_verification_code(body.email, code)
    return ApiResponse.success(
        RegisterResponse(
            user_id=str(user.id),
            message="Код подтверждения отправлен на email",
            verification_code=response_code,
        ).model_dump()
    )


@router.post(
    "/verify",
    summary="Подтверждение email",
    description=(
        "Подтверждает email 4-значным кодом. При успехе возвращает access_token и "
        "refresh_token (auto-login)."
    ),
)
@limiter.limit(settings.verify_rate_limit)
async def verify(request: Request, body: VerifyRequest, session: SessionDep) -> ApiResponse:
    access_token, refresh_token = await auth_service.verify_email(
        session,
        body.email,
        body.code,
    )
    await session.commit()
    return ApiResponse.success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        ).model_dump()
    )


@router.post(
    "/resend-code",
    summary="Повторная отправка кода",
    description="Повторно отправляет код подтверждения. Cooldown — 30 сек между запросами.",
)
@limiter.limit(settings.resend_rate_limit)
async def resend_code(
    request: Request, body: ResendCodeRequest, session: SessionDep
) -> ApiResponse:
    code = await auth_service.resend_code(session, body.email)
    await session.commit()
    response_code = await email_service.deliver_verification_code(body.email, code)
    return ApiResponse.success(
        ResendCodeResponse(
            message="Код отправлен повторно",
            verification_code=response_code,
        ).model_dump()
    )


@router.post(
    "/login",
    summary="Вход",
    description=(
        "Аутентификация по email и паролю. Возвращает пару access_token + refresh_token. "
        "Email должен быть подтверждён."
    ),
)
@limiter.limit(settings.login_rate_limit)
async def login(request: Request, body: LoginRequest, session: SessionDep) -> ApiResponse:
    access_token, refresh_token = await auth_service.login(
        session,
        body.email,
        body.password,
    )
    await session.commit()
    return ApiResponse.success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        ).model_dump()
    )


@router.post(
    "/refresh",
    summary="Обновление токенов",
    description=(
        "Ротация токенов: принимает текущий refresh_token, возвращает новую пару. "
        "Старый refresh_token отзывается."
    ),
)
async def refresh(body: RefreshRequest, session: SessionDep) -> ApiResponse:
    access_token, refresh_token = await auth_service.refresh_tokens(
        session,
        body.refresh_token,
    )
    await session.commit()
    return ApiResponse.success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        ).model_dump()
    )


@router.post(
    "/logout",
    summary="Выход",
    description=(
        "Отзывает refresh_token. Access_token продолжает работать до истечения срока (30 мин)."
    ),
)
async def logout(body: LogoutRequest, session: SessionDep) -> ApiResponse:
    await auth_service.logout(session, body.refresh_token)
    await session.commit()
    return ApiResponse.success(MessageResponse(message="Вы вышли из системы").model_dump())


@router.post(
    "/oauth/google",
    summary="Вход через Google",
    description=(
        "Проверяет Google id_token, автолинкует к существующему пользователю по email "
        "(регистронезависимо) или регистрирует нового. Возвращает пару access/refresh-токенов."
    ),
)
@limiter.limit(settings.oauth_login_rate_limit)
async def oauth_google(
    request: Request, body: OAuthGoogleRequest, session: SessionDep
) -> ApiResponse:
    access_token, refresh_token = await oauth_service.authenticate_google(
        session,
        body.id_token,
        body.client_type,
    )
    await session.commit()
    return ApiResponse.success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        ).model_dump()
    )


@router.post(
    "/oauth/apple",
    summary="Вход через Apple",
    description=(
        "Проверяет Apple identity_token, автолинкует к существующему пользователю по email "
        "(регистронезависимо) или регистрирует нового. email/name присылаются клиентом только "
        "при первой авторизации. Возвращает пару access/refresh-токенов."
    ),
)
@limiter.limit(settings.oauth_login_rate_limit)
async def oauth_apple(
    request: Request, body: OAuthAppleRequest, session: SessionDep
) -> ApiResponse:
    access_token, refresh_token = await oauth_service.authenticate_apple(
        session,
        body.identity_token,
        body.client_type,
        body.email,
        body.name,
    )
    await session.commit()
    return ApiResponse.success(
        TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        ).model_dump()
    )


@router.get(
    "/oauth/config",
    summary="Публичный конфиг OAuth-провайдеров",
    description=(
        "Отдаёт client_id/enabled для Google и Apple для запрошенного client_type — фронты "
        "используют, чтобы решить, показывать ли кнопку входа и с каким Client ID "
        "инициализировать SDK. null для невключённого провайдера."
    ),
)
async def oauth_config(
    session: SessionDep,
    client_type: Literal["web", "ios", "android"] = Query(
        description="Платформа клиента, запрашивающего конфиг"
    ),
) -> ApiResponse:
    data = await oauth_service.get_oauth_config(session, client_type)
    return ApiResponse.success(OAuthConfigResponse(**data).model_dump())
