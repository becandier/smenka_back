from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.user import UserResponse, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", summary="Текущий пользователь", description="Возвращает профиль авторизованного пользователя.")
async def get_me(user: CurrentUserDep) -> ApiResponse:
    return ApiResponse.success(
        UserResponse(
            id=str(user.id),
            email=user.email,
            phone=user.phone,
            name=user.name,
            is_verified=user.is_verified,
            role=user.role.value,
            created_at=user.created_at,
        ).model_dump(mode="json")
    )


@router.patch("/me", summary="Обновить профиль", description="Обновляет имя и/или телефон текущего пользователя. Передавайте только поля, которые нужно изменить.")
async def update_me(
    body: UserUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    if body.name is not None:
        user.name = body.name
    if body.phone is not None:
        user.phone = body.phone
    await session.commit()
    await session.refresh(user)
    return ApiResponse.success(
        UserResponse(
            id=str(user.id),
            email=user.email,
            phone=user.phone,
            name=user.name,
            is_verified=user.is_verified,
            role=user.role.value,
            created_at=user.created_at,
        ).model_dump(mode="json")
    )
