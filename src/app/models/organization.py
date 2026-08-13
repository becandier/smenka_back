import enum
import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base

if TYPE_CHECKING:
    from src.app.models.member_rate import OrganizationMemberRate
    from src.app.models.organization_role import OrganizationRole
    from src.app.models.organization_settings import OrganizationSettings
    from src.app.models.user import User
    from src.app.models.work_location import WorkLocation


class MemberRole(enum.StrEnum):
    admin = "admin"
    employee = "employee"


def _generate_invite_code() -> str:
    return secrets.token_hex(4).upper()


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(255))
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    invite_code: Mapped[str] = mapped_column(
        String(8),
        unique=True,
        default=_generate_invite_code,
    )
    timezone: Mapped[str] = mapped_column(
        String(64),
        default="Europe/Moscow",
        server_default="Europe/Moscow",
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    # foreign_keys обязателен с admin_created_accounts: между organizations и
    # users теперь два пути FK (owner_id и обратный users.created_by_org_id) —
    # без явного указания SQLAlchemy не может определить join однозначно
    # (AmbiguousForeignKeysError).
    owner: Mapped["User"] = relationship(
        back_populates="owned_organizations",
        foreign_keys=[owner_id],
    )
    members: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    work_locations: Mapped[list["WorkLocation"]] = relationship(
        back_populates="organization",
        cascade="all, delete-orphan",
    )
    settings: Mapped["OrganizationSettings | None"] = relationship(
        back_populates="organization",
        uselist=False,
        cascade="all, delete-orphan",
    )


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_org_member"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    role: Mapped[MemberRole] = mapped_column(
        Enum(MemberRole),
        default=MemberRole.employee,
    )
    role_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_roles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    display_name: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        default=None,
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    organization: Mapped["Organization"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship(back_populates="memberships")
    custom_role: Mapped["OrganizationRole | None"] = relationship(
        foreign_keys=[role_id],
    )
    rates: Mapped[list["OrganizationMemberRate"]] = relationship(
        back_populates="member",
        cascade="all, delete-orphan",
    )
