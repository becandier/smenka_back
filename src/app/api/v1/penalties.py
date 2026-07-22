import uuid
from datetime import datetime as dt_datetime

from fastapi import APIRouter, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.organization import OrganizationMember
from src.app.models.penalty import OrganizationPenaltyTemplate, Penalty
from src.app.models.user import User
from src.app.schemas.base import ApiResponse
from src.app.schemas.penalty import (
    MyPenaltyListResponse,
    MyPenaltyResponse,
    PenaltyCreate,
    PenaltyDeletedResponse,
    PenaltyListResponse,
    PenaltyResponse,
    PenaltyTemplateCreate,
    PenaltyTemplateDeletedResponse,
    PenaltyTemplateListResponse,
    PenaltyTemplateResponse,
    PenaltyTemplateUpdate,
    PenaltyUpdate,
)
from src.app.services import penalty as penalty_service

router = APIRouter(prefix="/organizations/{org_id}", tags=["penalties"])


def _template_to_response(template: OrganizationPenaltyTemplate) -> PenaltyTemplateResponse:
    return PenaltyTemplateResponse(
        id=str(template.id),
        reason=template.reason,
        amount_minor=template.amount_minor,
        currency=template.currency,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


async def _build_penalty_payloads(
    session: AsyncSession,
    penalties: list[Penalty],
) -> list[PenaltyResponse]:
    """Обогатить штрафы именем/UUID сотрудника (member → user) без N+1."""
    if not penalties:
        return []
    member_ids = {p.member_id for p in penalties}
    members_result = await session.execute(
        select(
            OrganizationMember.id,
            OrganizationMember.user_id,
            OrganizationMember.display_name,
        ).where(OrganizationMember.id.in_(member_ids))
    )
    members_rows = members_result.all()
    user_by_member = {row.id: row.user_id for row in members_rows}
    display_name_by_member = {row.id: row.display_name for row in members_rows}
    user_ids = set(user_by_member.values())
    users_result = await session.execute(select(User.id, User.name).where(User.id.in_(user_ids)))
    name_by_user = dict(users_result.tuples().all())

    payloads: list[PenaltyResponse] = []
    for p in penalties:
        uid = user_by_member.get(p.member_id)
        user_name = name_by_user.get(uid, "Unknown") if uid is not None else "Unknown"
        payloads.append(
            PenaltyResponse(
                id=str(p.id),
                member_id=str(p.member_id),
                user_id=str(uid) if uid is not None else "",
                user_name=user_name,
                display_name=display_name_by_member.get(p.member_id),
                template_id=str(p.template_id) if p.template_id is not None else None,
                reason=p.reason,
                amount_minor=p.amount_minor,
                currency=p.currency,
                shift_id=str(p.shift_id) if p.shift_id is not None else None,
                occurred_at=p.occurred_at,
                comment=p.comment,
                created_by_user_id=str(p.created_by_user_id),
                created_at=p.created_at,
                updated_at=p.updated_at,
            )
        )
    return payloads


def _my_penalty_to_response(penalty: Penalty) -> MyPenaltyResponse:
    return MyPenaltyResponse(
        id=str(penalty.id),
        reason=penalty.reason,
        amount_minor=penalty.amount_minor,
        currency=penalty.currency,
        shift_id=str(penalty.shift_id) if penalty.shift_id is not None else None,
        occurred_at=penalty.occurred_at,
        comment=penalty.comment,
        created_at=penalty.created_at,
    )


# --- Шаблоны штрафов ---------------------------------------------------------
@router.post(
    "/penalty-templates",
    status_code=201,
    summary="Создать шаблон штрафа",
    description="Справочный шаблон («Опоздание — 500 ₽») для быстрого выбора. Owner/admin.",
)
async def create_penalty_template(
    org_id: uuid.UUID,
    body: PenaltyTemplateCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    template = await penalty_service.create_template(
        session,
        org_id,
        user.id,
        reason=body.reason,
        amount_minor=body.amount_minor,
        currency=body.currency,
    )
    await session.commit()
    return ApiResponse.success(_template_to_response(template).model_dump(mode="json"))


@router.get(
    "/penalty-templates",
    summary="Список шаблонов штрафов",
    description="Активные шаблоны организации, created_at DESC. Owner/admin (и выбор в мобилке).",
)
async def list_penalty_templates(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    templates = await penalty_service.list_templates(session, org_id, user.id)
    return ApiResponse.success(
        PenaltyTemplateListResponse(
            items=[_template_to_response(t) for t in templates],
        ).model_dump(mode="json")
    )


@router.patch(
    "/penalty-templates/{template_id}",
    summary="Исправить шаблон штрафа",
    description="Правка шаблона не меняет ранее выданные штрафы (у них свой снимок). Owner/admin.",
)
async def update_penalty_template(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    body: PenaltyTemplateUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    template = await penalty_service.update_template(
        session,
        org_id,
        template_id,
        user.id,
        body.model_dump(exclude_unset=True),
    )
    await session.commit()
    return ApiResponse.success(_template_to_response(template).model_dump(mode="json"))


@router.delete(
    "/penalty-templates/{template_id}",
    summary="Удалить шаблон штрафа (soft-delete)",
    description="Шаблон уходит из списка выбора; выданные штрафы сохраняют снимок. Owner/admin.",
)
async def delete_penalty_template(
    org_id: uuid.UUID,
    template_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await penalty_service.delete_template(session, org_id, template_id, user.id)
    await session.commit()
    return ApiResponse.success(PenaltyTemplateDeletedResponse(deleted=True).model_dump())


# --- Штрафы ------------------------------------------------------------------
@router.post(
    "/penalties",
    status_code=201,
    summary="Назначить штраф",
    description=(
        "Назначает сотруднику штраф (из шаблона или кастомный). reason/amount — снимок. "
        "occurred_at: при привязке к смене по умолчанию = started_at, иначе обязателен. "
        "Owner/admin. Уменьшает «к выплате» в payroll."
    ),
)
async def create_penalty(
    org_id: uuid.UUID,
    body: PenaltyCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    penalty = await penalty_service.create_penalty(
        session,
        org_id,
        user.id,
        member_id=body.member_id,
        template_id=body.template_id,
        reason=body.reason,
        amount_minor=body.amount_minor,
        currency=body.currency,
        shift_id=body.shift_id,
        occurred_at=body.occurred_at,
        comment=body.comment,
    )
    await session.commit()
    payloads = await _build_penalty_payloads(session, [penalty])
    return ApiResponse.success(payloads[0].model_dump(mode="json"))


@router.get(
    "/penalties",
    summary="Список штрафов организации",
    description=(
        "Активные штрафы под фильтром (member_id/shift_id/период), "
        "occurred_at DESC. Owner/admin."
    ),
)
async def list_penalties(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    member_id: uuid.UUID | None = Query(None, description="Фильтр по сотруднику"),
    shift_id: uuid.UUID | None = Query(None, description="Фильтр по смене"),
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница по occurred_at, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница по occurred_at, включительно (UTC)"
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    penalties, total = await penalty_service.list_penalties(
        session,
        org_id,
        user.id,
        member_id=member_id,
        shift_id=shift_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    items = await _build_penalty_payloads(session, penalties)
    return ApiResponse.success(
        PenaltyListResponse(
            items=items,
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )


@router.get(
    "/penalties/{penalty_id}",
    summary="Деталь штрафа",
    description="Активный штраф организации. Снятый штраф невидим → 404. Owner/admin.",
)
async def get_penalty(
    org_id: uuid.UUID,
    penalty_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    penalty = await penalty_service.get_penalty(session, org_id, penalty_id, user.id)
    payloads = await _build_penalty_payloads(session, [penalty])
    return ApiResponse.success(payloads[0].model_dump(mode="json"))


@router.patch(
    "/penalties/{penalty_id}",
    summary="Исправить штраф",
    description=(
        "Правка записи штрафа. shift_id можно переустановить/обнулить. "
        "member_id неизменен. Owner/admin."
    ),
)
async def update_penalty(
    org_id: uuid.UUID,
    penalty_id: uuid.UUID,
    body: PenaltyUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    penalty = await penalty_service.update_penalty(
        session,
        org_id,
        penalty_id,
        user.id,
        body.model_dump(exclude_unset=True),
    )
    await session.commit()
    payloads = await _build_penalty_payloads(session, [penalty])
    return ApiResponse.success(payloads[0].model_dump(mode="json"))


@router.delete(
    "/penalties/{penalty_id}",
    summary="Снять штраф (soft-delete)",
    description="Любой owner/admin (не только автор). Штраф перестаёт вычитаться из payroll. ",
)
async def delete_penalty(
    org_id: uuid.UUID,
    penalty_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await penalty_service.delete_penalty(session, org_id, penalty_id, user.id)
    await session.commit()
    return ApiResponse.success(PenaltyDeletedResponse(deleted=True).model_dump())


# --- Сотрудник ---------------------------------------------------------------
@router.get(
    "/my-penalties",
    summary="Мои штрафы",
    description="Свои активные штрафы за период. Только участник (employee/admin); owner — 403.",
)
async def my_penalties(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница по occurred_at, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница по occurred_at, включительно (UTC)"
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    penalties, total = await penalty_service.list_my_penalties(
        session,
        org_id,
        user.id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return ApiResponse.success(
        MyPenaltyListResponse(
            items=[_my_penalty_to_response(p) for p in penalties],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )
