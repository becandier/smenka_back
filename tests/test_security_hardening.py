# tests/test_security_hardening.py
"""Тесты усиления безопасности: rate-limit, lockout, счётчик попыток кода,
аудит (включая системную запись Celery) и глобальный 500-хендлер."""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from src.app.core.config import get_settings
from src.app.core.database import get_session
from src.app.core.security import hash_password
from src.app.main import app
from src.app.models.audit_log import AuditLog
from src.app.models.organization import Organization
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User, VerificationCode
from src.app.tasks.shifts import auto_finish_stale_shifts

settings = get_settings()

# Часть тестов модуля гоняет Celery-таску (auto_finish_stale_shifts) через
# отдельное синхронное подключение — db_session должен коммитить по-настоящему.
# См. tests/conftest.py::db_session.
pytestmark = pytest.mark.db_real_commit

# Синхронная тестовая сессия (для Celery-задач, как в test_tasks.py).
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


async def _create_org(
    client: AsyncClient, headers: dict[str, str], name: str = "Audit Org"
) -> str:
    """Создать организацию от super_admin (он становится owner) и вернуть её id."""
    response = await client.post("/api/v1/organizations", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    org_id: str = response.json()["data"]["id"]
    return org_id


class TestLoginRateLimit:
    async def test_login_rate_limited_after_threshold(
        self, client: AsyncClient, verified_user: User, rate_limit_on: None
    ):
        """login_rate_limit = 5/minute → 6-й запрос с одного IP даёт 429."""
        last = None
        for _ in range(6):
            last = await client.post(
                "/api/v1/auth/login",
                json={"email": "test@example.com", "password": "wrong-pass-1"},
            )
        assert last is not None
        assert last.status_code == 429
        assert last.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert any(h.lower() == "retry-after" for h in last.headers)

    async def test_rate_limit_disabled_by_default(self, client: AsyncClient, verified_user: User):
        """Без фикстуры rate_limit_on лимит выключен — 6 запросов не дают 429."""
        for _ in range(6):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "test@example.com", "password": "wrong-pass-2"},
            )
            assert response.status_code != 429


class TestVerificationCodeAttempts:
    async def test_code_burned_after_max_attempts(self, client: AsyncClient):
        """После max_code_attempts неверных вводов код «сожжён» — даже верный
        код даёт TOO_MANY_CODE_ATTEMPTS."""
        reg = await client.post(
            "/api/v1/auth/register",
            json={"email": "burn@example.com", "password": "Password1", "name": "Burn"},
        )
        code = reg.json()["data"]["verification_code"]
        wrong = "0000" if code != "0000" else "1111"

        for _ in range(settings.max_code_attempts):
            response = await client.post(
                "/api/v1/auth/verify",
                json={"email": "burn@example.com", "code": wrong},
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "INVALID_CODE"

        # Верный код — но он уже сожжён.
        response = await client.post(
            "/api/v1/auth/verify",
            json={"email": "burn@example.com", "code": code},
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "TOO_MANY_CODE_ATTEMPTS"

    async def test_resend_issues_fresh_code_with_zero_attempts(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """resend-code выдаёт новый код со сброшенными попытками; верификация
        новым кодом проходит, несмотря на попытки по старому."""
        reg = await client.post(
            "/api/v1/auth/register",
            json={"email": "resend@example.com", "password": "Password1", "name": "Re"},
        )
        user_id = uuid.UUID(reg.json()["data"]["user_id"])
        code1 = reg.json()["data"]["verification_code"]
        wrong = "0000" if code1 != "0000" else "1111"

        for _ in range(3):
            await client.post(
                "/api/v1/auth/verify",
                json={"email": "resend@example.com", "code": wrong},
            )

        # Отодвигаем код в прошлое, чтобы обойти 30-с cooldown resend.
        await db_session.execute(
            update(VerificationCode)
            .where(VerificationCode.user_id == user_id)
            .values(created_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await db_session.commit()

        resend = await client.post(
            "/api/v1/auth/resend-code",
            json={"email": "resend@example.com"},
        )
        assert resend.status_code == 200
        code2 = resend.json()["data"]["verification_code"]

        response = await client.post(
            "/api/v1/auth/verify",
            json={"email": "resend@example.com", "code": code2},
        )
        assert response.status_code == 200
        assert response.json()["data"]["access_token"]


class TestAccountLockout:
    async def test_account_locked_after_max_failures(
        self, client: AsyncClient, verified_user: User
    ):
        """max_login_failures неудач → следующий вход (даже с верным паролем) — 423."""
        for _ in range(settings.max_login_failures):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "test@example.com", "password": "wrong"},
            )
            assert response.status_code == 401

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "test@example.com", "password": "Test1234"},
        )
        assert response.status_code == 423
        assert response.json()["error"]["code"] == "ACCOUNT_LOCKED"

    async def test_successful_login_resets_counter(self, client: AsyncClient, verified_user: User):
        """Успешный вход сбрасывает счётчик неудач."""
        for _ in range(3):
            await client.post(
                "/api/v1/auth/login",
                json={"email": "test@example.com", "password": "wrong"},
            )
        ok = await client.post(
            "/api/v1/auth/login",
            json={"email": "test@example.com", "password": "Test1234"},
        )
        assert ok.status_code == 200

        # Счётчик сброшен — ещё 3 неудачи не блокируют.
        for _ in range(3):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "test@example.com", "password": "wrong"},
            )
            assert response.status_code == 401

    async def test_lockout_no_enumeration_for_unknown_email(self, client: AsyncClient):
        """Несуществующий email тоже блокируется после N — нет enumeration-оракула."""
        for _ in range(settings.max_login_failures):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": "ghost@example.com", "password": "whatever"},
            )
            assert response.status_code == 401

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@example.com", "password": "whatever"},
        )
        assert response.status_code == 423
        assert response.json()["error"]["code"] == "ACCOUNT_LOCKED"


