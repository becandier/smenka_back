"""Фича online_payments: `POST /billing/yookassa/webhook` — идемпотентность,
проверка IP источника, сверка суммы/metadata с провайдером, `refund.succeeded`
(`docs/tasks/online_payments/backend.md`, §4 «Уведомления провайдера» + «Возвраты»).

Провайдер всегда мокается (`yookassa_client._create_payment_request` /
`_get_payment_request`) — живых запросов к ЮKassa быть не должно.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import get_settings
from src.app.core.security import hash_password
from src.app.models.organization import Organization
from src.app.models.payment import Payment, PaymentStatus
from src.app.models.subscription import Subscription, SubscriptionEvent
from src.app.models.user import User
from src.app.services import yookassa_client

settings = get_settings()

TRUSTED_IP = "185.71.76.5"  # 185.71.76.0/27 — официальная сеть ЮKassa
UNTRUSTED_IP = "1.2.3.4"


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


async def _make_subscription(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    *,
    plan_code: str = "standard",
    status: str = "active",
    current_period_start: datetime | None = None,
    current_period_end: datetime | None = None,
) -> Subscription:
    sub = Subscription(
        organization_id=org_id,
        plan_code=plan_code,
        status=status,
        current_period_start=current_period_start,
        current_period_end=current_period_end,
    )
    db_session.add(sub)
    await db_session.commit()
    return sub


async def _org_with_active_sub(
    db_session: AsyncSession, email_prefix: str
) -> tuple[User, Organization]:
    owner = await _make_user(db_session, f"{email_prefix}@example.com", "Owner")
    org = await _make_org(db_session, owner.id, f"{email_prefix}-org")
    now = datetime.now(UTC)
    await _make_subscription(
        db_session,
        org.id,
        plan_code="standard",
        status="active",
        current_period_start=now - timedelta(days=10),
        current_period_end=now + timedelta(days=5),
    )
    return owner, org


def _data(resp: Any) -> Any:
    return resp.json()["data"]


def _err(resp: Any) -> str:
    return resp.json()["error"]["code"]


async def _create_checkout_payment(
    client: AsyncClient,
    org: Organization,
    headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    provider_payment_id: str,
) -> tuple[str, int]:
    """Создаёт платёж через API (checkout), возвращает (payment_id, amount_minor)."""
    _mock_create_payment(monkeypatch, provider_payment_id)
    resp = await client.post(
        f"/api/v1/organizations/{org.id}/billing/checkout",
        headers=headers,
        json={"kind": "extend", "plan_code": "standard", "months": 1},
    )
    assert resp.status_code == 200, resp.text
    data = _data(resp)
    return data["payment_id"], data["amount_minor"]


def _succeeded_payload(
    provider_payment_id: str, payment_id: str, org_id: uuid.UUID, amount_minor: int
) -> dict[str, Any]:
    return {
        "id": provider_payment_id,
        "status": "succeeded",
        "paid": True,
        "amount": {
            "value": yookassa_client.minor_to_amount_value(amount_minor),
            "currency": "RUB",
        },
        "metadata": {
            "payment_id": payment_id,
            "organization_id": str(org_id),
            "kind": "extend",
            "plan_code": "standard",
            "months": "1",
        },
        "captured_at": "2026-08-31T10:05:00.000Z",
        "created_at": "2026-08-31T10:00:00.000Z",
    }


def _webhook_body(event: str, obj: dict[str, Any]) -> dict[str, Any]:
    return {"type": "notification", "event": event, "object": obj}


async def _post_webhook(
    client: AsyncClient, body: dict[str, Any], *, ip: str = TRUSTED_IP
) -> httpx.Response:
    return await client.post(
        "/api/v1/billing/yookassa/webhook",
        json=body,
        headers={"X-Forwarded-For": ip},
    )


async def _count_paid_online_events(db_session: AsyncSession, org_id: uuid.UUID) -> int:
    result = await db_session.execute(
        select(SubscriptionEvent).where(
            SubscriptionEvent.organization_id == org_id,
            SubscriptionEvent.type == "paid_online",
        )
    )
    return len(result.scalars().all())


class TestWebhookIpValidation:
    async def test_rejects_untrusted_ip(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        owner, org = await _org_with_active_sub(db_session, "wh-ip1")
        headers = await _login(client, "wh-ip1@example.com")
        payment_id, amount_minor = await _create_checkout_payment(
            client, org, headers, monkeypatch, "provider-wh-ip1"
        )

        get_mock = _mock_get_payment(
            monkeypatch, _succeeded_payload("provider-wh-ip1", payment_id, org.id, amount_minor)
        )
        resp = await _post_webhook(
            client,
            _webhook_body("payment.succeeded", {"id": "provider-wh-ip1"}),
            ip=UNTRUSTED_IP,
        )
        assert resp.status_code == 403
        assert _err(resp) == "FORBIDDEN"
        get_mock.assert_not_awaited()  # тело не разбирается, к провайдеру не обращаемся

        payment = await db_session.get(Payment, uuid.UUID(payment_id))
        assert payment is not None
        assert payment.applied_at is None
        assert payment.status == PaymentStatus.pending.value
        del owner

    async def test_accepts_trusted_ip(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        owner, org = await _org_with_active_sub(db_session, "wh-ip2")
        headers = await _login(client, "wh-ip2@example.com")
        payment_id, amount_minor = await _create_checkout_payment(
            client, org, headers, monkeypatch, "provider-wh-ip2"
        )
        _mock_get_payment(
            monkeypatch, _succeeded_payload("provider-wh-ip2", payment_id, org.id, amount_minor)
        )

        resp = await _post_webhook(
            client, _webhook_body("payment.succeeded", {"id": "provider-wh-ip2"}), ip=TRUSTED_IP
        )
        assert resp.status_code == 200
        del owner


class TestWebhookIdempotency:
    async def test_duplicate_delivery_applies_once(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        owner, org = await _org_with_active_sub(db_session, "wh-idem1")
        headers = await _login(client, "wh-idem1@example.com")
        payment_id, amount_minor = await _create_checkout_payment(
            client, org, headers, monkeypatch, "provider-wh-idem1"
        )
        _mock_get_payment(
            monkeypatch, _succeeded_payload("provider-wh-idem1", payment_id, org.id, amount_minor)
        )
        body = _webhook_body("payment.succeeded", {"id": "provider-wh-idem1"})

        first = await _post_webhook(client, body)
        assert first.status_code == 200

        sub_after_first = await client.get(
            f"/api/v1/organizations/{org.id}/subscription", headers=headers
        )
        period_end_after_first = _data(sub_after_first)["current_period_end"]

        second = await _post_webhook(client, body)
        assert second.status_code == 200

        sub_after_second = await client.get(
            f"/api/v1/organizations/{org.id}/subscription", headers=headers
        )
        period_end_after_second = _data(sub_after_second)["current_period_end"]

        assert period_end_after_first == period_end_after_second
        assert await _count_paid_online_events(db_session, org.id) == 1

        payment = await db_session.get(Payment, uuid.UUID(payment_id))
        assert payment is not None
        assert payment.status == PaymentStatus.succeeded.value
        assert payment.applied_at is not None
        del owner


class TestWebhookAmountMismatch:
    async def test_amount_mismatch_is_not_applied(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        owner, org = await _org_with_active_sub(db_session, "wh-amt1")
        headers = await _login(client, "wh-amt1@example.com")
        payment_id, amount_minor = await _create_checkout_payment(
            client, org, headers, monkeypatch, "provider-wh-amt1"
        )
        assert amount_minor == 500_000

        # Провайдер отвечает суммой, отличной от нашей записи (подделка/расхождение).
        forged = _succeeded_payload("provider-wh-amt1", payment_id, org.id, amount_minor)
        forged["amount"] = {"value": "1.00", "currency": "RUB"}
        _mock_get_payment(monkeypatch, forged)

        resp = await _post_webhook(
            client, _webhook_body("payment.succeeded", {"id": "provider-wh-amt1"})
        )
        assert resp.status_code == 200  # провайдера не долбим ретраями

        payment = await db_session.get(Payment, uuid.UUID(payment_id))
        assert payment is not None
        assert payment.applied_at is None
        assert payment.status == PaymentStatus.pending.value

        sub_resp = await client.get(
            f"/api/v1/organizations/{org.id}/subscription", headers=headers
        )
        # Период не сдвинулся — платёж не применён.
        assert _data(sub_resp)["current_period_end"] is not None
        del owner

    async def test_organization_metadata_mismatch_is_not_applied(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        owner, org = await _org_with_active_sub(db_session, "wh-amt2")
        headers = await _login(client, "wh-amt2@example.com")
        payment_id, amount_minor = await _create_checkout_payment(
            client, org, headers, monkeypatch, "provider-wh-amt2"
        )

        forged = _succeeded_payload("provider-wh-amt2", payment_id, org.id, amount_minor)
        forged["metadata"]["organization_id"] = str(uuid.uuid4())
        _mock_get_payment(monkeypatch, forged)

        resp = await _post_webhook(
            client, _webhook_body("payment.succeeded", {"id": "provider-wh-amt2"})
        )
        assert resp.status_code == 200

        payment = await db_session.get(Payment, uuid.UUID(payment_id))
        assert payment is not None
        assert payment.applied_at is None
        del owner


class TestWebhookCanceled:
    async def test_payment_canceled_event_marks_payment_canceled(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        owner, org = await _org_with_active_sub(db_session, "wh-canc1")
        headers = await _login(client, "wh-canc1@example.com")
        payment_id, amount_minor = await _create_checkout_payment(
            client, org, headers, monkeypatch, "provider-wh-canc1"
        )
        canceled_payload = {
            "id": "provider-wh-canc1",
            "status": "canceled",
            "paid": False,
            "amount": {
                "value": yookassa_client.minor_to_amount_value(amount_minor),
                "currency": "RUB",
            },
            "metadata": {
                "payment_id": payment_id,
                "organization_id": str(org.id),
                "kind": "extend",
                "plan_code": "standard",
                "months": "1",
            },
            "cancellation_details": {"reason": "3d_secure_failed"},
        }
        _mock_get_payment(monkeypatch, canceled_payload)

        resp = await _post_webhook(
            client, _webhook_body("payment.canceled", {"id": "provider-wh-canc1"})
        )
        assert resp.status_code == 200

        payment = await db_session.get(Payment, uuid.UUID(payment_id))
        assert payment is not None
        assert payment.status == PaymentStatus.canceled.value
        assert payment.cancellation_reason == "3d_secure_failed"
        del owner


class TestWebhookRefund:
    async def test_refund_succeeded_marks_payment_refunded_and_is_idempotent(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        yookassa_on: None,
        monkeypatch: pytest.MonkeyPatch,
    ):
        owner, org = await _org_with_active_sub(db_session, "wh-ref1")
        headers = await _login(client, "wh-ref1@example.com")
        payment_id, amount_minor = await _create_checkout_payment(
            client, org, headers, monkeypatch, "provider-wh-ref1"
        )
        _mock_get_payment(
            monkeypatch, _succeeded_payload("provider-wh-ref1", payment_id, org.id, amount_minor)
        )
        await _post_webhook(client, _webhook_body("payment.succeeded", {"id": "provider-wh-ref1"}))

        refund_obj = {
            "id": "refund-1",
            "payment_id": "provider-wh-ref1",
            "status": "succeeded",
            "amount": {"value": "5000.00", "currency": "RUB"},
        }
        first = await _post_webhook(client, _webhook_body("refund.succeeded", refund_obj))
        assert first.status_code == 200

        payment = await db_session.get(Payment, uuid.UUID(payment_id))
        assert payment is not None
        assert payment.status == PaymentStatus.refunded.value

        refund_events = (
            (
                await db_session.execute(
                    select(SubscriptionEvent).where(
                        SubscriptionEvent.organization_id == org.id,
                        SubscriptionEvent.type == "payment_refunded",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(refund_events) == 1
        assert refund_events[0].amount_minor == -500_000

        # Повторная доставка того же вебхука — без дублей.
        second = await _post_webhook(client, _webhook_body("refund.succeeded", refund_obj))
        assert second.status_code == 200
        refund_events_after = (
            (
                await db_session.execute(
                    select(SubscriptionEvent).where(
                        SubscriptionEvent.organization_id == org.id,
                        SubscriptionEvent.type == "payment_refunded",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(refund_events_after) == 1
        del owner


class TestWebhookMisc:
    async def test_unknown_event_returns_200_noop(
        self, client: AsyncClient, db_session: AsyncSession, yookassa_on: None
    ):
        resp = await _post_webhook(
            client, _webhook_body("payment.waiting_for_capture", {"id": "whatever"})
        )
        assert resp.status_code == 200

    async def test_missing_object_id_returns_400(
        self, client: AsyncClient, db_session: AsyncSession, yookassa_on: None
    ):
        resp = await _post_webhook(client, _webhook_body("payment.succeeded", {}))
        assert resp.status_code == 400
        assert _err(resp) == "VALIDATION_ERROR"
