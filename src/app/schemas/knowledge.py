"""Pydantic-схемы базы знаний: BLOCK SCHEMA, запросы и типизированные ответы.

Контент страницы валидируется на уровне body (discriminated union по `type`):
неизвестный `type` / битый `span` / не-YouTube видео → 422 (RequestValidationError).
Доменные проверки (`content` для раздела, валидность `file_id`) — в сервисе.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.app.models.knowledge import (
    KnowledgeAccessEffect,
    KnowledgeNodeKind,
    KnowledgeSubjectType,
)

# --- BLOCK SCHEMA (schema_version = 1) ---------------------------------------
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_youtube_id(url: str) -> str | None:
    """Извлекает 11-символьный YouTube video_id из ссылки или None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid if _YT_ID.match(vid) else None
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            vid = values[0] if values else ""
            return vid if _YT_ID.match(vid) else None
        for prefix in ("/embed/", "/shorts/", "/v/"):
            if parsed.path.startswith(prefix):
                vid = parsed.path[len(prefix) :].split("/")[0]
                return vid if _YT_ID.match(vid) else None
    return None


class Span(BaseModel):
    """Inline rich-text фрагмент. Любые лишние поля игнорируются (read→write round-trip)."""

    model_config = ConfigDict(extra="ignore")

    text: str
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None
    code: bool | None = None
    link: str | None = None


class _BlockBase(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)


class HeadingBlock(_BlockBase):
    type: Literal["heading"]
    level: Literal[1, 2, 3]
    rich: list[Span]


class ParagraphBlock(_BlockBase):
    type: Literal["paragraph"]
    rich: list[Span]


class BulletedListBlock(_BlockBase):
    type: Literal["bulleted_list"]
    items: list[list[Span]]


class NumberedListBlock(_BlockBase):
    type: Literal["numbered_list"]
    items: list[list[Span]]


class QuoteBlock(_BlockBase):
    type: Literal["quote"]
    rich: list[Span]


class CalloutBlock(_BlockBase):
    type: Literal["callout"]
    emoji: str | None = None
    rich: list[Span]


class DividerBlock(_BlockBase):
    type: Literal["divider"]


class ImageBlock(_BlockBase):
    type: Literal["image"]
    file_id: uuid.UUID
    caption: str | None = None


class FileBlock(_BlockBase):
    type: Literal["file"]
    file_id: uuid.UUID
    filename: str
    size_bytes: int


class VideoBlock(_BlockBase):
    type: Literal["video"]
    provider: Literal["youtube"]
    url: str
    video_id: str | None = None

    @model_validator(mode="after")
    def _normalize_youtube(self) -> "VideoBlock":
        vid = extract_youtube_id(self.url)
        if vid is None:
            raise ValueError("Невалидная ссылка YouTube")
        self.video_id = vid
        return self


class TableBlock(_BlockBase):
    type: Literal["table"]
    rows: list[list[list[Span]]]


Block = Annotated[
    HeadingBlock
    | ParagraphBlock
    | BulletedListBlock
    | NumberedListBlock
    | QuoteBlock
    | CalloutBlock
    | DividerBlock
    | ImageBlock
    | FileBlock
    | VideoBlock
    | TableBlock,
    Field(discriminator="type"),
]


# --- Узлы: запросы -----------------------------------------------------------
class NodeCreate(BaseModel):
    parent_id: uuid.UUID | None = Field(
        default=None, description="Родитель в этой org или null (корень)"
    )
    kind: KnowledgeNodeKind = Field(description="section | page")
    title: str = Field(min_length=1, max_length=255)
    icon: str | None = Field(default=None, max_length=64)
    position: int | None = Field(
        default=None, ge=0, description="По умолчанию — в конец сиблингов"
    )


class NodeUpdate(BaseModel):
    """Частичное обновление. Различение «не передано / передано» — по model_fields_set."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    icon: str | None = Field(default=None, max_length=64)
    all_members: bool | None = None
    content: list[Block] | None = Field(
        default=None, description="Блоки страницы (только kind=page)"
    )
    parent_id: uuid.UUID | None = Field(
        default=None, description="Перемещение узла; null = в корень"
    )
    position: int | None = Field(default=None, ge=0)


class ReorderRequest(BaseModel):
    parent_id: uuid.UUID | None = Field(
        default=None, description="Родитель сиблингов; null = корень"
    )
    ordered_ids: list[uuid.UUID] = Field(description="Полный новый порядок детей этого родителя")


# --- Узлы: ответы ------------------------------------------------------------
class NodeResponse(BaseModel):
    id: str
    parent_id: str | None = None
    kind: str
    title: str
    icon: str | None = None
    position: int
    all_members: bool
    content: list[dict[str, Any]] | None = Field(
        default=None, description="[] для page, null для section"
    )
    created_at: datetime
    updated_at: datetime


class NodeTreeItem(BaseModel):
    id: str
    kind: str
    title: str
    icon: str | None = None
    position: int
    all_members: bool | None = Field(
        default=None, description="Только для owner/admin/super_admin; у employee опускается"
    )
    children: list["NodeTreeItem"] = Field(default_factory=list)


class NodeTreeResponse(BaseModel):
    items: list[NodeTreeItem]


class Breadcrumb(BaseModel):
    id: str
    title: str


class NodeDetailResponse(BaseModel):
    id: str
    parent_id: str | None = None
    kind: str
    title: str
    icon: str | None = None
    position: int
    all_members: bool
    created_at: datetime
    updated_at: datetime
    breadcrumbs: list[Breadcrumb]
    content: list[dict[str, Any]] | None = Field(
        default=None, description="Обогащённые блоки (image/file +url/+url_expires_at) или null"
    )


# --- ACL ---------------------------------------------------------------------
class AccessRuleInput(BaseModel):
    subject_type: KnowledgeSubjectType
    role_id: uuid.UUID | None = None
    member_user_id: uuid.UUID | None = None
    effect: KnowledgeAccessEffect

    @model_validator(mode="after")
    def _check_subject(self) -> "AccessRuleInput":
        if self.subject_type == KnowledgeSubjectType.role:
            if self.role_id is None or self.member_user_id is not None:
                raise ValueError("Для subject_type=role нужен role_id без member_user_id")
        elif self.member_user_id is None or self.role_id is not None:
            raise ValueError("Для subject_type=member нужен member_user_id без role_id")
        return self


class AccessReplaceRequest(BaseModel):
    all_members: bool = False
    rules: list[AccessRuleInput] = Field(default_factory=list)


class AccessRuleResponse(BaseModel):
    id: str
    subject_type: str
    role_id: str | None = None
    member_user_id: str | None = None
    effect: str


class AccessResponse(BaseModel):
    all_members: bool
    rules: list[AccessRuleResponse]
