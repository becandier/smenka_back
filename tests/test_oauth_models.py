import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.oauth import OAuthIdentity
from src.app.models.user import User


async def test_create_oauth_identity_with_valid_data(
    db_session: AsyncSession, verified_user: User
) -> None:
    identity = OAuthIdentity(
        id=uuid.uuid4(),
        user_id=verified_user.id,
        provider="google",
        provider_user_id="google-sub-123",
        email="test@example.com",
    )
    db_session.add(identity)
    await db_session.commit()

    assert identity.id is not None
    assert identity.provider == "google"
    assert identity.created_at is not None


async def test_duplicate_provider_and_provider_user_id_raises_integrity_error(
    db_session: AsyncSession, verified_user: User
) -> None:
    """UNIQUE(provider, provider_user_id) — один внешний аккаунт не может быть
    привязан к двум разным пользователям Smenka."""
    other_user = User(
        id=uuid.uuid4(),
        email="other@example.com",
        password_hash=verified_user.password_hash,
        name="Other User",
        is_verified=True,
    )
    db_session.add(other_user)
    await db_session.commit()

    db_session.add(
        OAuthIdentity(
            id=uuid.uuid4(),
            user_id=verified_user.id,
            provider="google",
            provider_user_id="dup-sub",
            email="test@example.com",
        )
    )
    await db_session.commit()

    db_session.add(
        OAuthIdentity(
            id=uuid.uuid4(),
            user_id=other_user.id,
            provider="google",
            provider_user_id="dup-sub",
            email="other@example.com",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


async def test_user_can_be_created_with_null_password_hash(db_session: AsyncSession) -> None:
    """OAuth-only пользователь: password_hash теперь nullable."""
    user = User(
        id=uuid.uuid4(),
        email="oauth-only@example.com",
        password_hash=None,
        name="OAuth Only",
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()

    assert user.password_hash is None