class TestAuditLog:
    async def test_settings_update_recorded_and_listed(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org(client, super_admin_headers)
        patch_resp = await client.patch(
            f"/api/v1/organizations/{org_id}/settings",
            headers=super_admin_headers,
            json={"max_pause_minutes": 45},
        )
        assert patch_resp.status_code == 200

        response = await client.get(
            f"/api/v1/organizations/{org_id}/audit-logs",
            headers=super_admin_headers,
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] >= 1
        entry = next(it for it in data["items"] if it["action"] == "settings.update")
        assert entry["resource_type"] == "settings"
        assert entry["summary"]["max_pause_minutes"] == 45
        assert entry["actor_name"]
        assert entry["actor_name"] != "Система"
        assert entry["actor_user_id"] is not None

    async def test_location_create_audited_with_resource_id(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org(client, super_admin_headers)
        loc = await client.post(
            f"/api/v1/organizations/{org_id}/locations",
            headers=super_admin_headers,
            json={"name": "Точка", "latitude": 55.7, "longitude": 37.6, "radius_meters": 100},
        )
        loc_id = loc.json()["data"]["id"]

        response = await client.get(
            f"/api/v1/organizations/{org_id}/audit-logs?action=location.create",
            headers=super_admin_headers,
        )
        items = response.json()["data"]["items"]
        assert len(items) == 1
        assert items[0]["resource_type"] == "location"
        assert items[0]["resource_id"] == loc_id

    async def test_audit_filter_unknown_action_is_422(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org(client, super_admin_headers)
        response = await client.get(
            f"/api/v1/organizations/{org_id}/audit-logs?action=bogus.action",
            headers=super_admin_headers,
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_audit_forbidden_for_non_member(
        self,
        client: AsyncClient,
        super_admin_headers: dict[str, str],
        auth_headers: dict[str, str],
    ):
        org_id = await _create_org(client, super_admin_headers)
        response = await client.get(
            f"/api/v1/organizations/{org_id}/audit-logs",
            headers=auth_headers,
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"

    async def test_audit_404_for_missing_org(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        response = await client.get(
            f"/api/v1/organizations/{uuid.uuid4()}/audit-logs",
            headers=super_admin_headers,
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ORG_NOT_FOUND"


class TestCeleryAudit:
    async def test_auto_finish_writes_system_audit_entry(self, db_session: AsyncSession):
        """Авто-завершение org-смены по графику (work_schedules, R4) пишет аудит
        с actor_user_id = null. Персональные смены больше не авто-завершаются —
        сценарий проверяется на org-смене с просроченным scheduled_end_at."""
        user = User(
            id=uuid.uuid4(),
            email=f"celery-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("Test1234"),
            name="Celery User",
            is_verified=True,
        )
        db_session.add(user)
        await db_session.flush()

        org = Organization(id=uuid.uuid4(), name="Celery Org", owner_id=user.id)
        db_session.add(org)
        await db_session.flush()
        db_session.add(OrganizationSettings(id=uuid.uuid4(), organization_id=org.id))
        await db_session.flush()

        shift_id = uuid.uuid4()
        scheduled_end = datetime.now(UTC) - timedelta(hours=1)
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=17),
            status=ShiftStatus.active,
            scheduled_start_at=datetime.now(UTC) - timedelta(hours=17),
            scheduled_end_at=scheduled_end,
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "shift.auto_finish",
                AuditLog.resource_id == shift_id,
            )
        )
        entry = result.scalar_one()
        assert entry.actor_user_id is None
        assert entry.resource_type == "shift"
        assert entry.summary is not None
        assert "finished_at" in entry.summary


class TestGlobalExceptionHandler:
    async def test_unhandled_exception_returns_500_envelope(
        self,
        db_session: AsyncSession,
        verified_user: User,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Необработанное исключение → конверт {data:null, error:{code:ERROR}} со
        статусом 500 (формат для клиента не меняется)."""

        async def _override_session():
            yield db_session

        app.dependency_overrides[get_session] = _override_session
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            ) as ac:
                login = await ac.post(
                    "/api/v1/auth/login",
                    json={"email": "test@example.com", "password": "Test1234"},
                )
                token = login.json()["data"]["access_token"]

                import src.app.api.deps as deps_module

                async def _boom(*args: object, **kwargs: object) -> object:
                    raise RuntimeError("boom")

                monkeypatch.setattr(deps_module, "get_user_by_id", _boom)

                response = await ac.get(
                    "/api/v1/users/me",
                    headers={"Authorization": f"Bearer {token}"},
                )
            assert response.status_code == 500
            body = response.json()
            assert body["data"] is None
            assert body["error"]["code"] == "ERROR"
        finally:
            app.dependency_overrides.clear()
