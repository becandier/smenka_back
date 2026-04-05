# tests/test_tasks.py
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from src.app.core.config import get_settings
from src.app.core.security import hash_password
from src.app.models.organization import Organization
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.shift import Pause, Shift, ShiftStatus
from src.app.models.user import RefreshToken, User, VerificationCode
from src.app.tasks.cleanup import cleanup_expired_tokens
from src.app.tasks.shifts import auto_finish_stale_pauses, auto_finish_stale_shifts

settings = get_settings()

TEST_DATABASE_URL_SYNC = (
    f"postgresql://{settings.postgres_user}:{settings.postgres_password}"
    f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}_test"
)

sync_test_engine = create_engine(TEST_DATABASE_URL_SYNC, echo=False)
sync_test_session_factory = sessionmaker(sync_test_engine, expire_on_commit=False)


@contextmanager
def get_sync_test_session() -> Generator[Session]:
    session = sync_test_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _make_user(
    *,
    user_id: uuid.UUID | None = None,
    email: str | None = None,
) -> User:
    return User(
        id=user_id or uuid.uuid4(),
        email=email or f"task-test-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("Test1234"),
        name="Task Test User",
        is_verified=True,
    )


def _make_org(*, owner_id: uuid.UUID, org_id: uuid.UUID | None = None) -> Organization:
    return Organization(
        id=org_id or uuid.uuid4(),
        name="Test Org",
        owner_id=owner_id,
    )


class TestAutoFinishStaleShifts:
    async def test_personal_stale_shift_finished(self, db_session: AsyncSession):
        """Personal shift started 17h ago (exceeds 16h default) -> finished."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=None,
            started_at=datetime.now(UTC) - timedelta(hours=17),
            status=ShiftStatus.active,
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated = result.scalar_one()

        assert updated.status == ShiftStatus.finished
        assert updated.finished_at is not None

    async def test_personal_fresh_shift_not_finished(self, db_session: AsyncSession):
        """Personal shift started 5h ago (within 16h default) -> untouched."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=None,
            started_at=datetime.now(UTC) - timedelta(hours=5),
            status=ShiftStatus.active,
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated = result.scalar_one()

        assert updated.status == ShiftStatus.active
        assert updated.finished_at is None

    async def test_org_shift_uses_org_settings(self, db_session: AsyncSession):
        """Org with auto_finish_hours=8, shift started 9h ago -> finished."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(),
            organization_id=org.id,
            auto_finish_hours=8,
        )
        db_session.add(org_settings)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=9),
            status=ShiftStatus.active,
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated = result.scalar_one()

        assert updated.status == ShiftStatus.finished
        assert updated.finished_at is not None

    async def test_stale_shift_pauses_also_closed(self, db_session: AsyncSession):
        """Stale shift with an open pause -> both shift and pause get finished."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=None,
            started_at=datetime.now(UTC) - timedelta(hours=17),
            status=ShiftStatus.paused,
        )
        db_session.add(shift)
        await db_session.flush()

        pause_id = uuid.uuid4()
        pause = Pause(
            id=pause_id,
            shift_id=shift_id,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            finished_at=None,
        )
        db_session.add(pause)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()

        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = result.scalar_one()
        assert updated_shift.status == ShiftStatus.finished
        assert updated_shift.finished_at is not None

        pause_result = await db_session.execute(select(Pause).where(Pause.id == pause_id))
        updated_pause = pause_result.scalar_one()
        assert updated_pause.finished_at is not None


