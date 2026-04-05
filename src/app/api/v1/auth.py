from fastapi import APIRouter

from src.app.api.deps import SessionDep
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
from src.app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201, summary="Регистрация", description="Создаёт нового пользователя. На указанный email отправляется 4-значный код подтверждения (TTL 15 мин). До подтверждения email вход невозможен.")
async def register(body: RegisterRequest, session: SessionDep) -> ApiResponse:
    user, code = await auth_service.register(
        session,
        body.email,
        body.password,
        body.name,
    )
    await session.commit()
    return ApiResponse.success(
        RegisterResponse(
            user_id=str(user.id),
            message="Код подтверждения отправлен на email",
            verification_code=code,
        ).model_dump()
    )


@router.post("/verify", summary="Подтверждение email", description="Подтверждает email 4-значным кодом. При успехе возвращает access_token и refresh_token (auto-login).")
async def verify(body: VerifyRequest, session: SessionDep) -> ApiResponse:
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


@router.post("/resend-code", summary="Повторная отправка кода", description="Повторно отправляет код подтверждения. Cooldown — 30 сек между запросами.")
async def resend_code(body: ResendCodeRequest, session: SessionDep) -> ApiResponse:
    code = await auth_service.resend_code(session, body.email)
    await session.commit()
    return ApiResponse.success(
        ResendCodeResponse(
            message="Код отправлен повторно",
            verification_code=code,
        ).model_dump()
    )


@router.post("/login", summary="Вход", description="Аутентификация по email и паролю. Возвращает пару access_token + refresh_token. Email должен быть подтверждён.")
async def login(body: LoginRequest, session: SessionDep) -> ApiResponse:
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


@router.post("/refresh", summary="Обновление токенов", description="Ротация токенов: принимает текущий refresh_token, возвращает новую пару. Старый refresh_token отзывается.")
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


@router.post("/logout", summary="Выход", description="Отзывает refresh_token. Access_token продолжает работать до истечения срока (30 мин).")
async def logout(body: LogoutRequest, session: SessionDep) -> ApiResponse:
    await auth_service.logout(session, body.refresh_token)
    await session.commit()
    return ApiResponse.success(MessageResponse(message="Вы вышли из системы").model_dump())
