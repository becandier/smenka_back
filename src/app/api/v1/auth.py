from fastapi import APIRouter
from fastapi.responses import JSONResponse

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
from src.app.services.auth import AuthError

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201)
async def register(body: RegisterRequest, session: SessionDep) -> ApiResponse:
    try:
        user, code = await auth_service.register(
            session, body.email, body.password, body.name,
        )
        await session.commit()
        return ApiResponse.success(
            RegisterResponse(
                user_id=str(user.id),
                message="Код подтверждения отправлен на email",
                verification_code=code,
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/verify")
async def verify(body: VerifyRequest, session: SessionDep) -> ApiResponse:
    try:
        access_token, refresh_token = await auth_service.verify_email(
            session, body.email, body.code,
        )
        await session.commit()
        return ApiResponse.success(
            TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/resend-code")
async def resend_code(body: ResendCodeRequest, session: SessionDep) -> ApiResponse:
    try:
        code = await auth_service.resend_code(session, body.email)
        await session.commit()
        return ApiResponse.success(
            ResendCodeResponse(
                message="Код отправлен повторно",
                verification_code=code,
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/login")
async def login(body: LoginRequest, session: SessionDep) -> ApiResponse:
    try:
        access_token, refresh_token = await auth_service.login(
            session, body.email, body.password,
        )
        await session.commit()
        return ApiResponse.success(
            TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/refresh")
async def refresh(body: RefreshRequest, session: SessionDep) -> ApiResponse:
    try:
        access_token, refresh_token = await auth_service.refresh_tokens(
            session, body.refresh_token,
        )
        await session.commit()
        return ApiResponse.success(
            TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/logout")
async def logout(body: LogoutRequest, session: SessionDep) -> ApiResponse:
    try:
        await auth_service.logout(session, body.refresh_token)
        await session.commit()
        return ApiResponse.success(
            MessageResponse(message="Вы вышли из системы").model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )
