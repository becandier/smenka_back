import uuid

from fastapi import APIRouter, Query

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.notification import Notification
from src.app.schemas.base import ApiResponse
from src.app.schemas.notification import (
    NotificationListResponse,
    NotificationOut,
    ReadAllResponse,
    UnreadCountResponse,
)
from src.app.services import notification as notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


def _notification_to_response(notification: Notification) -> NotificationOut:
    return NotificationOut(
        id=str(notification.id),
        type=notification.type,
        title=notification.title,
        body=notification.body,
        payload=notification.payload,
        is_read=notification.is_read,
        created_at=notification.created_at,
    )


@router.get(
    "",
    summary="Лента уведомлений",
    description="Свои уведомления, created_at DESC. Опционально только непрочитанные.",
)
async def list_notifications(
    user: CurrentUserDep,
    session: SessionDep,
    limit: int = Query(20, ge=1, le=50),
    offset: int = Query(0, ge=0),
    unread: bool | None = Query(None, description="true — только непрочитанные"),
) -> ApiResponse:
    items, total = await notification_service.list_notifications(
        session,
        user.id,
        limit=limit,
        offset=offset,
        unread=unread,
    )
    return ApiResponse.success(
        NotificationListResponse(
            items=[_notification_to_response(n) for n in items],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/unread-count",
    summary="Счётчик непрочитанных",
    description="Число непрочитанных уведомлений текущего пользователя (для бейджа колокольчика).",
)
async def unread_count(
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    count = await notification_service.count_unread(session, user.id)
    return ApiResponse.success(UnreadCountResponse(count=count).model_dump())


@router.post(
    "/{notification_id}/read",
    summary="Пометить прочитанным",
    description="Идемпотентно: повторный вызов на уже прочитанном — no-op, 200.",
)
async def mark_read(
    notification_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    notification = await notification_service.mark_read(session, user.id, notification_id)
    await session.commit()
    return ApiResponse.success(_notification_to_response(notification).model_dump(mode="json"))


@router.post(
    "/read-all",
    summary="Пометить все прочитанными",
    description="Ставит read_at=now() только непрочитанным уведомлениям текущего пользователя.",
)
async def mark_all_read(
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    updated = await notification_service.mark_all_read(session, user.id)
    await session.commit()
    return ApiResponse.success(ReadAllResponse(updated=updated).model_dump())
