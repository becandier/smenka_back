from pydantic import BaseModel, Field


class PersonalOverrideRequest(BaseModel):
    type: str = Field(description="add — добавить поверх роли, remove — исключить из роли")


class PersonalOverrideResponse(BaseModel):
    template_id: str
    user_id: str
    type: str


class MemberOverrideItemResponse(BaseModel):
    template_id: str
    template_name: str
    template_type: str = Field(description="shift_start или shift_end")
    type: str = Field(description="add или remove")


class MemberOverrideListResponse(BaseModel):
    items: list[MemberOverrideItemResponse]
