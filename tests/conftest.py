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
