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
from src.app.models.billing_period import BillingPeriod
from src.app.models.plan import Plan
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


async def _seed_plans(conn) -> None:
    """`plans` (tariffs) — статичный справочник, в проде сидируется миграцией,
    не рантаймом приложения. Тестовая БД поднимается из ORM-метаданных
    (`create_all`), а не через Alembic, поэтому сидируем вручную — иначе
    любое создание организации падает на FK `subscriptions.plan_code`."""
    await conn.execute(
        Plan.__table__.insert(),
        [
            {
                "code": "standard",
                "name": "Стандарт",
                "price_minor": 500000,
                "currency": "RUB",
                "max_employees": 15,
                "max_locations": 3,
                "feature_fines": False,
                "feature_test_import": False,
                "sort_order": 10,
                "is_active": True,
            },
            {
                "code": "premium",
                "name": "Премиум",
                "price_minor": 1000000,
                "currency": "RUB",
                "max_employees": None,
                "max_locations": None,
                "feature_fines": True,
                "feature_test_import": True,
                "sort_order": 20,
                "is_active": True,
            },
        ],
    )


async def _seed_billing_periods(conn) -> None:
    """`billing_periods` (online_payments) — тот же случай, что `plans` выше:
    в проде сидируется миграцией (`(1,0)`/`(3,5)`/`(6,10)`), тестовая БД
    поднимается из ORM-метаданных, поэтому сидируем вручную — иначе любой
    `POST .../billing/checkout` с `kind=extend` падает на «период недоступен»."""
    await conn.execute(
        BillingPeriod.__table__.insert(),
        [
            {"months": 1, "discount_percent": 0, "is_active": True, "sort_order": 10},
            {"months": 3, "discount_percent": 5, "is_active": True, "sort_order": 20},
            {"months": 6, "discount_percent": 10, "is_active": True, "sort_order": 30},
        ],
    )


@pytest.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await _seed_plans(conn)
        await _seed_billing_periods(conn)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(scope="session")
def test_session_factory(test_engine):
    return async_sessionmaker(test_engine, expire_on_commit=False)


# Маркер для тестов, которым SAVEPOINT-изоляция не подходит (см. docstring
# db_session ниже) — зарегистрирован в pyproject.toml [tool.pytest.ini_options].
REAL_COMMIT_MARKER = "db_real_commit"


@pytest.fixture(autouse=True)
async def _cleanup_tables(request: pytest.FixtureRequest, test_session_factory):
    """TRUNCATE после теста — только для тестов с `@pytest.mark.db_real_commit`.

    Раньше это была общая зачистка после КАЖДОГО теста (десятки TRUNCATE на
    каждый из 1000+ тестов). Теперь для подавляющего большинства тестов
    изоляцию даёт сам `db_session` — откатом внешней транзакции, чистить
    после него нечего. TRUNCATE остаётся только для тестов, которым
    savepoint не подходит (см. `db_session`): они коммитят по-настоящему,
    и это надо реально смыть перед следующим тестом.

    `plans`/`billing_periods` — исключение и для этого пути: статичные
    справочники, в проде живут только миграцией и не трогаются рантаймом —
    как и в проде, в тестах сидируются один раз на сессию
    (`_seed_plans`/`_seed_billing_periods`) и не участвуют в per-test
    truncate, иначе следующий же тест не смог бы создать организацию (FK
    `subscriptions.plan_code`) или оплатить продление (FK
    `payments.plan_code`, `billing_periods.months` в
    `_get_active_billing_period`).
    """
    yield
    if request.node.get_closest_marker(REAL_COMMIT_MARKER) is None:
        return
    async with test_session_factory() as session:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name in ("plans", "billing_periods"):
                continue
            await session.execute(text(f"TRUNCATE TABLE {table.name} CASCADE"))
        await session.commit()


@pytest.fixture
async def db_session(
    request: pytest.FixtureRequest, test_engine, test_session_factory
) -> AsyncGenerator[AsyncSession]:
    """Сессия для теста.

    По умолчанию — SAVEPOINT-изоляция вместо TRUNCATE: тест получает
    выделенное подключение с реальной внешней транзакцией, а `AsyncSession`
    присоединяется к ней через `join_transaction_mode="create_savepoint"`
    (SQLAlchemy 2.0, см. "Joining a Session into an External Transaction" в
    доках SQLAlchemy) — под капотом это SAVEPOINT. Код приложения (эндпоинты)
    делает собственные `session.commit()` — это освобождает и тут же заново
    открывает SAVEPOINT, не трогая внешнюю транзакцию, так что несколько
    commit() внутри одного теста работают как обычно. В конце теста внешняя
    транзакция откатывается целиком — вместе с ней пропадают и все commit()
    приложения, база возвращается к состоянию до теста. TRUNCATE не нужен.

    Тесты, помеченные `@pytest.mark.db_real_commit` (module-level
    `pytestmark` в `test_tasks.py`/`test_tariffs.py`/
    `test_security_hardening.py`), получают старую схему — сессию с
    реальными commit на общем пуле подключений. Им это необходимо: они
    прогоняют Celery-таски через ОТДЕЛЬНОЕ синхронное подключение
    (`get_sync_session` патчится на `sync_test_session_factory`), и это
    подключение обязано увидеть данные, которые тест закоммитил через
    `db_session`, — а чужое подключение никогда не видит чужой SAVEPOINT
    (он не закоммичен на уровне БД). Для них же остаётся TRUNCATE после
    теста (`_cleanup_tables`).
    """
    if request.node.get_closest_marker(REAL_COMMIT_MARKER) is not None:
        async with test_session_factory() as session:
            yield session
        return

    async with test_engine.connect() as connection:
        await connection.begin()
        session = AsyncSession(
            bind=connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        try:
            yield session
        finally:
            await session.close()
            # Откат внешней транзакции целиком — обнуляет и её, и все
            # SAVEPOINT'ы/commit() приложения внутри неё.
            await connection.rollback()


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
