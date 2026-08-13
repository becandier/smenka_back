import enum
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base

if TYPE_CHECKING:
    from src.app.models.oauth import OAuthIdentity
    from src.app.models.organization import Organization, OrganizationMember
    from src.app.models.shift import Shift


class UserRole(enum.StrEnum):
    super_admin = "super_admin"
    user = "user"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Пользователь всегда идентифицируем хотя бы одним способом
        # (admin_created_accounts: email стал nullable для учёток без почты,
        # заведённых админом организации — но login тогда обязателен).
        CheckConstraint(
            "email IS NOT NULL OR login IS NOT NULL",
            name="ck_users_email_or_login",
        ),
        # Уникальность login — глобальная по платформе, без учёта регистра;
        # частичный индекс — NULL-логины (обычный саморегистрационный путь) не
        # участвуют в уникальности.
        Index(
            "uq_users_login_lower",
            text("lower(login)"),
            unique=True,
            postgresql_where=text("login IS NOT NULL"),
        ),
        # Не unique (email остаётся регистрозависимо уникальным — вне scope
        # admin_created_accounts, см. комментарий у email ниже): только ускоряет
        # `func.lower(User.email) == ...` в `services/auth._find_user_by_ident`
        # (email-фолбэк входа) — без него это full scan на каждый логин по email,
        # т.к. `ix_users_email` — обычный btree на самой колонке, lower() под
        # него не попадает.
        Index("ix_users_email_lower", text("lower(email)")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # nullable с admin_created_accounts: учётка, заведённая админом организации,
    # может не иметь email вообще (идентификатор — только login). unique(email)
    # сохраняется — в PostgreSQL несколько NULL допустимы.
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    # Логин для входа (admin_created_accounts). Хранится как ввёл админ,
    # сравнение — по lower() (см. uq_users_login_lower выше и services/auth._find_user_by_ident).
    login: Mapped[str | None] = mapped_column(String(32), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # NULL для OAuth-only пользователей (вход только через Google/Apple, без пароля).
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    name: Mapped[str] = mapped_column(String(255))
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole),
        default=UserRole.user,
        server_default="user",
    )
    # Заполняется только для учёток, созданных админом организации через
    # admin_created_accounts — ключевое поле для прав на пароль/логин
    # (services/member_account: сброс пароля и смена логина разрешены только
    # если created_by_org_id совпадает с текущей организацией).
    created_by_org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # use_alter=True — организации ссылаются на users.id (owner_id), а
        # теперь users ссылается на organizations.id: без use_alter это
        # циклическая зависимость, которую `Base.metadata.create_all`/`drop_all`
        # (тесты) не может топологически отсортировать
        # (`sqlalchemy.exc.CircularDependencyError`). Alembic саму миграцию не
        # затрагивает — там FK создаётся явным `op.create_foreign_key` уже
        # после обеих таблиц.
        ForeignKey(
            "organizations.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_users_created_by_org_id_organizations",
        ),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    @property
    def email_display(self) -> str:
        """`email` для контрактов, где поле типа `str` не nullable (обратная
        совместимость мобильных билдов) — `""` вместо `None`, если email не
        задан (admin_created_accounts). Единая точка вместо `user.email or ""`,
        повторённого по всем org-ответам с сотрудником."""
        return self.email or ""

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    verification_codes: Mapped[list["VerificationCode"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    shifts: Mapped[list["Shift"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    # foreign_keys обязателен с admin_created_accounts: см. комментарий у
    # Organization.owner (два пути FK между organizations и users).
    owned_organizations: Mapped[list["Organization"]] = relationship(
        back_populates="owner",
        cascade="all, delete-orphan",
        foreign_keys="Organization.owner_id",
    )
    memberships: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    oauth_identities: Mapped[list["OAuthIdentity"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    token: Mapped[str] = mapped_column(Text, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")


class VerificationCode(Base):
    __tablename__ = "verification_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    code: Mapped[str] = mapped_column(String(10))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Счётчик неверных вводов этого кода в verify. При достижении
    # settings.max_code_attempts код «сжигается» (TOO_MANY_CODE_ATTEMPTS).
    attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )

    user: Mapped["User"] = relationship(back_populates="verification_codes")
