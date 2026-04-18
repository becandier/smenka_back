import uuid

from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.checklist import (
    AssignmentResponse,
    EffectiveTemplateResponse,
    EffectiveTemplatesResponse,
    MemberInfo,
    MemberOverrideRequest,
    RoleAssignmentRequest,
)
from src.app.services import checklist_assignment as assign_service

router = APIRouter(
    prefix="/organizations/{org_id}",
    tags=["checklist-assignments"],
)


def _member_info(member) -> MemberInfo:
    return MemberInfo(
        user_id=str(member.user_id),
        user_name=member.user.name,
        user_email=member.user.email,
    )


@router.put(
    "/checklist-templates/{template_id}/roles",
    summary="Назначить шаблон ролям",
    description="PUT-семантика: передайте полный список ролей. Отсутствующие — удаляются, новые — добавляются.",
)
async def assign_template_to_roles(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    body: RoleAssignmentRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    role_uuids = [uuid.UUID(r) for r in body.role_ids]
    result_ids = await assign_service.assign_template_to_roles(
        session, org_id, template_id, role_uuids, user.id,
    )
    await session.commit()
    return ApiResponse.success({"role_ids": [str(r) for r in result_ids]})


@router.get(
    "/checklist-templates/{template_id}/assignments",
    summary="Кому назначен шаблон",
    description="Роли + сотрудники с личными add/remove. Доступно владельцу и админам.",
)
async def get_template_assignments(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    role_ids, personal_add, personal_remove = await assign_service.get_template_assignments(
        session, org_id, template_id, user.id,
    )
    return ApiResponse.success(
        AssignmentResponse(
            template_id=str(template_id),
            role_ids=[str(r) for r in role_ids],
            personal_add=[_member_info(m) for m in personal_add],
            personal_remove=[_member_info(m) for m in personal_remove],
        ).model_dump(mode="json")
    )


@router.put(
    "/members/{user_id}/checklist-overrides",
    summary="Установить личные переопределения",
    description="PUT-семантика: передайте полный список overrides. Отсутствующие в запросе — удаляются.",
)
async def set_member_overrides(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberOverrideRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    overrides = [(uuid.UUID(o.template_id), o.type) for o in body.overrides]
    parsed = await assign_service.set_member_overrides(
        session, org_id, user_id, overrides, user.id,
    )
    await session.commit()
    return ApiResponse.success(
        {
            "overrides": [
                {"template_id": str(tpl_id), "type": t.value}
                for tpl_id, t in parsed
            ],
        }
    )


@router.get(
    "/members/{user_id}/checklists",
    summary="Эффективные чек-листы сотрудника",
    description="Вычисляет итоговый набор чек-листов по формуле: (шаблоны роли − remove) + add. Доступно владельцу, админам и самому сотруднику.",
)
async def get_member_effective_checklists(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    pairs = await assign_service.get_effective_templates(
        session, org_id, user_id, user.id,
    )
    return ApiResponse.success(
        EffectiveTemplatesResponse(
            items=[
                EffectiveTemplateResponse(
                    id=str(t.id),
                    name=t.name,
                    type=t.type.value,
                    is_required=t.is_required,
                    source=source,
                )
                for t, source in pairs
            ],
        ).model_dump(mode="json")
    )
