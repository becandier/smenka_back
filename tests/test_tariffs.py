"""Фича tariffs: тарифы и подписки организаций (ADR-004 «Доступ = роль × тариф»)."""

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from src.app.core.security import hash_password
from src.app.models.notification import Notification, NotificationType
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.penalty import OrganizationPenaltyTemplate, Penalty
from src.app.models.plan import Plan
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.subscription import Subscription, SubscriptionEvent
from src.app.models.user import User
from src.app.models.work_location import WorkLocation
from src.app.services import entitlements
from src.app.services.entitlements import GRACE_DAYS, EffectiveStatus, PlanFeature
from src.app.tasks.subscriptions import notify_subscription_status
from tests.conftest import TEST_DATABASE_URL_SYNC

# Часть тестов модуля гоняет Celery-таску уведомлений (run_notify_task) через
# отдельное синхронное подключение — db_session должен коммитить по-настоящему.
# См. tests/conftest.py::db_session.
pytestmark = pytest.mark.db_real_commit

# TEST_DATABASE_URL_SYNC — из conftest, а не пересчитан здесь: под pytest-xdist
# (make test-fast) у каждого воркера своя суффиксированная база (см.
# tests/conftest.py::TEST_DB_NAME) — sync-подключение обязано смотреть в ТУ ЖЕ
# базу, что и db_session этого воркера, иначе Celery-таска не увидит данных,
# которые тест закоммитил.
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


def run_notify_task() -> None:
    """Прогон celery-задачи уведомлений на тестовой БД (паттерн test_tasks.py)."""
    with patch("src.app.tasks.subscriptions.get_sync_session", get_sync_test_session):
        notify_subscription_status()


# --- helpers -------------------------------------------------------------------
async def _make_user(db_session: AsyncSession, email: str, name: str = "User") -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("Test1234"),
        name=name,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "Test1234"})
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _make_org(
    db_session: AsyncSession, owner_id: uuid.UUID, name: str = "Org"
) -> Organization:
    org = Organization(name=name, owner_id=owner_id)
    db_session.add(org)
    await db_session.commit()
    return org


async def _add_member(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID,
    role: MemberRole = MemberRole.employee,
) -> OrganizationMember:
    member = OrganizationMember(organization_id=org_id, user_id=user_id, role=role)
    db_session.add(member)
    await db_session.commit()
    return member


async def _make_subscription(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    *,
    plan_code: str = "premium",
    status: str = "trialing",
    trial_ends_at: datetime | None = None,
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
    last_expiry_notice_days: int | None = None,
) -> Subscription:
    sub = Subscription(
        organization_id=org_id,
        plan_code=plan_code,
        status=status,
        trial_ends_at=trial_ends_at,
        current_period_start=current_period_start,
        current_period_end=current_period_end,
        last_expiry_notice_days=last_expiry_notice_days,
    )
    db_session.add(sub)
    await db_session.commit()
    return sub


def _data(resp: Any) -> Any:
    return resp.json()["data"]


def _err(resp: Any) -> str:
    return resp.json()["error"]["code"]


