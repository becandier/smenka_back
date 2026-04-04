from datetime import datetime

from pydantic import BaseModel, Field


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class OrganizationUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class OrganizationResponse(BaseModel):
    id: str
    name: str
    owner_id: str
    invite_code: str
    is_deleted: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class OrganizationListResponse(BaseModel):
    items: list[OrganizationResponse]


class MemberResponse(BaseModel):
    id: str
    organization_id: str
    user_id: str
    user_name: str
    user_email: str
    role: str
    joined_at: datetime

    model_config = {"from_attributes": True}


class MemberListResponse(BaseModel):
    items: list[MemberResponse]


class JoinResponse(BaseModel):
    organization_id: str
    organization_name: str
    role: str


class InviteCodeResponse(BaseModel):
    invite_code: str
