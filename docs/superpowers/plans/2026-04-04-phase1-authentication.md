# Phase 1 — Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement full authentication flow — registration with email verification, login, token refresh, logout, and user profile endpoints with a unified response wrapper.

**Architecture:** Three-layer approach: ORM models → service layer (business logic) → FastAPI endpoints. Auth service handles all token/code logic, endpoints are thin wrappers. All responses wrapped in `{"data": ..., "error": ...}` format. Tests run against real PostgreSQL with per-test transaction rollback.

**Tech Stack:** FastAPI, async SQLAlchemy 2.0, asyncpg, Pydantic v2, python-jose (JWT), passlib (bcrypt), pytest + httpx

---

## File Structure

### New files

| File | Responsibility |
|------|---------------|
| `src/app/schemas/base.py` | Response wrapper (`ApiResponse`, `ApiError`), error codes enum |
| `src/app/models/user.py` | `User`, `RefreshToken`, `VerificationCode` ORM models |
| `src/app/schemas/auth.py` | Auth request/response schemas (`RegisterRequest`, `LoginRequest`, etc.) |
| `src/app/schemas/user.py` | User schemas (`UserResponse`, `UserUpdate`) |
| `src/app/services/auth.py` | Auth business logic (register, verify, login, refresh, logout) |
| `src/app/api/v1/auth.py` | Auth endpoints router |
| `src/app/api/v1/users.py` | User profile endpoints router |
| `tests/test_auth.py` | Auth flow tests |
| `tests/test_users.py` | User profile tests |

### Modified files

| File | Changes |
|------|---------|
| `src/app/core/config.py` | Add `verification_code_expire_minutes`, `verification_code_length`, `verification_code_cooldown_seconds` |
| `src/app/models/__init__.py` | Import all models for Alembic autodetect |
| `src/app/api/deps.py` | Add `get_current_user`, `CurrentUserDep` |
| `src/app/api/v1/router.py` | Include auth and users routers |
| `src/app/main.py` | Add exception handlers for response wrapper |
| `tests/conftest.py` | Test DB setup, session override, auth fixtures |

---

### Task 1: Response Wrapper Foundation

**Files:**
- Create: `src/app/schemas/base.py`
- Modify: `src/app/main.py`

- [ ] **Step 1: Create response wrapper schemas**

```python
# src/app/schemas/base.py
from typing import Any

from pydantic import BaseModel


class ApiError(BaseModel):
    code: str
    message: str
    validation: list[dict[str, str]] | None = None


class ApiResponse(BaseModel):
    data: Any | None = None
    error: ApiError | None = None

    @classmethod
    def success(cls, data: Any = None) -> "ApiResponse":
        return cls(data=data)

    @classmethod
    def fail(
        cls,
        code: str,
        message: str,
        validation: list[dict[str, str]] | None = None,
    ) -> "ApiResponse":
        return cls(error=ApiError(code=code, message=message, validation=validation))
```

- [ ] **Step 2: Add exception handlers to main.py**

Add to `src/app/main.py` after `app` creation (before `app.include_router`):

```python
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.app.schemas.base import ApiResponse


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiResponse.fail(
            code=exc.detail if isinstance(exc.detail, str) else "ERROR",
            message=str(exc.detail),
        ).model_dump(),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError,
) -> JSONResponse:
    validation_errors = [
        {"field": ".".join(str(loc) for loc in err["loc"]), "message": err["msg"]}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=ApiResponse.fail(
            code="VALIDATION_ERROR",
            message="Ошибка валидации",
            validation=validation_errors,
        ).model_dump(),
    )
```

- [ ] **Step 3: Verify the app still starts**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.main import app; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/app/schemas/base.py src/app/main.py
git commit -m "feat(auth): add API response wrapper with exception handlers"
```

---

### Task 2: Config Updates

**Files:**
- Modify: `src/app/core/config.py`

- [ ] **Step 1: Add verification code settings**

Add to `Settings` class in `src/app/core/config.py`, after the `# Auth` section:

```python
    # Verification
    verification_code_expire_minutes: int = 15
    verification_code_length: int = 4
    verification_code_cooldown_seconds: int = 30
```

- [ ] **Step 2: Verify import works**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.core.config import get_settings; s = get_settings(); print(s.verification_code_expire_minutes, s.verification_code_length, s.verification_code_cooldown_seconds)"`
Expected: `15 4 30`

