import uuid

from fastapi import APIRouter

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
from src.app.services import organization as org_service

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
