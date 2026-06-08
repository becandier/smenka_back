import uuid

from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.checklist_override import (
    MemberOverrideItemResponse,
    MemberOverrideListResponse,
    PersonalOverrideRequest,
    PersonalOverrideResponse,
)
from src.app.services import checklist_override as override_service

router = APIRouter(
    prefix="/organizations/{org_id}",
    tags=["checklist-overrides"],
)


@router.get(
    "/members/{user_id}/checklist-overrides",
    summary="Все личные overrides сотрудника",
    description="Возвращает все личные переопределения сотрудника включая overrides "
    "архивных шаблонов. Доступно владельцу, админам и самому сотруднику.",
)
async def list_member_overrides(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    pairs = await override_service.list_member_overrides(
        session, org_id, user_id, user.id,
    )
    return ApiResponse.success(
        MemberOverrideListResponse(
            items=[
                MemberOverrideItemResponse(
                    template_id=str(ov.template_id),
                    template_name=tpl.name,
                    template_type=tpl.type.value,
                    type=ov.override_type.value,
                )
                for ov, tpl in pairs
            ],
        ).model_dump(mode="json")
    )


@router.put(
    "/checklist-templates/{template_id}/personal/{user_id}",
    summary="Установить личный override для (шаблон, сотрудник)",
    description="Upsert-семантика: создаёт или обновляет override для пары (template, user). "
    "На архивных шаблонах запрещено.",
)
async def upsert_personal_override(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user_id: uuid.UUID,
    body: PersonalOverrideRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    override_type = await override_service.upsert_override(
        session, org_id, template_id, user_id, body.type, user.id,
    )
    await session.commit()
    return ApiResponse.success(
        PersonalOverrideResponse(
            template_id=str(template_id),
            user_id=str(user_id),
            type=override_type.value,
        ).model_dump(mode="json")
    )


@router.delete(
    "/checklist-templates/{template_id}/personal/{user_id}",
    summary="Снять личный override",
    description="Идемпотентно удаляет override для пары (template, user). Возвращает 200 "
    "даже если override отсутствует.",
)
async def delete_personal_override(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await override_service.delete_override(
        session, org_id, template_id, user_id, user.id,
    )
    await session.commit()
    return ApiResponse.success(None)
