import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from jose import JWTError, jwt
from sqlalchemy import delete, select
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
    expire_minutes = settings.verification_code_expire_minutes
    verification = VerificationCode(
        user_id=user.id,
        code=code,
        expires_at=datetime.now(UTC) + timedelta(minutes=expire_minutes),
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
    await session.execute(delete(VerificationCode).where(VerificationCode.user_id == user.id))
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
    expire_minutes = settings.verification_code_expire_minutes
    verification = VerificationCode(
        user_id=user.id,
        code=code,
        expires_at=datetime.now(UTC) + timedelta(minutes=expire_minutes),
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
    try:
        payload = jwt.decode(refresh_token, settings.secret_key, algorithms=[ALGORITHM])
        if payload.get("type") != "refresh":
            raise AuthError("INVALID_TOKEN", "Невалидный refresh-токен", 401)
        user_id = payload.get("sub")
    except JWTError as exc:
        raise AuthError("INVALID_TOKEN", "Невалидный refresh-токен", 401) from exc

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


async def _create_refresh_token_db(session: AsyncSession, user_id: uuid.UUID) -> str:
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
    upper = 10**length
    return str(secrets.randbelow(upper)).zfill(length)
