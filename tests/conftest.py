# tests/conftest.py
import os

# Тестовое окружение задаём ДО импорта приложения (settings кэшируется через
# lru_cache при первом импорте src.*): rate-limit выключен по умолчанию (включаем
# точечно фикстурой rate_limit_on), хранилище slowapi — in-memory без сети.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_STORAGE_URI", "memory://")

import uuid
from collections.abc import AsyncGenerator, Generator

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.app.core import redis as redis_module
from src.app.core.config import get_settings
from src.app.core.database import Base, get_session
from src.app.core.rate_limit import limiter
from src.app.core.security import hash_password
from src.app.main import app
from src.app.models.user import User, UserRole

settings = get_settings()


@pytest.fixture(autouse=True)
async def _fake_redis() -> AsyncGenerator[None]:
    """Подменяет общий Redis-клиент на fakeredis (lockout) — сеть не нужна.

    Свежий инстанс на каждый тест даёт изоляцию счётчиков блокировки.
    """
    client: fakeredis.aioredis.FakeRedis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_module._client = client
    yield
    await client.flushall()
    redis_module._client = None


@pytest.fixture
def rate_limit_on() -> Generator[None]:
    """Включить slowapi-лимит для конкретного теста (с чистым счётчиком)."""
    limiter.reset()
    limiter.enabled = True
    yield
    limiter.enabled = False
    limiter.reset()


TEST_DATABASE_URL = (
    f"postgresql+asyncpg://{settings.postgres_user}:{settings.postgres_password}"
    f"@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}_test"
)


@pytest.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(scope="session")
def test_session_factory(test_engine):
    return async_sessionmaker(test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _cleanup_tables(test_session_factory):
    """Truncate all tables after each test for isolation."""
    yield
    async with test_session_factory() as session:
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(text(f"TRUNCATE TABLE {table.name} CASCADE"))
        await session.commit()


@pytest.fixture
async def db_session(test_session_factory) -> AsyncGenerator[AsyncSession]:
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
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "test@example.com",
            "password": "Test1234",
        },
    )
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def super_admin_user(db_session: AsyncSession) -> User:
    user = User(
        id=uuid.uuid4(),
        email="admin@example.com",
        password_hash=hash_password("Test1234"),
        name="Super Admin",
        is_verified=True,
        role=UserRole.super_admin,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def super_admin_headers(
    super_admin_user: User,
    client: AsyncClient,
) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "Test1234"},
    )
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}