class TestAutoFinishStalePauses:
    async def test_pause_exceeding_limit_finished(self, db_session: AsyncSession):
        """Org max_pause_minutes=30, pause started 35 min ago -> pause closed, shift active."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(),
            organization_id=org.id,
            max_pause_minutes=30,
        )
        db_session.add(org_settings)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.paused,
        )
        db_session.add(shift)
        await db_session.flush()

        pause_id = uuid.uuid4()
        pause = Pause(
            id=pause_id,
            shift_id=shift_id,
            started_at=datetime.now(UTC) - timedelta(minutes=35),
            finished_at=None,
        )
        db_session.add(pause)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_pauses()

        db_session.expire_all()

        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = result.scalar_one()
        assert updated_shift.status == ShiftStatus.active

        pause_result = await db_session.execute(select(Pause).where(Pause.id == pause_id))
        updated_pause = pause_result.scalar_one()
        assert updated_pause.finished_at is not None

    async def test_pause_within_limit_not_finished(self, db_session: AsyncSession):
        """Org max_pause_minutes=60, pause started 30 min ago -> stays open."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(),
            organization_id=org.id,
            max_pause_minutes=60,
        )
        db_session.add(org_settings)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.paused,
        )
        db_session.add(shift)
        await db_session.flush()

        pause_id = uuid.uuid4()
        pause = Pause(
            id=pause_id,
            shift_id=shift_id,
            started_at=datetime.now(UTC) - timedelta(minutes=30),
            finished_at=None,
        )
        db_session.add(pause)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_pauses()

        db_session.expire_all()

        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = result.scalar_one()
        assert updated_shift.status == ShiftStatus.paused

        pause_result = await db_session.execute(select(Pause).where(Pause.id == pause_id))
        updated_pause = pause_result.scalar_one()
        assert updated_pause.finished_at is None

    async def test_personal_pauses_not_affected(self, db_session: AsyncSession):
        """Personal shift (no org) with open pause -> not affected by auto-finish."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=None,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.paused,
        )
        db_session.add(shift)
        await db_session.flush()

        pause_id = uuid.uuid4()
        pause = Pause(
            id=pause_id,
            shift_id=shift_id,
            started_at=datetime.now(UTC) - timedelta(minutes=120),
            finished_at=None,
        )
        db_session.add(pause)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_pauses()

        db_session.expire_all()

        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = result.scalar_one()
        assert updated_shift.status == ShiftStatus.paused

        pause_result = await db_session.execute(select(Pause).where(Pause.id == pause_id))
        updated_pause = pause_result.scalar_one()
        assert updated_pause.finished_at is None


class TestCleanupExpiredTokens:
    async def test_expired_tokens_deleted(self, db_session: AsyncSession):
        """Expired refresh token + expired verification code -> both deleted."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        token_id = uuid.uuid4()
        token = RefreshToken(
            id=token_id,
            user_id=user.id,
            token=f"expired-token-{uuid.uuid4().hex}",
            expires_at=datetime.now(UTC) - timedelta(days=1),
            revoked=False,
        )
        db_session.add(token)

        code_id = uuid.uuid4()
        code = VerificationCode(
            id=code_id,
            user_id=user.id,
            code="1234",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db_session.add(code)
        await db_session.commit()

        with patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session):
            cleanup_expired_tokens()

        db_session.expire_all()

        token_result = await db_session.execute(
            select(RefreshToken).where(RefreshToken.id == token_id)
        )
        assert token_result.scalar_one_or_none() is None

        code_result = await db_session.execute(
            select(VerificationCode).where(VerificationCode.id == code_id)
        )
        assert code_result.scalar_one_or_none() is None

    async def test_revoked_tokens_deleted(self, db_session: AsyncSession):
        """Revoked (but not expired) token -> deleted."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        token_id = uuid.uuid4()
        token = RefreshToken(
            id=token_id,
            user_id=user.id,
            token=f"revoked-token-{uuid.uuid4().hex}",
            expires_at=datetime.now(UTC) + timedelta(days=30),
            revoked=True,
        )
        db_session.add(token)
        await db_session.commit()

        with patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session):
            cleanup_expired_tokens()

        db_session.expire_all()

        result = await db_session.execute(
            select(RefreshToken).where(RefreshToken.id == token_id)
        )
        assert result.scalar_one_or_none() is None

    async def test_valid_tokens_kept(self, db_session: AsyncSession):
        """Valid token (not expired, not revoked) -> kept."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        token_id = uuid.uuid4()
        token = RefreshToken(
            id=token_id,
            user_id=user.id,
            token=f"valid-token-{uuid.uuid4().hex}",
            expires_at=datetime.now(UTC) + timedelta(days=30),
            revoked=False,
        )
        db_session.add(token)
        await db_session.commit()

        with patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session):
            cleanup_expired_tokens()

        db_session.expire_all()

        result = await db_session.execute(
            select(RefreshToken).where(RefreshToken.id == token_id)
        )
        assert result.scalar_one_or_none() is not None
