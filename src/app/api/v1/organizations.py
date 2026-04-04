import uuid
from datetime import datetime as dt_datetime

from fastapi import APIRouter, Query

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.organization import (
    InviteCodeResponse,
    JoinResponse,
    MemberListResponse,
    MemberResponse,
    OrganizationCreate,
    OrganizationListResponse,
    OrganizationResponse,
    OrganizationUpdate,
)
from src.app.schemas.organization_settings import (
    OrganizationSettingsResponse,
    OrganizationSettingsUpdate,
)
from src.app.schemas.shift import ShiftListResponse
from src.app.services import organization as org_service
from src.app.services import organization_settings as settings_service
from src.app.services import shift as shift_service
from src.app.api.v1.shifts import _shift_to_response

router = APIRouter(prefix="/organizations", tags=["organizations"])


def _org_to_response(org) -> dict:
    return OrganizationResponse(
        id=str(org.id),
        name=org.name,
        owner_id=str(org.owner_id),
        invite_code=org.invite_code,
        is_deleted=org.is_deleted,
        created_at=org.created_at,
    ).model_dump(mode="json")


def _member_to_response(member) -> dict:
    return MemberResponse(
        id=str(member.id),
        organization_id=str(member.organization_id),
        user_id=str(member.user_id),
        user_name=member.user.name,
        user_email=member.user.email,
        role=member.role.value,
        joined_at=member.joined_at,
    ).model_dump(mode="json")


@router.post("", status_code=201)
async def create_organization(
    body: OrganizationCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    org = await org_service.create_organization(session, body.name, user.id)
    await session.commit()
    return ApiResponse.success(_org_to_response(org))


@router.get("")
async def list_organizations(
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    orgs = await org_service.get_user_organizations(session, user.id)
    return ApiResponse.success(
        OrganizationListResponse(
            items=[_org_to_response(o) for o in orgs],
        ).model_dump(mode="json")
    )


@router.get("/{org_id}")
async def get_organization(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    org = await org_service.get_organization(session, org_id)
    return ApiResponse.success(_org_to_response(org))


@router.patch("/{org_id}")
async def update_organization(
    org_id: uuid.UUID,
    body: OrganizationUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    org = await org_service.update_organization(session, org_id, user.id, body.name)
    await session.commit()
    return ApiResponse.success(_org_to_response(org))


@router.delete("/{org_id}", status_code=200)
async def delete_organization(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await org_service.delete_organization(session, org_id, user.id)
    await session.commit()
    return ApiResponse.success({"message": "Организация удалена"})


@router.post("/{org_id}/rotate-invite", status_code=200)
async def rotate_invite_code(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    new_code = await org_service.rotate_invite_code(session, org_id, user.id)
    await session.commit()
    return ApiResponse.success(
        InviteCodeResponse(invite_code=new_code).model_dump()
    )


@router.post("/join/{invite_code}", status_code=201)
async def join_organization(
    invite_code: str,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    org, member = await org_service.join_by_invite(session, invite_code, user.id)
    await session.commit()
    return ApiResponse.success(
        JoinResponse(
            organization_id=str(org.id),
            organization_name=org.name,
            role=member.role.value,
        ).model_dump()
    )


@router.get("/{org_id}/members")
async def list_members(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    members = await org_service.get_members(session, org_id, user.id)
    return ApiResponse.success(
        MemberListResponse(
            items=[_member_to_response(m) for m in members],
        ).model_dump(mode="json")
    )


@router.delete("/{org_id}/members/{member_user_id}")
async def remove_member(
    org_id: uuid.UUID,
    member_user_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await org_service.remove_member(session, org_id, member_user_id, user.id)
    await session.commit()
    return ApiResponse.success({"message": "Участник удалён"})


def _settings_to_response(s) -> dict:
    return OrganizationSettingsResponse(
        organization_id=str(s.organization_id),
        geo_check_enabled=s.geo_check_enabled,
        auto_finish_hours=s.auto_finish_hours,
        max_pause_minutes=s.max_pause_minutes,
        max_pauses_per_shift=s.max_pauses_per_shift,
    ).model_dump()


@router.get("/{org_id}/settings")
async def get_org_settings(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    settings = await settings_service.get_settings(session, org_id, user.id)
    return ApiResponse.success(_settings_to_response(settings))


@router.patch("/{org_id}/settings")
async def update_org_settings(
    org_id: uuid.UUID,
    body: OrganizationSettingsUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    fields = body.model_dump(exclude_unset=True)
    settings = await settings_service.update_settings(
        session, org_id, user.id, **fields,
    )
    await session.commit()
    return ApiResponse.success(_settings_to_response(settings))


@router.get("/{org_id}/shifts")
async def list_org_shifts(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    user_id: uuid.UUID | None = Query(None, description="Filter by employee"),
    status: str | None = Query(None),
    date_from: dt_datetime | None = Query(None),
    date_to: dt_datetime | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    # Only owner or admin can view org shifts
    from src.app.services.work_location import _check_admin_or_owner

    org = await org_service.get_organization(session, org_id)
    await _check_admin_or_owner(session, org, user.id)

    from src.app.models.shift import ShiftStatus as ShiftStatusEnum
    from src.app.services.shift import ShiftError

    status_enum = None
    if status is not None:
        try:
            status_enum = ShiftStatusEnum(status)
        except ValueError:
            raise ShiftError(
                "INVALID_STATUS",
                f"Статус должен быть: {', '.join(s.value for s in ShiftStatusEnum)}",
                400,
            )

    shifts, total = await shift_service.get_org_shifts(
        session, org_id,
        user_id=user_id,
        status=status_enum,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return ApiResponse.success(
        ShiftListResponse(
            items=[_shift_to_response(s) for s in shifts],
            total=total,
            limit=limit,
            offset=offset,
        ).model_dump(mode="json")
    )
