"""Фича online_payments: `GET /billing/config`, витрина `billing/options`,
`billing/checkout`, статус/история платежей организации, RBAC, доступность в
`suspended`, реестр `GET /admin/payments` (`docs/tasks/online_payments/backend.md`).

HTTP-слой ЮKassa всегда мокается (`yookassa_client._create_payment_request` /
`_get_payment_request`) — живых запросов к ЮKassa из тестов быть не должно
(тот же паттерн, что `email_sendpulse._send_email_request` в `test_sendpulse_email.py`).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.payment import Payment, PaymentStatus
from src.app.models.subscription import Subscription
from src.app.models.user import User
from src.app.services import yookassa_client

settings = get_settings()


# --- fixtures & helpers ----------------------------------------------------------
@pytest.fixture
def yookassa_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "yookassa_enabled", True)
    monkeypatch.setattr(settings, "yookassa_mode", "test")
    monkeypatch.setattr(settings, "yookassa_shop_id", "test-shop-id")
    monkeypatch.setattr(settings, "yookassa_secret_key", "test-secret-key")
    monkeypatch.setattr(settings, "yookassa_return_url_base", "https://admin.example.com")
    monkeypatch.setattr(settings, "billing_max_payment_minor", 10_000_000)


def _mock_create_payment(monkeypatch: pytest.MonkeyPatch, provider_payment_id: str) -> AsyncMock:
    async def _fake(
        settings_arg: Any, idempotence_key: Any, payload: dict[str, Any]
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": provider_payment_id,
                "status": "pending",
                "paid": False,
                "amount": payload["amount"],
                "confirmation": {
                    "type": "redirect",
                    "confirmation_url": f"https://yoomoney.ru/checkout/{provider_payment_id}",
                },
                "created_at": "2026-08-31T10:00:00.000Z",
                "metadata": payload["metadata"],
                "test": True,
            },
        )

    mock = AsyncMock(side_effect=_fake)
    monkeypatch.setattr(yookassa_client, "_create_payment_request", mock)
    return mock


def _mock_get_payment(monkeypatch: pytest.MonkeyPatch, response_json: dict[str, Any]) -> AsyncMock:
    mock = AsyncMock(return_value=httpx.Response(200, json=response_json))
    monkeypatch.setattr(yookassa_client, "_get_payment_request", mock)
    return mock


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
    plan_code: str = "standard",
    status: str = "active",
    trial_ends_at: datetime | None = None,
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
) -> Subscription:
    sub = Subscription(
        organization_id=org_id,
        plan_code=plan_code,
        status=status,
        trial_ends_at=trial_ends_at,
        current_period_start=current_period_start,
        current_period_end=current_period_end,
    )
    db_session.add(sub)
    await db_session.commit()
    return sub


async def _org_with_sub(
    db_session: AsyncSession,
    email_prefix: str,
    *,
    plan_code: str = "standard",
    status: str = "active",
    trial_ends_at: datetime | None = None,
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
) -> tuple[User, Organization]:
    owner = await _make_user(db_session, f"{email_prefix}@example.com", "Owner")
    org = await _make_org(db_session, owner.id, f"{email_prefix}-org")
    await _make_subscription(
        db_session,
        org.id,
        plan_code=plan_code,
        status=status,
        trial_ends_at=trial_ends_at,
        current_period_start=current_period_start,
        current_period_end=current_period_end,
    )
    return owner, org


def _data(resp: Any) -> Any:
    return resp.json()["data"]


def _err(resp: Any) -> str:
    return resp.json()["error"]["code"]


# --- 1. GET /billing/config --------------------------------------------------------
class TestBillingConfig:
    async def test_disabled_by_default(self, client: AsyncClient, auth_headers: dict[str, str]):
        resp = await client.get("/api/v1/billing/config", headers=auth_headers)
        assert resp.status_code == 200
        assert _data(resp)["enabled"] is False

    async def test_enabled_reflects_mode(
        self, client: AsyncClient, auth_headers: dict[str, str], yookassa_on: None
    ):
        resp = await client.get("/api/v1/billing/config", headers=auth_headers)
        assert resp.status_code == 200
        data = _data(resp)
        assert data == {"enabled": True, "mode": "test", "provider": "yookassa"}


# --- 2. GET .../billing/options ----------------------------------------------------
class TestBillingOptions:
    async def test_matches_backend_md_example_amounts(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Числа буквально из backend.md, §API-контракты п.2."""
        owner, org = await _org_with_sub(db_session, "opt1", plan_code="standard")
        headers = await _login(client, "opt1@example.com")

        resp = await client.get(f"/api/v1/organizations/{org.id}/billing/options", headers=headers)
        assert resp.status_code == 200, resp.text
        data = _data(resp)
        assert data["currency"] == "RUB"
        assert data["current_plan_code"] == "standard"

        by_key = {(i["plan_code"], i["months"]): i for i in data["extend"]}
        assert by_key[("standard", 1)]["amount_minor"] == 500_000
        assert by_key[("standard", 1)]["savings_minor"] == 0
        assert by_key[("standard", 3)]["amount_minor"] == 1_425_000
        assert by_key[("standard", 3)]["savings_minor"] == 75_000
        assert by_key[("standard", 3)]["monthly_minor"] == 475_000
        assert by_key[("standard", 6)]["amount_minor"] == 2_700_000
        assert by_key[("standard", 6)]["savings_minor"] == 300_000
        assert by_key[("standard", 6)]["monthly_minor"] == 450_000
        assert by_key[("premium", 1)]["amount_minor"] == 1_000_000
        assert by_key[("premium", 3)]["amount_minor"] == 2_850_000
        assert by_key[("premium", 3)]["savings_minor"] == 150_000
        assert by_key[("premium", 6)]["amount_minor"] == 5_400_000
        assert by_key[("premium", 6)]["savings_minor"] == 600_000
        del owner

    async def test_upgrade_available_standard_active(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        now = datetime.now(UTC)
        owner, org = await _org_with_sub(
            db_session,
            "opt2",
            plan_code="standard",
            status="active",
            current_period_start=now - timedelta(days=15),
            current_period_end=now + timedelta(days=15),
        )
        headers = await _login(client, "opt2@example.com")
        resp = await client.get(f"/api/v1/organizations/{org.id}/billing/options", headers=headers)
        upgrade = _data(resp)["upgrade"]
        assert upgrade["available"] is True
        assert upgrade["from_plan_code"] == "standard"
        assert upgrade["to_plan_code"] == "premium"
        assert upgrade["months_remaining"] == 1
        assert upgrade["amount_minor"] == 500_000  # (10000-5000) * 1 мес
        del owner

    async def test_upgrade_unavailable_already_premium(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        now = datetime.now(UTC)
        owner, org = await _org_with_sub(
            db_session,
            "opt3",
            plan_code="premium",
            status="active",
            current_period_end=now + timedelta(days=10),
        )
        headers = await _login(client, "opt3@example.com")
        resp = await client.get(f"/api/v1/organizations/{org.id}/billing/options", headers=headers)
        upgrade = _data(resp)["upgrade"]
        assert upgrade["available"] is False
        assert upgrade["reason"] == "already_premium"
        del owner

    async def test_upgrade_unavailable_trialing(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        now = datetime.now(UTC)
        owner, org = await _org_with_sub(
            db_session,
            "opt4",
            plan_code="standard",
            status="trialing",
            trial_ends_at=now + timedelta(days=5),
        )
        headers = await _login(client, "opt4@example.com")
        resp = await client.get(f"/api/v1/organizations/{org.id}/billing/options", headers=headers)
        upgrade = _data(resp)["upgrade"]
        assert upgrade["available"] is False
        assert upgrade["reason"] == "not_applicable"
        del owner

    async def test_variants_over_payment_limit_are_excluded(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "billing_max_payment_minor", 600_000)
        owner, org = await _org_with_sub(db_session, "opt5", plan_code="standard")
        headers = await _login(client, "opt5@example.com")
        resp = await client.get(f"/api/v1/organizations/{org.id}/billing/options", headers=headers)
        extend = _data(resp)["extend"]
        assert extend == [
            {
                "plan_code": "standard",
                "plan_name": "Стандарт",
                "months": 1,
                "base_amount_minor": 500_000,
                "discount_percent": 0,
                "amount_minor": 500_000,
                "savings_minor": 0,
                "monthly_minor": 500_000,
            }
        ]
        del owner


# --- 3. POST .../billing/checkout ---------------------------------------------------
class TestBillingCheckout:
    async def test_disabled_returns_billing_disabled(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner, org = await _org_with_sub(db_session, "co1", plan_code="standard")
        headers = await _login(client, "co1@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )
        assert resp.status_code == 503
        assert _err(resp) == "BILLING_DISABLED"
        del owner

    async def test_unknown_plan_code_404(
        self, client: AsyncClient, db_session: AsyncSession, yookassa_on: None
    ):
        owner, org = await _org_with_sub(db_session, "co2", plan_code="standard")
        headers = await _login(client, "co2@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "bogus", "months": 1},
        )
        assert resp.status_code == 404
        assert _err(resp) == "PLAN_NOT_FOUND"
        del owner

    async def test_invalid_months_returns_validation_error(
        self, client: AsyncClient, db_session: AsyncSession, yookassa_on: None
    ):
        owner, org = await _org_with_sub(db_session, "co3", plan_code="standard")
        headers = await _login(client, "co3@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 2},
        )
        assert resp.status_code == 422
        assert _err(resp) == "VALIDATION_ERROR"
        del owner

    async def test_amount_over_limit_returns_422(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(settings, "billing_max_payment_minor", 100_000)
        owner, org = await _org_with_sub(db_session, "co4", plan_code="premium")
        headers = await _login(client, "co4@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "premium", "months": 6},
        )
        assert resp.status_code == 422
        assert _err(resp) == "PAYMENT_AMOUNT_LIMIT"
        del owner

    async def test_creates_pending_payment_and_calls_provider(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        create_mock = _mock_create_payment(monkeypatch, "provider-pay-checkout-1")
        owner, org = await _org_with_sub(db_session, "co5", plan_code="standard")
        headers = await _login(client, "co5@example.com")

        resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 3},
        )
        assert resp.status_code == 200, resp.text
        data = _data(resp)
        assert data["amount_minor"] == 1_425_000
        assert data["currency"] == "RUB"
        assert data["status"] == "pending"
        assert data["confirmation_url"] == "https://yoomoney.ru/checkout/provider-pay-checkout-1"
        create_mock.assert_awaited_once()

        payment = await db_session.get(Payment, uuid.UUID(data["payment_id"]))
        assert payment is not None
        assert payment.kind == "extend"
        assert payment.plan_code == "standard"
        assert payment.months == 3
        assert payment.amount_minor == 1_425_000
        assert payment.status == PaymentStatus.pending.value
        assert payment.is_test is True
        assert payment.provider_payment_id == "provider-pay-checkout-1"
        assert payment.created_by_user_id == owner.id

    async def test_reuses_pending_payment_within_15_minutes(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        create_mock = _mock_create_payment(monkeypatch, "provider-pay-reuse-1")
        owner, org = await _org_with_sub(db_session, "co6", plan_code="standard")
        headers = await _login(client, "co6@example.com")
        body = {"kind": "extend", "plan_code": "standard", "months": 1}

        first = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout", headers=headers, json=body
        )
        second = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout", headers=headers, json=body
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert _data(first)["payment_id"] == _data(second)["payment_id"]
        create_mock.assert_awaited_once()
        del owner

    async def test_upgrade_not_applicable_returns_409(
        self, client: AsyncClient, db_session: AsyncSession, yookassa_on: None
    ):
        now = datetime.now(UTC)
        owner, org = await _org_with_sub(
            db_session,
            "co7",
            plan_code="standard",
            status="trialing",
            trial_ends_at=now + timedelta(days=5),
        )
        headers = await _login(client, "co7@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "upgrade", "plan_code": "premium"},
        )
        assert resp.status_code == 409
        assert _err(resp) == "UPGRADE_NOT_APPLICABLE"
        del owner

    async def test_upgrade_creates_payment_with_computed_amount(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _mock_create_payment(monkeypatch, "provider-pay-upgrade-1")
        now = datetime.now(UTC)
        owner, org = await _org_with_sub(
            db_session,
            "co8",
            plan_code="standard",
            status="active",
            current_period_start=now - timedelta(days=20),
            current_period_end=now + timedelta(days=40),  # ceil(40/30) = 2 месяца
        )
        headers = await _login(client, "co8@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "upgrade", "plan_code": "premium"},
        )
        assert resp.status_code == 200, resp.text
        data = _data(resp)
        assert data["amount_minor"] == 1_000_000  # (10000-5000)*2

        payment = await db_session.get(Payment, uuid.UUID(data["payment_id"]))
        assert payment is not None
        assert payment.kind == "upgrade"
        assert payment.plan_code == "premium"
        assert payment.months == 2
        assert payment.discount_percent == 0
        del owner

    async def test_upgrade_rejects_non_premium_plan_code(
        self, client: AsyncClient, db_session: AsyncSession, yookassa_on: None
    ):
        now = datetime.now(UTC)
        owner, org = await _org_with_sub(
            db_session,
            "co9",
            plan_code="standard",
            status="active",
            current_period_end=now + timedelta(days=10),
        )
        headers = await _login(client, "co9@example.com")
        resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "upgrade", "plan_code": "standard"},
        )
        assert resp.status_code == 422
        assert _err(resp) == "VALIDATION_ERROR"
        del owner


# --- RBAC + scope --------------------------------------------------------------------
class TestBillingRbacAndScope:
    async def test_employee_forbidden_on_options(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner, org = await _org_with_sub(db_session, "rbac1", plan_code="standard")
        emp = await _make_user(db_session, "rbac1-emp@example.com", "Employee")
        await _add_member(db_session, org.id, emp.id, MemberRole.employee)
        headers = await _login(client, "rbac1-emp@example.com")

        resp = await client.get(f"/api/v1/organizations/{org.id}/billing/options", headers=headers)
        assert resp.status_code == 403
        del owner

    async def test_employee_forbidden_on_checkout(
        self, client: AsyncClient, db_session: AsyncSession, yookassa_on: None
    ):
        owner, org = await _org_with_sub(db_session, "rbac2", plan_code="standard")
        emp = await _make_user(db_session, "rbac2-emp@example.com", "Employee")
        await _add_member(db_session, org.id, emp.id, MemberRole.employee)
        headers = await _login(client, "rbac2-emp@example.com")

        resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )
        assert resp.status_code == 403
        del owner

    async def test_employee_forbidden_on_payments_list(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner, org = await _org_with_sub(db_session, "rbac3", plan_code="standard")
        emp = await _make_user(db_session, "rbac3-emp@example.com", "Employee")
        await _add_member(db_session, org.id, emp.id, MemberRole.employee)
        headers = await _login(client, "rbac3-emp@example.com")

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/billing/payments", headers=headers
        )
        assert resp.status_code == 403
        del owner

    async def test_admin_member_allowed_on_options(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        owner, org = await _org_with_sub(db_session, "rbac4", plan_code="standard")
        admin_user = await _make_user(db_session, "rbac4-admin@example.com", "Admin")
        await _add_member(db_session, org.id, admin_user.id, MemberRole.admin)
        headers = await _login(client, "rbac4-admin@example.com")

        resp = await client.get(f"/api/v1/organizations/{org.id}/billing/options", headers=headers)
        assert resp.status_code == 200
        del owner

    async def test_payment_from_another_org_returns_404(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _mock_create_payment(monkeypatch, "provider-pay-scope-1")
        owner_a, org_a = await _org_with_sub(db_session, "scope-a", plan_code="standard")
        owner_b, org_b = await _org_with_sub(db_session, "scope-b", plan_code="standard")
        headers_a = await _login(client, "scope-a@example.com")
        headers_b = await _login(client, "scope-b@example.com")

        checkout = await client.post(
            f"/api/v1/organizations/{org_a.id}/billing/checkout",
            headers=headers_a,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )
        payment_id = _data(checkout)["payment_id"]

        resp = await client.get(
            f"/api/v1/organizations/{org_b.id}/billing/payments/{payment_id}", headers=headers_b
        )
        assert resp.status_code == 404
        assert _err(resp) == "PAYMENT_NOT_FOUND"
        del owner_a, owner_b

    async def test_super_admin_has_pass_through_access_to_org_payments(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
        super_admin_headers: dict[str, str],
    ):
        """super_admin — сквозной доступ, как у GET .../subscription (уточнение
        аналитика 2026-08-31: п.5/6 backend.md)."""
        _mock_create_payment(monkeypatch, "provider-pay-sa-1")
        owner, org = await _org_with_sub(db_session, "sa1", plan_code="standard")
        owner_headers = await _login(client, "sa1@example.com")

        checkout = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=owner_headers,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )
        payment_id = _data(checkout)["payment_id"]

        detail = await client.get(
            f"/api/v1/organizations/{org.id}/billing/payments/{payment_id}",
            headers=super_admin_headers,
        )
        assert detail.status_code == 200, detail.text

        listing = await client.get(
            f"/api/v1/organizations/{org.id}/billing/payments", headers=super_admin_headers
        )
        assert listing.status_code == 200
        assert _data(listing)["total"] == 1
        del owner


# --- Read-only режим и оплата ---------------------------------------------------------
class TestBillingAvailableWhenSuspended:
    async def test_options_and_checkout_work_in_suspended_org(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        from src.app.services.entitlements import GRACE_DAYS

        _mock_create_payment(monkeypatch, "provider-pay-suspended-1")
        now = datetime.now(UTC)
        owner, org = await _org_with_sub(
            db_session,
            "susp1",
            plan_code="standard",
            status="active",
            current_period_start=now - timedelta(days=90),
            current_period_end=now - timedelta(days=GRACE_DAYS + 5),
        )
        headers = await _login(client, "susp1@example.com")

        # Организация действительно suspended: обычная мутация закрыта.
        sub_resp = await client.get(
            f"/api/v1/organizations/{org.id}/subscription", headers=headers
        )
        assert _data(sub_resp)["status"] == "suspended"
        assert _data(sub_resp)["is_read_only"] is True

        # Но billing/* — нет.
        options_resp = await client.get(
            f"/api/v1/organizations/{org.id}/billing/options", headers=headers
        )
        assert options_resp.status_code == 200, options_resp.text

        checkout_resp = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )
        assert checkout_resp.status_code == 200, checkout_resp.text
        assert _err_or_none(checkout_resp) != "SUBSCRIPTION_INACTIVE"
        del owner


def _err_or_none(resp: Any) -> str | None:
    body = resp.json()
    return body["error"]["code"] if body.get("error") else None


# --- Статус/история платежей + поллинг ------------------------------------------------
class TestBillingPaymentStatusAndHistory:
    async def test_pending_payment_younger_than_10s_is_not_polled(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _mock_create_payment(monkeypatch, "provider-pay-fresh-1")
        get_mock = _mock_get_payment(monkeypatch, {})  # не должен вызываться
        owner, org = await _org_with_sub(db_session, "poll1", plan_code="standard")
        headers = await _login(client, "poll1@example.com")

        checkout = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )
        payment_id = _data(checkout)["payment_id"]

        detail = await client.get(
            f"/api/v1/organizations/{org.id}/billing/payments/{payment_id}", headers=headers
        )
        assert detail.status_code == 200
        assert _data(detail)["status"] == "pending"
        get_mock.assert_not_awaited()
        del owner

    async def test_pending_payment_older_than_10s_is_reconciled_and_applied(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _mock_create_payment(monkeypatch, "provider-pay-stale-1")
        owner, org = await _org_with_sub(
            db_session,
            "poll2",
            plan_code="standard",
            status="active",
            current_period_start=datetime.now(UTC) - timedelta(days=10),
            current_period_end=datetime.now(UTC) + timedelta(days=5),
        )
        headers = await _login(client, "poll2@example.com")

        checkout = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )
        payment_id = _data(checkout)["payment_id"]

        payment = await db_session.get(Payment, uuid.UUID(payment_id))
        assert payment is not None
        payment.created_at = datetime.now(UTC) - timedelta(seconds=15)
        await db_session.commit()

        _mock_get_payment(
            monkeypatch,
            {
                "id": "provider-pay-stale-1",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "5000.00", "currency": "RUB"},
                "metadata": {
                    "payment_id": payment_id,
                    "organization_id": str(org.id),
                    "kind": "extend",
                    "plan_code": "standard",
                    "months": "1",
                },
                "captured_at": "2026-08-31T10:05:00.000Z",
                "created_at": "2026-08-31T10:00:00.000Z",
            },
        )

        detail = await client.get(
            f"/api/v1/organizations/{org.id}/billing/payments/{payment_id}", headers=headers
        )
        assert detail.status_code == 200, detail.text
        data = _data(detail)
        assert data["status"] == "succeeded"
        assert data["applied_at"] is not None

        sub_resp = await client.get(
            f"/api/v1/organizations/{org.id}/subscription", headers=headers
        )
        assert _data(sub_resp)["status"] == "active"
        del owner

    async def test_payments_history_lists_created_payments(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _mock_create_payment(monkeypatch, "provider-pay-hist-1")
        owner, org = await _org_with_sub(db_session, "hist1", plan_code="standard")
        headers = await _login(client, "hist1@example.com")

        await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )

        resp = await client.get(
            f"/api/v1/organizations/{org.id}/billing/payments", headers=headers
        )
        assert resp.status_code == 200
        data = _data(resp)
        assert data["total"] == 1
        assert data["items"][0]["plan_name"] == "Стандарт"
        del owner


# --- Реестр платформы (super_admin) ---------------------------------------------------
class TestAdminPaymentsRegistry:
    async def test_requires_super_admin(self, client: AsyncClient, db_session: AsyncSession):
        owner, _org = await _org_with_sub(db_session, "adm1", plan_code="standard")
        headers = await _login(client, "adm1@example.com")
        resp = await client.get("/api/v1/admin/payments", headers=headers)
        assert resp.status_code == 403
        del owner

    async def test_lists_payment_with_organization_id(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
        super_admin_headers: dict[str, str],
    ):
        _mock_create_payment(monkeypatch, "provider-pay-admin-1")
        owner, org = await _org_with_sub(db_session, "adm2", plan_code="standard")
        headers = await _login(client, "adm2@example.com")

        await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )

        resp = await client.get("/api/v1/admin/payments", headers=super_admin_headers)
        assert resp.status_code == 200, resp.text
        items = _data(resp)["items"]
        row = next(i for i in items if i["organization_id"] == str(org.id))
        assert row["organization_name"] == org.name
        assert row["created_by"]["id"] == str(owner.id)

    async def test_totals_exclude_test_payments(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
        super_admin_headers: dict[str, str],
    ):
        """Тестовый магазин (yookassa_mode=test) → payments.is_test=True для ЛЮБОГО
        успешного платежа в тестах — totals обязаны игнорировать такие строки
        даже когда они succeeded (backend.md, «Тестовые платежи реально
        продлевают подписку... в MRR и суммах выручки не учитываются»)."""
        _mock_create_payment(monkeypatch, "provider-pay-totals-1")
        owner, org = await _org_with_sub(
            db_session,
            "adm3",
            plan_code="standard",
            status="active",
            current_period_start=datetime.now(UTC) - timedelta(days=10),
            current_period_end=datetime.now(UTC) + timedelta(days=5),
        )
        headers = await _login(client, "adm3@example.com")

        checkout = await client.post(
            f"/api/v1/organizations/{org.id}/billing/checkout",
            headers=headers,
            json={"kind": "extend", "plan_code": "standard", "months": 1},
        )
        payment_id = _data(checkout)["payment_id"]

        payment = await db_session.get(Payment, uuid.UUID(payment_id))
        assert payment is not None
        assert payment.is_test is True
        payment.created_at = datetime.now(UTC) - timedelta(seconds=15)
        await db_session.commit()

        _mock_get_payment(
            monkeypatch,
            {
                "id": "provider-pay-totals-1",
                "status": "succeeded",
                "paid": True,
                "amount": {"value": "5000.00", "currency": "RUB"},
                "metadata": {
                    "payment_id": payment_id,
                    "organization_id": str(org.id),
                    "kind": "extend",
                    "plan_code": "standard",
                    "months": "1",
                },
                "captured_at": "2026-08-31T10:05:00.000Z",
                "created_at": "2026-08-31T10:00:00.000Z",
            },
        )
        await client.get(
            f"/api/v1/organizations/{org.id}/billing/payments/{payment_id}", headers=headers
        )

        resp = await client.get("/api/v1/admin/payments", headers=super_admin_headers)
        assert resp.status_code == 200
        totals = _data(resp)["totals"]
        assert totals["succeeded_amount_minor"] == 0
        assert totals["count"] == 0
        del owner