# --- 1. Эффективный статус: границы (чистая функция, без БД) -------------------
class TestEffectiveStatusBoundaries:
    NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

    def test_last_second_of_trial_is_still_trialing(self):
        sub = Subscription(status="trialing", trial_ends_at=self.NOW + timedelta(seconds=1))
        assert entitlements.compute_effective_status(sub, self.NOW) == EffectiveStatus.trialing

    def test_exact_trial_boundary_is_still_trialing(self):
        """`now <= trial_ends_at` инклюзивно."""
        sub = Subscription(status="trialing", trial_ends_at=self.NOW)
        assert entitlements.compute_effective_status(sub, self.NOW) == EffectiveStatus.trialing

    def test_first_second_after_trial_is_past_due(self):
        sub = Subscription(status="trialing", trial_ends_at=self.NOW - timedelta(seconds=1))
        assert entitlements.compute_effective_status(sub, self.NOW) == EffectiveStatus.past_due

    def test_first_day_of_grace_is_past_due(self):
        sub = Subscription(status="active", current_period_end=self.NOW - timedelta(days=1))
        assert entitlements.compute_effective_status(sub, self.NOW) == EffectiveStatus.past_due

    def test_exact_end_of_grace_is_still_past_due(self):
        """`now <= period_reference + GRACE_DAYS` инклюзивно — ровно 7 суток ещё grace."""
        sub = Subscription(
            status="active", current_period_end=self.NOW - timedelta(days=GRACE_DAYS)
        )
        assert entitlements.compute_effective_status(sub, self.NOW) == EffectiveStatus.past_due

    def test_one_second_past_grace_is_suspended(self):
        sub = Subscription(
            status="active",
            current_period_end=self.NOW - timedelta(days=GRACE_DAYS, seconds=1),
        )
        assert entitlements.compute_effective_status(sub, self.NOW) == EffectiveStatus.suspended

    def test_canceled_is_always_canceled_regardless_of_dates(self):
        sub = Subscription(status="canceled", current_period_end=self.NOW + timedelta(days=30))
        assert entitlements.compute_effective_status(sub, self.NOW) == EffectiveStatus.canceled

    def test_days_left_negative_in_past_due(self):
        sub = Subscription(status="active", current_period_end=self.NOW - timedelta(days=2))
        assert entitlements.days_left(sub, self.NOW) == -2

    def test_days_left_none_in_suspended(self):
        sub = Subscription(
            status="active",
            current_period_end=self.NOW - timedelta(days=GRACE_DAYS, seconds=1),
        )
        assert entitlements.days_left(sub, self.NOW) is None

    def test_days_left_none_in_canceled(self):
        sub = Subscription(status="canceled", current_period_end=self.NOW + timedelta(days=30))
        assert entitlements.days_left(sub, self.NOW) is None


# --- PlanFeature <-> колонки plans ----------------------------------------------
class TestPlanFeatureMatchesPlansColumns:
    async def test_every_plan_feature_has_a_boolean_column_on_every_plan(
        self, db_session: AsyncSession
    ):
        plans = list((await db_session.execute(select(Plan))).scalars().all())
        assert len(plans) >= 2, "standard/premium должны быть засеяны"
        for feature in PlanFeature:
            column_name = entitlements.FEATURE_COLUMNS[feature]
            for plan in plans:
                assert hasattr(plan, column_name), f"{plan.code} без колонки {column_name}"
                assert isinstance(getattr(plan, column_name), bool)

    async def test_standard_lacks_premium_features_premium_has_all(self, db_session: AsyncSession):
        standard = await db_session.get(Plan, "standard")
        premium = await db_session.get(Plan, "premium")
        assert standard is not None
        assert premium is not None
        assert standard.feature_fines is False
        assert standard.feature_test_import is False
        assert premium.feature_fines is True
        assert premium.feature_test_import is True
        assert standard.max_employees == 15
        assert standard.max_locations == 3
        assert premium.max_employees is None
        assert premium.max_locations is None


