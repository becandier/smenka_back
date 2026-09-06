import uuid

from fastapi import APIRouter, Query

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.organization import OrganizationMember
from src.app.schemas.base import ApiResponse
from src.app.schemas.checklist import (
    AssignmentResponse,
    EffectiveTemplateResponse,
    EffectiveTemplatesResponse,
    MemberInfo,
    MemberOverrideRequest,
    RoleAssignmentRequest,
    TemplateLocationAssignmentRequest,
    TemplateScheduleAssignmentRequest,
)
from src.app.services import checklist_assignment as assign_service
from src.app.services import checklist_location as location_service
from src.app.services import checklist_schedule as schedule_service

router = APIRouter(
    prefix="/organizations/{org_id}",
    tags=["checklist-assignments"],
)


def _member_info(member: OrganizationMember) -> MemberInfo:
    return MemberInfo(
        user_id=str(member.user_id),
        user_name=member.user.name,
        # "" вместо null — admin-created учётка без email (admin_created_accounts).
        user_email=member.user.email_display,
    )


@router.put(
    "/checklist-templates/{template_id}/roles",
    summary="Назначить шаблон ролям",
    description="PUT-семантика: передайте полный список ролей. "
    "Отсутствующие — удаляются, новые — добавляются.",
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
        session,
        org_id,
        template_id,
        role_uuids,
        user.id,
    )
    await session.commit()
    return ApiResponse.success({"role_ids": [str(r) for r in result_ids]})


@router.put(
    "/checklist-templates/{template_id}/locations",
    summary="Задать точки шаблона",
    description="PUT-семантика: передайте полный список точек. "
    "Отсутствующие — удаляются, новые — добавляются. Пустой список снимает "
    "все привязки (шаблон снова действует на всех точках).",
)
async def assign_template_to_locations(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    body: TemplateLocationAssignmentRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    location_uuids = [uuid.UUID(loc_id) for loc_id in body.location_ids]
    result_ids = await location_service.set_template_locations(
        session,
        org_id,
        template_id,
        location_uuids,
        user.id,
    )
    await session.commit()
    return ApiResponse.success({"location_ids": [str(loc_id) for loc_id in result_ids]})


@router.put(
    "/checklist-templates/{template_id}/schedules",
    summary="Задать графики шаблона",
    description=(
        "PUT-семантика: передайте полный список графиков. Пустой список снимает все привязки."
    ),
)
async def assign_template_to_schedules(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    body: TemplateScheduleAssignmentRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    schedule_ids = [uuid.UUID(schedule_id) for schedule_id in body.schedule_ids]
    result_ids = await schedule_service.set_template_schedules(
        session, org_id, template_id, schedule_ids, user.id
    )
    await session.commit()
    return ApiResponse.success({"schedule_ids": [str(schedule_id) for schedule_id in result_ids]})


@router.get(
    "/checklist-templates/{template_id}/assignments",
    summary="Кому назначен шаблон",
    description="Роли + сотрудники с личными add/remove + точки. Доступно владельцу и админам.",
)
async def get_template_assignments(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    (
        role_ids,
        personal_add,
        personal_remove,
        location_ids,
        schedule_ids,
    ) = await assign_service.get_template_assignments(
        session,
        org_id,
        template_id,
        user.id,
    )
    return ApiResponse.success(
        AssignmentResponse(
            template_id=str(template_id),
            role_ids=[str(r) for r in role_ids],
            personal_add=[_member_info(m) for m in personal_add],
            personal_remove=[_member_info(m) for m in personal_remove],
            location_ids=[str(loc_id) for loc_id in location_ids],
            schedule_ids=[str(schedule_id) for schedule_id in schedule_ids],
        ).model_dump(mode="json")
    )


@router.put(
    "/members/{user_id}/checklist-overrides",
    summary="Установить личные переопределения",
    description="PUT-семантика: передайте полный список overrides. "
    "Отсутствующие в запросе — удаляются.",
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
        session,
        org_id,
        user_id,
        overrides,
        user.id,
    )
    await session.commit()
    return ApiResponse.success(
        {
            "overrides": [{"template_id": str(tpl_id), "type": t.value} for tpl_id, t in parsed],
        }
    )


@router.get(
    "/members/{user_id}/checklists",
    summary="Эффективные чек-листы сотрудника",
    description="Вычисляет итоговый набор чек-листов по формуле: "
    "(шаблоны роли − remove) + add, включая шаблоны без ролей, но с "
    "привязкой к точкам. Без `work_location_id` — весь набор, без фильтра "
    "по точке. С `work_location_id` — ровно то, что сотрудник получил бы, "
    "открыв смену на этой точке. Доступно владельцу, админам и самому сотруднику.",
)
async def get_member_effective_checklists(
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    work_location_id: uuid.UUID | None = Query(
        default=None,
        description="Точка, для которой применить фильтр matches_location",
    ),
) -> ApiResponse:
    triples = await assign_service.get_effective_templates(
        session,
        org_id,
        user_id,
        user.id,
        work_location_id=work_location_id,
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
                    location_ids=[str(loc_id) for loc_id in location_ids],
                )
                for t, source, location_ids in triples
            ],
        ).model_dump(mode="json")
    )