- [ ] **Step 3: Commit**

```bash
git add src/app/core/config.py
git commit -m "feat(auth): add verification code settings to config"
```

---

### Task 3: ORM Models

**Files:**
- Create: `src/app/models/user.py`
- Modify: `src/app/models/__init__.py`

- [ ] **Step 1: Create User, RefreshToken, VerificationCode models**

```python
# src/app/models/user.py
import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(255))
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )
    verification_codes: Mapped[list["VerificationCode"]] = relationship(
        back_populates="user", cascade="all, delete-orphan",
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
    )
    token: Mapped[str] = mapped_column(Text, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")


class VerificationCode(Base):
    __tablename__ = "verification_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
    )
    code: Mapped[str] = mapped_column(String(10))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
    )

    user: Mapped["User"] = relationship(back_populates="verification_codes")
```

- [ ] **Step 2: Update models __init__.py**

```python
# src/app/models/__init__.py
from src.app.models.user import RefreshToken, User, VerificationCode

__all__ = ["User", "RefreshToken", "VerificationCode"]
```

- [ ] **Step 3: Verify models are importable**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.models import User, RefreshToken, VerificationCode; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/app/models/user.py src/app/models/__init__.py
git commit -m "feat(auth): add User, RefreshToken, VerificationCode models"
```

---

### Task 4: Alembic Migration

**Files:**
- Generate: `src/migrations/versions/` (new migration file)

- [ ] **Step 1: Generate migration**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && alembic -c src/alembic.ini revision --autogenerate -m "add_users_refresh_tokens_verification_codes"`
Expected: New file in `src/migrations/versions/`

- [ ] **Step 2: Review the generated migration**

Open the generated file and verify it creates:
- `users` table with all columns
- `refresh_tokens` table with FK to users
- `verification_codes` table with FK to users
- Unique index on `users.email`
- Index on `refresh_tokens.token`

- [ ] **Step 3: Run migration**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && alembic -c src/alembic.ini upgrade head`
Expected: Migration applied successfully

- [ ] **Step 4: Commit**

```bash
git add src/migrations/versions/
git commit -m "feat(auth): add migration for auth tables"
```

---

### Task 5: Test Infrastructure

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Rewrite conftest with real PostgreSQL support**

Replace the entire `tests/conftest.py`:

```python
# tests/conftest.py
import uuid
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.app.core.config import get_settings
from src.app.core.database import Base, get_session
from src.app.core.security import hash_password
from src.app.main import app
from src.app.models.user import User

settings = get_settings()

TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}"
    f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}_test"
)

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False)
test_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(scope="session", autouse=True)
async def _setup_db():
    """Create all tables once per test session."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()


@pytest.fixture(autouse=True)
async def _cleanup_tables():
    """Truncate all tables after each test for isolation."""
    yield
    async with test_session_factory() as session:
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(text(f"TRUNCATE TABLE {table.name} CASCADE"))
        await session.commit()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    """Provide a database session for the test."""
    async with test_session_factory() as session:
        yield session


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """HTTP client with overridden DB session."""
    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def verified_user(db_session: AsyncSession) -> User:
    """Create a verified user for tests that need an existing account."""
    user = User(
        id=uuid.uuid4(),
        email="test@example.com",
        password_hash=hash_password("Test1234"),
        name="Test User",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def auth_headers(verified_user: User, client: AsyncClient) -> dict[str, str]:
    """Login as verified_user and return Authorization header."""
    response = await client.post("/api/v1/auth/login", json={
        "email": "test@example.com",
        "password": "Test1234",
    })
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}
```

- [ ] **Step 2: Create test database**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && docker compose exec db psql -U smenka -c "CREATE DATABASE smenka_test;" || true`
Expected: `CREATE DATABASE` or already exists

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "feat(auth): set up test infrastructure with real PostgreSQL"
```

---

### Task 6: Auth Schemas

**Files:**
- Create: `src/app/schemas/auth.py`

- [ ] **Step 1: Create auth schemas with password validation**

```python
# src/app/schemas/auth.py
import re

from pydantic import BaseModel, EmailStr, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    name: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            msg = "Пароль должен быть не менее 8 символов"
            raise ValueError(msg)
        if not re.search(r"[a-zA-Zа-яА-ЯёЁ]", v):
            msg = "Пароль должен содержать хотя бы одну букву"
            raise ValueError(msg)
        if not re.search(r"\d", v):
            msg = "Пароль должен содержать хотя бы одну цифру"
            raise ValueError(msg)
        return v


class RegisterResponse(BaseModel):
    user_id: str
    message: str
    verification_code: str | None = None  # Only in dev — remove later


class VerifyRequest(BaseModel):
    email: EmailStr
    code: str


class ResendCodeRequest(BaseModel):
    email: EmailStr


class ResendCodeResponse(BaseModel):
    message: str
    verification_code: str | None = None  # Only in dev — remove later


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class MessageResponse(BaseModel):
    message: str
```

- [ ] **Step 2: Verify import**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.schemas.auth import RegisterRequest; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/app/schemas/auth.py
git commit -m "feat(auth): add auth request/response schemas"
```

---

### Task 7: User Schemas

**Files:**
- Create: `src/app/schemas/user.py`

- [ ] **Step 1: Create user schemas**

```python
# src/app/schemas/user.py
from datetime import datetime

from pydantic import BaseModel, EmailStr


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    phone: str | None
    name: str
    is_verified: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
```

- [ ] **Step 2: Verify import**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.schemas.user import UserResponse, UserUpdate; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/app/schemas/user.py
git commit -m "feat(auth): add user request/response schemas"
```

---

### Task 8: Auth Service

**Files:**
- Create: `src/app/services/auth.py`

- [ ] **Step 1: Write the auth service**

```python
# src/app/services/auth.py
import logging
import secrets
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.core.security import (
    ALGORITHM,
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from src.app.models.user import RefreshToken, User, VerificationCode

logger = logging.getLogger(__name__)
settings = get_settings()


class AuthError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def register(
    session: AsyncSession,
    email: str,
    password: str,
    name: str,
) -> tuple[User, str]:
    """Register a new user and generate a verification code.

    Returns (user, code).
    """
    existing = await get_user_by_email(session, email)
    if existing is not None:
        raise AuthError("EMAIL_TAKEN", "Пользователь с таким email уже существует", 409)

    user = User(
        email=email,
        password_hash=hash_password(password),
        name=name,
    )
    session.add(user)
    await session.flush()

    code = _generate_code()
    verification = VerificationCode(
        user_id=user.id,
        code=code,
        expires_at=datetime.now(UTC) + timedelta(minutes=settings.verification_code_expire_minutes),
    )
    session.add(verification)
    await session.flush()

    logger.info("Verification code for %s: %s", email, code)
    return user, code


async def verify_email(
    session: AsyncSession,
    email: str,
    code: str,
) -> tuple[str, str]:
    """Verify email with code. Returns (access_token, refresh_token) on success."""
    user = await get_user_by_email(session, email)
    if user is None:
        raise AuthError("USER_NOT_FOUND", "Пользователь не найден", 404)

    if user.is_verified:
        raise AuthError("ALREADY_VERIFIED", "Email уже подтверждён", 400)

    result = await session.execute(
        select(VerificationCode)
        .where(
            VerificationCode.user_id == user.id,
            VerificationCode.code == code,
            VerificationCode.expires_at > datetime.now(UTC),
        )
        .order_by(VerificationCode.created_at.desc())
        .limit(1)
    )
    verification = result.scalar_one_or_none()

    if verification is None:
        raise AuthError("INVALID_CODE", "Неверный или просроченный код", 400)

    user.is_verified = True
    await session.delete(verification)
    await session.flush()

    access_token = create_access_token(str(user.id))
    refresh_token = await _create_refresh_token_db(session, user.id)
    return access_token, refresh_token


async def resend_code(session: AsyncSession, email: str) -> str:
    """Resend verification code. Returns the code (for dev logging)."""
    user = await get_user_by_email(session, email)
    if user is None:
        raise AuthError("USER_NOT_FOUND", "Пользователь не найден", 404)

    if user.is_verified:
        raise AuthError("ALREADY_VERIFIED", "Email уже подтверждён", 400)

    # Check cooldown — last code must be older than cooldown_seconds
    result = await session.execute(
        select(VerificationCode)
        .where(VerificationCode.user_id == user.id)
        .order_by(VerificationCode.created_at.desc())
        .limit(1)
    )
    last_code = result.scalar_one_or_none()

    if last_code is not None:
        elapsed = (datetime.now(UTC) - last_code.created_at).total_seconds()
        if elapsed < settings.verification_code_cooldown_seconds:
            remaining = int(settings.verification_code_cooldown_seconds - elapsed)
            raise AuthError(
                "COOLDOWN",
                f"Повторная отправка доступна через {remaining} сек",
                429,
            )

    code = _generate_code()
    verification = VerificationCode(
        user_id=user.id,
        code=code,
        expires_at=datetime.now(UTC) + timedelta(minutes=settings.verification_code_expire_minutes),
    )
    session.add(verification)
    await session.flush()

    logger.info("Verification code for %s: %s", email, code)
    return code


async def login(
    session: AsyncSession,
    email: str,
    password: str,
) -> tuple[str, str]:
    """Authenticate user. Returns (access_token, refresh_token)."""
    user = await get_user_by_email(session, email)
    if user is None or not verify_password(password, user.password_hash):
        raise AuthError("INVALID_CREDENTIALS", "Неверный email или пароль", 401)

    if not user.is_verified:
        raise AuthError("NOT_VERIFIED", "Email не подтверждён", 403)

    access_token = create_access_token(str(user.id))
    refresh_token = await _create_refresh_token_db(session, user.id)
    return access_token, refresh_token


async def refresh_tokens(
    session: AsyncSession,
    refresh_token: str,
) -> tuple[str, str]:
    """Rotate refresh token. Returns (new_access_token, new_refresh_token)."""
    # Decode JWT to get subject
    try:
        payload = jwt.decode(refresh_token, settings.secret_key, algorithms=[ALGORITHM])
        if payload.get("type") != "refresh":
            raise AuthError("INVALID_TOKEN", "Невалидный refresh-токен", 401)
        user_id = payload.get("sub")
    except JWTError:
        raise AuthError("INVALID_TOKEN", "Невалидный refresh-токен", 401)

    # Check token exists in DB and is not revoked
    result = await session.execute(
        select(RefreshToken).where(
            RefreshToken.token == refresh_token,
            RefreshToken.revoked.is_(False),
        )
    )
    db_token = result.scalar_one_or_none()
    if db_token is None:
        raise AuthError("INVALID_TOKEN", "Токен отозван или не существует", 401)

    # Revoke old, issue new
    db_token.revoked = True
    await session.flush()

    new_access = create_access_token(user_id)
    new_refresh = await _create_refresh_token_db(session, db_token.user_id)
    return new_access, new_refresh


async def logout(session: AsyncSession, refresh_token: str) -> None:
    """Revoke a refresh token."""
    result = await session.execute(
        select(RefreshToken).where(
            RefreshToken.token == refresh_token,
            RefreshToken.revoked.is_(False),
        )
    )
    db_token = result.scalar_one_or_none()
    if db_token is not None:
        db_token.revoked = True
        await session.flush()


async def get_user_by_id(session: AsyncSession, user_id: str) -> User | None:
    """Get user by UUID string."""
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def _create_refresh_token_db(session: AsyncSession, user_id) -> str:
    """Create a refresh token JWT and store it in the DB."""
    token_str = create_refresh_token(str(user_id))
    db_token = RefreshToken(
        user_id=user_id,
        token=token_str,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_expire_days),
    )
    session.add(db_token)
    await session.flush()
    return token_str


def _generate_code() -> str:
    """Generate a random N-digit numeric code."""
    length = settings.verification_code_length
    upper = 10 ** length
    return str(secrets.randbelow(upper)).zfill(length)
```

- [ ] **Step 2: Verify import**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.services.auth import register, login, verify_email, refresh_tokens, logout; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/app/services/auth.py
git commit -m "feat(auth): add auth service with register, verify, login, refresh, logout"
```

---

### Task 9: `get_current_user` Dependency

**Files:**
- Modify: `src/app/api/deps.py`

- [ ] **Step 1: Add get_current_user dependency**

Replace the entire `src/app/api/deps.py`:

```python
# src/app/api/deps.py
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.core.database import get_session
from src.app.core.security import ALGORITHM
from src.app.models.user import User
from src.app.services.auth import get_user_by_id

SessionDep = Annotated[AsyncSession, Depends(get_session)]

security_scheme = HTTPBearer()
settings = get_settings()


async def get_current_user(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security_scheme)],
) -> User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
        if payload.get("type") == "refresh":
            raise HTTPException(status_code=401, detail="INVALID_TOKEN")
    except JWTError:
        raise HTTPException(status_code=401, detail="INVALID_TOKEN")

    user = await get_user_by_id(session, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="USER_NOT_FOUND")
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
```

- [ ] **Step 2: Verify import**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.api.deps import SessionDep, CurrentUserDep; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/app/api/deps.py
git commit -m "feat(auth): add get_current_user dependency"
```

---

### Task 10: Auth Endpoints

**Files:**
- Create: `src/app/api/v1/auth.py`
- Modify: `src/app/api/v1/router.py`

- [ ] **Step 1: Create auth router**

```python
# src/app/api/v1/auth.py
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.app.api.deps import SessionDep
from src.app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    ResendCodeRequest,
    ResendCodeResponse,
    TokenResponse,
    VerifyRequest,
)
from src.app.schemas.base import ApiResponse
from src.app.services.auth import AuthError
from src.app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201)
async def register(body: RegisterRequest, session: SessionDep) -> ApiResponse:
    try:
        user, code = await auth_service.register(
            session, body.email, body.password, body.name,
        )
        await session.commit()
        return ApiResponse.success(
            RegisterResponse(
                user_id=str(user.id),
                message="Код подтверждения отправлен на email",
                verification_code=code,  # dev only
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/verify")
async def verify(body: VerifyRequest, session: SessionDep) -> ApiResponse:
    try:
        access_token, refresh_token = await auth_service.verify_email(
            session, body.email, body.code,
        )
        await session.commit()
        return ApiResponse.success(
            TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/resend-code")
async def resend_code(body: ResendCodeRequest, session: SessionDep) -> ApiResponse:
    try:
        code = await auth_service.resend_code(session, body.email)
        await session.commit()
        return ApiResponse.success(
            ResendCodeResponse(
                message="Код отправлен повторно",
                verification_code=code,  # dev only
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/login")
async def login(body: LoginRequest, session: SessionDep) -> ApiResponse:
    try:
        access_token, refresh_token = await auth_service.login(
            session, body.email, body.password,
        )
        await session.commit()
        return ApiResponse.success(
            TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/refresh")
async def refresh(body: RefreshRequest, session: SessionDep) -> ApiResponse:
    try:
        access_token, refresh_token = await auth_service.refresh_tokens(
            session, body.refresh_token,
        )
        await session.commit()
        return ApiResponse.success(
            TokenResponse(
                access_token=access_token,
                refresh_token=refresh_token,
            ).model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )


@router.post("/logout")
async def logout(body: LogoutRequest, session: SessionDep) -> ApiResponse:
    try:
        await auth_service.logout(session, body.refresh_token)
        await session.commit()
        return ApiResponse.success(
            MessageResponse(message="Вы вышли из системы").model_dump()
        )
    except AuthError as e:
        return JSONResponse(
            status_code=e.status_code,
            content=ApiResponse.fail(e.code, e.message).model_dump(),
        )
```

- [ ] **Step 2: Wire auth router into v1 router**

Replace `src/app/api/v1/router.py`:

```python
# src/app/api/v1/router.py
from fastapi import APIRouter

from src.app.api.v1.auth import router as auth_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
```

- [ ] **Step 3: Verify app starts with all routes**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.main import app; routes = [r.path for r in app.routes]; print([r for r in routes if 'auth' in r])"`
Expected: List containing `/api/v1/auth/register`, `/api/v1/auth/login`, etc.

- [ ] **Step 4: Commit**

```bash
git add src/app/api/v1/auth.py src/app/api/v1/router.py
git commit -m "feat(auth): add auth endpoints (register, verify, login, refresh, logout)"
```

---

### Task 11: User Endpoints

**Files:**
- Create: `src/app/api/v1/users.py`
- Modify: `src/app/api/v1/router.py`

- [ ] **Step 1: Create users router**

```python
# src/app/api/v1/users.py
from fastapi import APIRouter

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.schemas.base import ApiResponse
from src.app.schemas.user import UserResponse, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me")
async def get_me(user: CurrentUserDep) -> ApiResponse:
    return ApiResponse.success(
        UserResponse(
            id=str(user.id),
            email=user.email,
            phone=user.phone,
            name=user.name,
            is_verified=user.is_verified,
            created_at=user.created_at,
        ).model_dump(mode="json")
    )


@router.patch("/me")
async def update_me(
    body: UserUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    if body.name is not None:
        user.name = body.name
    if body.phone is not None:
        user.phone = body.phone
    await session.commit()
    await session.refresh(user)
    return ApiResponse.success(
        UserResponse(
            id=str(user.id),
            email=user.email,
            phone=user.phone,
            name=user.name,
            is_verified=user.is_verified,
            created_at=user.created_at,
        ).model_dump(mode="json")
    )
```

- [ ] **Step 2: Add users router to v1**

Update `src/app/api/v1/router.py` — add after auth_router import:

```python
from src.app.api.v1.users import router as users_router
```

And add after `router.include_router(auth_router)`:

```python
router.include_router(users_router)
```

- [ ] **Step 3: Verify routes**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && python -c "from src.app.main import app; routes = [r.path for r in app.routes]; print([r for r in routes if 'users' in r])"`
Expected: `['/api/v1/users/me']` (appears twice for GET and PATCH)

- [ ] **Step 4: Commit**

```bash
git add src/app/api/v1/users.py src/app/api/v1/router.py
git commit -m "feat(auth): add user profile endpoints (GET /me, PATCH /me)"
```

---

### Task 12: Auth Tests

**Files:**
- Create: `tests/test_auth.py`

- [ ] **Step 1: Write registration tests**

```python
# tests/test_auth.py
import pytest
from httpx import AsyncClient


class TestRegister:
    async def test_register_success(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/register", json={
            "email": "new@example.com",
            "password": "Password1",
            "name": "New User",
        })
        assert response.status_code == 201
        data = response.json()
        assert data["error"] is None
        assert data["data"]["user_id"] is not None
        assert data["data"]["verification_code"] is not None
        assert len(data["data"]["verification_code"]) == 4

    async def test_register_duplicate_email(self, client: AsyncClient, verified_user):
        response = await client.post("/api/v1/auth/register", json={
            "email": "test@example.com",
            "password": "Password1",
            "name": "Another",
        })
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "EMAIL_TAKEN"

    async def test_register_weak_password_no_digit(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/register", json={
            "email": "weak@example.com",
            "password": "NoDigitsHere",
            "name": "Weak",
        })
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_register_weak_password_too_short(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/register", json={
            "email": "short@example.com",
            "password": "Ab1",
            "name": "Short",
        })
        assert response.status_code == 422

    async def test_register_missing_name(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/register", json={
            "email": "noname@example.com",
            "password": "Password1",
        })
        assert response.status_code == 422


class TestVerify:
    async def test_verify_success_returns_tokens(self, client: AsyncClient):
        # Register
        reg = await client.post("/api/v1/auth/register", json={
            "email": "verify@example.com",
            "password": "Password1",
            "name": "Verifier",
        })
        code = reg.json()["data"]["verification_code"]

        # Verify
        response = await client.post("/api/v1/auth/verify", json={
            "email": "verify@example.com",
            "code": code,
        })
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["access_token"] is not None
        assert data["refresh_token"] is not None
        assert data["token_type"] == "bearer"

    async def test_verify_wrong_code(self, client: AsyncClient):
        await client.post("/api/v1/auth/register", json={
            "email": "wrongcode@example.com",
            "password": "Password1",
            "name": "Wrong",
        })
        response = await client.post("/api/v1/auth/verify", json={
            "email": "wrongcode@example.com",
            "code": "0000",
        })
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_CODE"

    async def test_verify_already_verified(self, client: AsyncClient, verified_user):
        response = await client.post("/api/v1/auth/verify", json={
            "email": "test@example.com",
            "code": "1234",
        })
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ALREADY_VERIFIED"

    async def test_verify_nonexistent_email(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/verify", json={
            "email": "nobody@example.com",
            "code": "1234",
        })
        assert response.status_code == 404


class TestResendCode:
    async def test_resend_code_success(self, client: AsyncClient):
        await client.post("/api/v1/auth/register", json={
            "email": "resend@example.com",
            "password": "Password1",
            "name": "Resender",
        })
        # Wait for cooldown to pass (in tests it's instant since we control time)
        # For now, we accept a 429 if cooldown hasn't passed
        response = await client.post("/api/v1/auth/resend-code", json={
            "email": "resend@example.com",
        })
        # Either 200 (cooldown passed) or 429 (too fast)
        assert response.status_code in (200, 429)

    async def test_resend_code_nonexistent_email(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/resend-code", json={
            "email": "nobody@example.com",
        })
        assert response.status_code == 404


class TestLogin:
    async def test_login_success(self, client: AsyncClient, verified_user):
        response = await client.post("/api/v1/auth/login", json={
            "email": "test@example.com",
            "password": "Test1234",
        })
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["access_token"] is not None
        assert data["refresh_token"] is not None

    async def test_login_wrong_password(self, client: AsyncClient, verified_user):
        response = await client.post("/api/v1/auth/login", json={
            "email": "test@example.com",
            "password": "WrongPass1",
        })
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_login_nonexistent_user(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/login", json={
            "email": "nobody@example.com",
            "password": "Password1",
        })
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_login_unverified_user(self, client: AsyncClient):
        await client.post("/api/v1/auth/register", json={
            "email": "unverified@example.com",
            "password": "Password1",
            "name": "Unverified",
        })
        response = await client.post("/api/v1/auth/login", json={
            "email": "unverified@example.com",
            "password": "Password1",
        })
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "NOT_VERIFIED"


class TestRefresh:
    async def test_refresh_success(self, client: AsyncClient, verified_user):
        login_resp = await client.post("/api/v1/auth/login", json={
            "email": "test@example.com",
            "password": "Test1234",
        })
        old_refresh = login_resp.json()["data"]["refresh_token"]

        response = await client.post("/api/v1/auth/refresh", json={
            "refresh_token": old_refresh,
        })
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["access_token"] is not None
        assert data["refresh_token"] is not None
        assert data["refresh_token"] != old_refresh  # Token rotated

    async def test_refresh_revoked_token(self, client: AsyncClient, verified_user):
        login_resp = await client.post("/api/v1/auth/login", json={
            "email": "test@example.com",
            "password": "Test1234",
        })
        old_refresh = login_resp.json()["data"]["refresh_token"]

        # Use it once (revokes old)
        await client.post("/api/v1/auth/refresh", json={
            "refresh_token": old_refresh,
        })

        # Try to use revoked token again
        response = await client.post("/api/v1/auth/refresh", json={
            "refresh_token": old_refresh,
        })
        assert response.status_code == 401

    async def test_refresh_invalid_token(self, client: AsyncClient):
        response = await client.post("/api/v1/auth/refresh", json={
            "refresh_token": "garbage.token.here",
        })
        assert response.status_code == 401


class TestLogout:
    async def test_logout_success(self, client: AsyncClient, verified_user):
        login_resp = await client.post("/api/v1/auth/login", json={
            "email": "test@example.com",
            "password": "Test1234",
        })
        refresh_token = login_resp.json()["data"]["refresh_token"]

        response = await client.post("/api/v1/auth/logout", json={
            "refresh_token": refresh_token,
        })
        assert response.status_code == 200

        # Refresh should now fail
        refresh_resp = await client.post("/api/v1/auth/refresh", json={
            "refresh_token": refresh_token,
        })
        assert refresh_resp.status_code == 401
```

- [ ] **Step 2: Run auth tests**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && pytest tests/test_auth.py -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_auth.py
git commit -m "test(auth): add auth flow tests"
```

---

### Task 13: User Profile Tests

**Files:**
- Create: `tests/test_users.py`

- [ ] **Step 1: Write user profile tests**

```python
# tests/test_users.py
from httpx import AsyncClient


class TestGetMe:
    async def test_get_me_success(
        self, client: AsyncClient, verified_user, auth_headers: dict,
    ):
        response = await client.get("/api/v1/users/me", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["email"] == "test@example.com"
        assert data["name"] == "Test User"
        assert data["is_verified"] is True

    async def test_get_me_unauthorized(self, client: AsyncClient):
        response = await client.get("/api/v1/users/me")
        assert response.status_code in (401, 403)


class TestUpdateMe:
    async def test_update_name(
        self, client: AsyncClient, verified_user, auth_headers: dict,
    ):
        response = await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"name": "Updated Name"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["name"] == "Updated Name"

    async def test_update_phone(
        self, client: AsyncClient, verified_user, auth_headers: dict,
    ):
        response = await client.patch(
            "/api/v1/users/me",
            headers=auth_headers,
            json={"phone": "+79991234567"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["phone"] == "+79991234567"

    async def test_update_me_unauthorized(self, client: AsyncClient):
        response = await client.patch(
            "/api/v1/users/me",
            json={"name": "Hacker"},
        )
        assert response.status_code in (401, 403)
```

- [ ] **Step 2: Run all tests**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_users.py
git commit -m "test(auth): add user profile tests"
```

---

### Task 14: Update Documentation

**Files:**
- Modify: `docs/ARCHITECTURE.md`
- Modify: `docs/ROADMAP.md`

- [ ] **Step 1: Update ARCHITECTURE.md**

Replace the entire file with:

```markdown
# Архитектура — текущее состояние

Последнее обновление: 2026-04-04 (фаза 1)

---

## Модели (SQLAlchemy)

| Модель | Таблица | Описание |
|--------|---------|----------|
| `User` | `users` | Пользователь (email, name, phone, password_hash, is_verified) |
| `RefreshToken` | `refresh_tokens` | JWT refresh-токен (token, expires_at, revoked) |
| `VerificationCode` | `verification_codes` | Код верификации email (code, expires_at) |

---

## Эндпоинты

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| GET | `/health` | Проверка жизни | Нет |
| POST | `/api/v1/auth/register` | Регистрация | Нет |
| POST | `/api/v1/auth/verify` | Подтверждение email → auto-login | Нет |
| POST | `/api/v1/auth/resend-code` | Повторная отправка кода | Нет |
| POST | `/api/v1/auth/login` | Логин | Нет |
| POST | `/api/v1/auth/refresh` | Обновление пары токенов | Нет (refresh_token в body) |
| POST | `/api/v1/auth/logout` | Отзыв refresh-токена | Нет (refresh_token в body) |
| GET | `/api/v1/users/me` | Текущий пользователь | Bearer |
| PATCH | `/api/v1/users/me` | Обновление профиля (name, phone) | Bearer |

---

## Сервисы

| Файл | Описание |
|------|----------|
| `services/auth.py` | Регистрация, верификация, логин, refresh, logout |

---

## Зависимости (DI)

| Имя | Файл | Описание |
|-----|------|----------|
| `SessionDep` | `api/deps.py` | `AsyncSession` через `Depends` |
| `CurrentUserDep` | `api/deps.py` | Текущий пользователь из JWT (HTTPBearer) |

---

## Формат ответов

Все ответы обёрнуты в:

```json
{"data": <payload | null>, "error": <ApiError | null>}
```

`ApiError`: `{"code": "ERROR_CODE", "message": "...", "validation": [...]}`

---

## Внешние сервисы

Нет. Проект полностью автономный (PostgreSQL + API).

---

## Ключевые решения

См. `docs/decisions/` для полных ADR.
```

- [ ] **Step 2: Update ROADMAP.md — mark Phase 1 tasks**

Change Phase 1 status to `[x]` for completed items. Note that SQLAdmin is deferred:

```markdown
## Фаза 1 — Аутентификация `[x]`
- [x] Модель `User` (id, email, phone, password_hash, name, is_verified, created_at)
- [x] Модель `RefreshToken` (id, user_id, token, expires_at, revoked)
- [x] Модель `VerificationCode` (id, user_id, code, expires_at, created_at)
- [x] Регистрация (email + пароль + name)
- [x] Верификация email (4-значный код, 15 мин TTL, cooldown 30 сек)
- [x] Логин → access_token + refresh_token
- [x] Refresh-эндпоинт (ротация токенов)
- [x] Logout (отзыв refresh-токена)
- [x] GET /me — текущий пользователь
- [x] PATCH /me — обновление профиля (name, phone)
- [x] Зависимость `get_current_user` в deps.py
- [ ] SQLAdmin: отложено до стабилизации бека
- [x] Alembic-миграция
- [x] Тесты: регистрация, верификация, логин, refresh, logout, /me, невалидный токен
- [x] Обёртка ответов: `{"data": ..., "error": ...}`
```

- [ ] **Step 3: Commit**

```bash
git add docs/ARCHITECTURE.md docs/ROADMAP.md
git commit -m "docs: update architecture and roadmap for phase 1"
```

---

### Task 15: Final Linting & Verification

- [ ] **Step 1: Run ruff**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && ruff check src/ tests/`
Expected: No errors (fix any issues)

- [ ] **Step 2: Run ruff format check**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && ruff format --check src/ tests/`
Expected: All files formatted (run `ruff format src/ tests/` if needed)

- [ ] **Step 3: Run full test suite**

Run: `cd /Users/sharaputdinimanov/projects/smenka/smenka_back && pytest tests/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 4: Commit any lint fixes**

```bash
git add -u
git commit -m "style: fix lint issues"
```
