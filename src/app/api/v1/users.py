from typing import Annotated

from fastapi import APIRouter, Path, Query

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.user import UserResponse, UserUpdate
from src.app.schemas.user_preference import (
    PREFERENCE_KEY_MAX_LENGTH,
    PREFERENCE_KEY_PATTERN,
    PREFERENCE_PREFIX_PATTERN,
    PreferenceDeleteResponse,
    PreferenceResponse,
    PreferencesResponse,
    PreferenceUpdate,
)
from src.app.services import user_preference as preference_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/me",
    summary="Текущий пользователь",
    description="Возвращает профиль авторизованного пользователя.",
)
async def get_me(user: CurrentUserDep) -> ApiResponse:
    return ApiResponse.success(
        UserResponse(
            id=str(user.id),
            email=user.email,
            login=user.login,
            phone=user.phone,
            name=user.name,
            is_verified=user.is_verified,
            role=user.role.value,
            created_at=user.created_at,
        ).model_dump(mode="json")
    )


@router.patch(
    "/me",
    summary="Обновить профиль",
    description=(
        "Обновляет имя и/или телефон текущего пользователя. "
        "Передавайте только поля, которые нужно изменить."
    ),
)
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
            login=user.login,
            phone=user.phone,
            name=user.name,
            is_verified=user.is_verified,
            role=user.role.value,
            created_at=user.created_at,
        ).model_dump(mode="json")
    )


# --- Личные настройки (table_column_settings) -----------------------------------
# Доступ `authenticated`: только свои настройки по user_id из токена. Проверки
# подписки, прав организации и профилей доступа намеренно не применяются.

PreferenceKey = Annotated[
    str,
    Path(
        max_length=PREFERENCE_KEY_MAX_LENGTH,
        pattern=PREFERENCE_KEY_PATTERN,
        description="Ключ настройки, например `admin.table.org-shifts`",
    ),
]


@router.get(
    "/me/preferences",
    response_model=ApiResponse[PreferencesResponse],
    summary="Личные настройки",
    description=(
        "Все настройки текущего пользователя; с `prefix` — только ключи, "
        'начинающиеся с него. Пустой результат — `{"preferences": {}}`.'
    ),
)
async def list_my_preferences(
    user: CurrentUserDep,
    session: SessionDep,
    prefix: Annotated[
        str | None,
        Query(
            pattern=PREFERENCE_PREFIX_PATTERN,
            description="Начало ключа, 1..120 символов из алфавита ключа",
        ),
    ] = None,
) -> ApiResponse:
    preferences = await preference_service.list_preferences(session, user.id, prefix)
    return ApiResponse.success(PreferencesResponse(preferences=preferences).model_dump())


@router.put(
    "/me/preferences/{key}",
    response_model=ApiResponse[PreferenceResponse],
    summary="Сохранить личную настройку",
    description=(
        "Создаёт или целиком заменяет значение по ключу. 409 "
        "`PREFERENCES_LIMIT_REACHED`, если у пользователя уже 200 настроек, "
        "а ключ новый; перезапись существующего проходит всегда."
    ),
)
async def put_my_preference(
    key: PreferenceKey,
    body: PreferenceUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    preference = await preference_service.put_preference(session, user.id, key, body.value)
    return ApiResponse.success(
        PreferenceResponse(
            key=preference.key,
            value=preference.value,
            updated_at=preference.updated_at,
        ).model_dump(mode="json")
    )


@router.delete(
    "/me/preferences/{key}",
    response_model=ApiResponse[PreferenceDeleteResponse],
    summary="Удалить личную настройку",
    description='Идемпотентно: отсутствующий ключ — `{"deleted": 0}` с HTTP 200.',
)
async def delete_my_preference(
    key: PreferenceKey,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    deleted = await preference_service.delete_preference(session, user.id, key)
    return ApiResponse.success(PreferenceDeleteResponse(deleted=deleted).model_dump())