# --- Лимиты: три точки роста -----------------------------------------------------
class TestPlanLimitReached:
    async def test_member_creation_blocked_at_standard_limit(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "limit-owner1@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "LimitOrg1")
        await _make_subscription(
            db_session,
            org.id,
            plan_code="standard",
            status="active",
            current_period_start=datetime.now(UTC),
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
        for i in range(15):
            emp = await _make_user(db_session, f"limit1-emp{i}@example.com", f"Emp {i}")
            await _add_member(db_session, org.id, emp.id)

        headers = await _login(client, "limit-owner1@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=headers,
            json={"name": "Overflow", "login": "overflow1"},
        )
        assert resp.status_code == 402, resp.text
        assert _err(resp) == "PLAN_LIMIT_REACHED"

    async def test_member_creation_ok_below_standard_limit(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "limit-owner1b@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "LimitOrg1b")
        await _make_subscription(
            db_session,
            org.id,
            plan_code="standard",
            status="active",
            current_period_start=datetime.now(UTC),
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
        for i in range(14):
            emp = await _make_user(db_session, f"limit1b-emp{i}@example.com", f"Emp {i}")
            await _add_member(db_session, org.id, emp.id)

        headers = await _login(client, "limit-owner1b@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=headers,
            json={"name": "The 15th", "login": "the15th"},
        )
        assert resp.status_code == 201, resp.text

    async def test_join_by_invite_blocked_at_standard_limit(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "limit-owner2@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "LimitOrg2")
        await _make_subscription(
            db_session,
            org.id,
            plan_code="standard",
            status="active",
            current_period_start=datetime.now(UTC),
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
        for i in range(15):
            emp = await _make_user(db_session, f"limit2-emp{i}@example.com", f"Emp {i}")
            await _add_member(db_session, org.id, emp.id)

        joiner = await _make_user(db_session, "joiner2@example.com", "Joiner")
        headers = await _login(client, "joiner2@example.com")
        resp = await client.post(f"/api/v1/organizations/join/{org.invite_code}", headers=headers)
        assert resp.status_code == 402, resp.text
        assert _err(resp) == "PLAN_LIMIT_REACHED"
        del joiner

    async def test_location_creation_blocked_at_standard_limit(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "limit-owner3@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "LimitOrg3")
        await _make_subscription(
            db_session,
            org.id,
            plan_code="standard",
            status="active",
            current_period_start=datetime.now(UTC),
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
        for i in range(3):
            db_session.add(
                WorkLocation(
                    organization_id=org.id,
                    name=f"Точка {i}",
                    latitude=55.0 + i * 0.01,
                    longitude=37.0,
                    radius_meters=100,
                )
            )
        await db_session.commit()

        headers = await _login(client, "limit-owner3@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/locations",
            headers=headers,
            json={"name": "4-я точка", "latitude": 55.5, "longitude": 37.5},
        )
        assert resp.status_code == 402, resp.text
        assert _err(resp) == "PLAN_LIMIT_REACHED"


# --- Grandfathering при даунгрейде ------------------------------------------------
class TestGrandfathering:
    async def test_existing_members_over_limit_survive_downgrade_new_one_blocked(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "gf-owner1@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "GFOrg1")
        # Начинали на Премиуме — 20 сотрудников (> лимита Стандарта).
        await _make_subscription(
            db_session,
            org.id,
            plan_code="premium",
            status="active",
            current_period_start=datetime.now(UTC),
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
        for i in range(20):
            emp = await _make_user(db_session, f"gf1-emp{i}@example.com", f"Emp {i}")
            await _add_member(db_session, org.id, emp.id)

        # Даунгрейд на Стандарт.
        sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == org.id)
            )
        ).scalar_one()
        sub.plan_code = "standard"
        await db_session.commit()

        headers = await _login(client, "gf-owner1@example.com")

        # Существующие 20 никуда не делись и организация продолжает работать.
        list_resp = await client.get(f"/api/v1/organizations/{org.id}/members", headers=headers)
        assert list_resp.status_code == 200
        assert len(_data(list_resp)["items"]) == 20

        # Новый (21-й) сотрудник — блокируется лимитом Стандарта.
        create_resp = await client.post(
            f"/api/v1/organizations/{org.id}/members",
            headers=headers,
            json={"name": "21st", "login": "the21st"},
        )
        assert create_resp.status_code == 402
        assert _err(create_resp) == "PLAN_LIMIT_REACHED"

    async def test_existing_locations_over_limit_survive_downgrade_new_one_blocked(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "gf-owner2@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "GFOrg2")
        await _make_subscription(
            db_session,
            org.id,
            plan_code="premium",
            status="active",
            current_period_start=datetime.now(UTC),
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
        for i in range(5):
            db_session.add(
                WorkLocation(
                    organization_id=org.id,
                    name=f"Точка {i}",
                    latitude=55.0 + i * 0.01,
                    longitude=37.0,
                    radius_meters=100,
                )
            )
        await db_session.commit()

        sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == org.id)
            )
        ).scalar_one()
        sub.plan_code = "standard"
        await db_session.commit()

        headers = await _login(client, "gf-owner2@example.com")
        list_resp = await client.get(f"/api/v1/organizations/{org.id}/locations", headers=headers)
        assert len(_data(list_resp)["items"]) == 5

        create_resp = await client.post(
            f"/api/v1/organizations/{org.id}/locations",
            headers=headers,
            json={"name": "6-я точка", "latitude": 55.9, "longitude": 37.9},
        )
        assert create_resp.status_code == 402
        assert _err(create_resp) == "PLAN_LIMIT_REACHED"


# --- Read-only (suspended/canceled) ---------------------------------------------
class TestReadOnly:
    async def _suspended_org(self, db_session: AsyncSession, owner_email: str, org_name: str):
        owner = await _make_user(db_session, owner_email, "Owner")
        org = await _make_org(db_session, owner.id, org_name)
        # OrganizationSettings нужны для PATCH .../settings (get_settings иначе
        # 404 SETTINGS_NOT_FOUND раньше, чем дойдёт до read-only проверки).
        db_session.add(OrganizationSettings(organization_id=org.id))
        await db_session.commit()
        await _make_subscription(
            db_session,
            org.id,
            plan_code="premium",
            status="active",
            current_period_start=datetime.now(UTC) - timedelta(days=60),
            current_period_end=datetime.now(UTC) - timedelta(days=GRACE_DAYS + 5),
        )
        return owner, org

    async def test_shift_start_blocked_in_suspended_org(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner, org = await self._suspended_org(db_session, "ro-owner1@example.com", "ROOrg1")
        emp = await _make_user(db_session, "ro-emp1@example.com", "Employee")
        await _add_member(db_session, org.id, emp.id)

        headers = await _login(client, "ro-emp1@example.com")
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=headers,
            json={"organization_id": str(org.id)},
        )
        assert resp.status_code == 402, resp.text
        assert _err(resp) == "SUBSCRIPTION_INACTIVE"
        del owner

    async def test_finish_active_shift_allowed_in_suspended_org(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner, org = await self._suspended_org(db_session, "ro-owner2@example.com", "ROOrg2")
        emp = await _make_user(db_session, "ro-emp2@example.com", "Employee")
        await _add_member(db_session, org.id, emp.id)

        shift = Shift(
            user_id=emp.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.active,
        )
        db_session.add(shift)
        await db_session.commit()

        headers = await _login(client, "ro-emp2@example.com")
        resp = await client.post(f"/api/v1/shifts/{shift.id}/finish", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "finished"
        del owner

    async def test_settings_update_blocked_in_suspended_org(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        _owner, org = await self._suspended_org(db_session, "ro-owner3@example.com", "ROOrg3")
        headers = await _login(client, "ro-owner3@example.com")
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/settings",
            headers=headers,
            json={"geo_check_enabled": False},
        )
        assert resp.status_code == 402
        assert _err(resp) == "SUBSCRIPTION_INACTIVE"

    async def test_role_update_blocked_in_suspended_org(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """admin_grants_admin_role: require_active_subscription сохранился в update_member_role
        после расширения авторизации на admin-участника."""
        _owner, org = await self._suspended_org(db_session, "ro-owner6@example.com", "ROOrg6")
        emp = await _make_user(db_session, "ro-emp6@example.com", "Employee")
        await _add_member(db_session, org.id, emp.id)

        headers = await _login(client, "ro-owner6@example.com")
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}/members/{emp.id}/role",
            headers=headers,
            json={"role": "admin"},
        )
        assert resp.status_code == 402
        assert _err(resp) == "SUBSCRIPTION_INACTIVE"

    async def test_member_leave_allowed_in_suspended_org(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Исключение read-only: выход/исключение сотрудника не блокируется."""
        _owner, org = await self._suspended_org(db_session, "ro-owner4@example.com", "ROOrg4")
        emp = await _make_user(db_session, "ro-emp4@example.com", "Employee")
        await _add_member(db_session, org.id, emp.id)

        headers = await _login(client, "ro-emp4@example.com")
        resp = await client.delete(
            f"/api/v1/organizations/{org.id}/members/{emp.id}", headers=headers
        )
        assert resp.status_code == 200, resp.text

    async def test_super_admin_bypasses_read_only(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_headers: dict[str, str]
    ):
        _owner, org = await self._suspended_org(db_session, "ro-owner5@example.com", "ROOrg5")
        resp = await client.patch(
            f"/api/v1/organizations/{org.id}",
            headers=super_admin_headers,
            json={"name": "Renamed by platform"},
        )
        assert resp.status_code == 200, resp.text


# --- Штрафы: фича Стандарта закрыта, GET/DELETE остаются --------------------------
class TestPenaltiesFeatureGating:
    async def _standard_org(self, db_session: AsyncSession, owner_email: str, org_name: str):
        owner = await _make_user(db_session, owner_email, "Owner")
        org = await _make_org(db_session, owner.id, org_name)
        await _make_subscription(
            db_session,
            org.id,
            plan_code="standard",
            status="active",
            current_period_start=datetime.now(UTC),
            current_period_end=datetime.now(UTC) + timedelta(days=30),
        )
        return owner, org

    async def test_create_penalty_template_blocked_on_standard(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner, org = await self._standard_org(db_session, "pen-owner1@example.com", "PenOrg1")
        headers = await _login(client, "pen-owner1@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/penalty-templates",
            headers=headers,
            json={"reason": "Опоздание", "amount_minor": 50000},
        )
        assert resp.status_code == 402
        assert _err(resp) == "PLAN_FEATURE_UNAVAILABLE"
        del owner

    async def test_create_penalty_blocked_on_standard(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        _owner, org = await self._standard_org(db_session, "pen-owner2@example.com", "PenOrg2")
        emp = await _make_user(db_session, "pen-emp2@example.com", "Employee")
        member = await _add_member(db_session, org.id, emp.id)

        headers = await _login(client, "pen-owner2@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/penalties",
            headers=headers,
            json={
                "member_id": str(member.id),
                "reason": "Опоздание",
                "amount_minor": 30000,
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        assert resp.status_code == 402
        assert _err(resp) == "PLAN_FEATURE_UNAVAILABLE"

    async def test_get_and_delete_penalty_available_on_standard(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner, org = await self._standard_org(db_session, "pen-owner3@example.com", "PenOrg3")
        emp = await _make_user(db_session, "pen-emp3@example.com", "Employee")
        member = await _add_member(db_session, org.id, emp.id)

        # Штраф был выписан ещё на Премиуме (снимок), сейчас организация — Стандарт.
        penalty = Penalty(
            organization_id=org.id,
            member_id=member.id,
            reason="Опоздание",
            amount_minor=50000,
            occurred_at=datetime.now(UTC),
            created_by_user_id=owner.id,
        )
        db_session.add(penalty)
        await db_session.commit()

        owner_headers = await _login(client, "pen-owner3@example.com")
        emp_headers = await _login(client, "pen-emp3@example.com")

        list_resp = await client.get(
            f"/api/v1/organizations/{org.id}/penalties", headers=owner_headers
        )
        assert list_resp.status_code == 200
        assert len(_data(list_resp)["items"]) == 1

        my_resp = await client.get(
            f"/api/v1/organizations/{org.id}/my-penalties", headers=emp_headers
        )
        assert my_resp.status_code == 200
        assert len(_data(my_resp)["items"]) == 1

        delete_resp = await client.delete(
            f"/api/v1/organizations/{org.id}/penalties/{penalty.id}", headers=owner_headers
        )
        assert delete_resp.status_code == 200, delete_resp.text
        assert _data(delete_resp)["deleted"] is True

    async def test_delete_penalty_template_available_on_standard(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        _owner, org = await self._standard_org(db_session, "pen-owner4@example.com", "PenOrg4")
        template = OrganizationPenaltyTemplate(
            organization_id=org.id, reason="Опоздание", amount_minor=50000
        )
        db_session.add(template)
        await db_session.commit()

        headers = await _login(client, "pen-owner4@example.com")
        resp = await client.delete(
            f"/api/v1/organizations/{org.id}/penalty-templates/{template.id}", headers=headers
        )
        assert resp.status_code == 200, resp.text


# --- Идемпотентность уведомлений celery-задачи ------------------------------------
class TestSubscriptionNotificationsIdempotent:
    async def test_expiring_notice_sent_once_per_threshold(self, db_session: AsyncSession):
        owner = await _make_user(db_session, "notif-owner1@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "NotifOrg1")
        await _make_subscription(
            db_session,
            org.id,
            plan_code="premium",
            status="trialing",
            trial_ends_at=datetime.now(UTC) + timedelta(days=3, hours=1),
        )

        run_notify_task()
        run_notify_task()  # повторный прогон в тот же день — не дублирует

        notes = (
            (
                await db_session.execute(
                    select(Notification).where(
                        Notification.user_id == owner.id,
                        Notification.type == NotificationType.subscription_expiring.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notes) == 1
        assert notes[0].payload["days_left"] == 3

        sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == org.id)
            )
        ).scalar_one()
        assert sub.last_expiry_notice_days == 3

    async def test_expiring_notice_reports_actual_days_left_not_threshold(
        self, db_session: AsyncSession
    ):
        """Уведомление наблюдает подписку впервые с 5 днями до конца (порог,
        сработавший первым — 7), тело/payload должны показывать реальные 5, а
        не 7 (см. code-review: раньше сообщался порог, а не days_left)."""
        owner = await _make_user(db_session, "notif-owner1b@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "NotifOrg1b")
        await _make_subscription(
            db_session,
            org.id,
            plan_code="premium",
            status="trialing",
            trial_ends_at=datetime.now(UTC) + timedelta(days=5, hours=1),
        )

        run_notify_task()

        note = (
            (
                await db_session.execute(
                    select(Notification).where(
                        Notification.user_id == owner.id,
                        Notification.type == NotificationType.subscription_expiring.value,
                    )
                )
            )
            .scalars()
            .one()
        )
        assert note.payload["days_left"] == 5
        assert "5 дн." in note.body

        sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == org.id)
            )
        ).scalar_one()
        # Антидубль остаётся привязан к сработавшему порогу (7), а не к days_left.
        assert sub.last_expiry_notice_days == 7

    async def test_suspended_notice_sent_once_even_across_many_runs(
        self, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "notif-owner2@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "NotifOrg2")
        await _make_subscription(
            db_session,
            org.id,
            plan_code="premium",
            status="active",
            current_period_start=datetime.now(UTC) - timedelta(days=60),
            current_period_end=datetime.now(UTC) - timedelta(days=GRACE_DAYS + 3),
        )

        run_notify_task()
        run_notify_task()
        run_notify_task()

        notes = (
            (
                await db_session.execute(
                    select(Notification).where(
                        Notification.user_id == owner.id,
                        Notification.type == NotificationType.subscription_suspended.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(notes) == 1

        events = (
            (
                await db_session.execute(
                    select(SubscriptionEvent).where(
                        SubscriptionEvent.organization_id == org.id,
                        SubscriptionEvent.type == "auto_suspended",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].actor_user_id is None

    async def test_admin_also_receives_notifications_employee_does_not(
        self, db_session: AsyncSession
    ):
        owner = await _make_user(db_session, "notif-owner3@example.com", "Owner")
        org = await _make_org(db_session, owner.id, "NotifOrg3")
        admin_user = await _make_user(db_session, "notif-admin3@example.com", "Admin")
        emp_user = await _make_user(db_session, "notif-emp3@example.com", "Employee")
        await _add_member(db_session, org.id, admin_user.id, role=MemberRole.admin)
        await _add_member(db_session, org.id, emp_user.id, role=MemberRole.employee)
        await _make_subscription(
            db_session,
            org.id,
            plan_code="premium",
            status="trialing",
            trial_ends_at=datetime.now(UTC) + timedelta(days=1, hours=1),
        )

        run_notify_task()

        for recipient_id in (owner.id, admin_user.id):
            notes = (
                (
                    await db_session.execute(
                        select(Notification).where(Notification.user_id == recipient_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(notes) == 1

        emp_notes_result = await db_session.execute(
            select(Notification).where(Notification.user_id == emp_user.id)
        )
        emp_notes = emp_notes_result.scalars().all()
        assert emp_notes == []
