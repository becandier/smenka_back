"""Фича tariffs: эндпоинты 1–8 (витрина тарифов, состояние подписки, admin)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.subscription import Subscription
from src.app.models.user import User


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


async def _create_org_via_api(client: AsyncClient, super_admin_headers: dict[str, str]) -> str:
    resp = await client.post(
        "/api/v1/organizations", headers=super_admin_headers, json={"name": "API Org"}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


def _data(resp: Any) -> Any:
    return resp.json()["data"]


def _err(resp: Any) -> str:
    return resp.json()["error"]["code"]


class TestPlansEndpoint:
    async def test_list_plans_returns_two_active_plans_sorted(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ):
        resp = await client.get("/api/v1/plans", headers=auth_headers)
        assert resp.status_code == 200
        items = _data(resp)["items"]
        assert [i["code"] for i in items] == ["standard", "premium"]
        standard = items[0]
        assert standard["name"] == "Стандарт"
        assert standard["price_minor"] == 500000
        assert standard["currency"] == "RUB"
        assert standard["limits"] == {"max_employees": 15, "max_locations": 3}
        assert standard["features"] == {"fines": False, "test_import": False}
        premium = items[1]
        assert premium["limits"] == {"max_employees": None, "max_locations": None}
        assert premium["features"] == {"fines": True, "test_import": True}

    async def test_plans_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/plans")
        assert resp.status_code == 401


class TestOrganizationSubscriptionEndpoint:
    async def test_auto_created_on_org_creation_is_premium_trialing_14_days(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        resp = await client.get(
            f"/api/v1/organizations/{org_id}/subscription", headers=super_admin_headers
        )
        assert resp.status_code == 200, resp.text
        data = _data(resp)
        assert data["plan_code"] == "premium"
        assert data["status"] == "trialing"
        assert data["is_read_only"] is False
        assert data["limits"] == {"max_employees": None, "max_locations": None}
        assert data["features"] == {"fines": True, "test_import": True}
        assert data["days_left"] in (13, 14)
        trial_ends_at = datetime.fromisoformat(data["trial_ends_at"])
        assert 13 <= (trial_ends_at - datetime.now(UTC)).days <= 14

    async def test_employee_cannot_view_subscription(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        org_resp = await client.get(f"/api/v1/organizations/{org_id}", headers=super_admin_headers)
        invite_code = _data(org_resp)["invite_code"]

        emp = await _make_user(db_session, "sub-emp@example.com", "Employee")
        emp_headers = await _login(client, "sub-emp@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=emp_headers)

        resp = await client.get(
            f"/api/v1/organizations/{org_id}/subscription", headers=emp_headers
        )
        assert resp.status_code == 403
        del emp

    async def test_additive_field_populated_for_owner_null_for_employee(
        self, client: AsyncClient, db_session: AsyncSession, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)

        owner_resp = await client.get(
            f"/api/v1/organizations/{org_id}", headers=super_admin_headers
        )
        assert owner_resp.status_code == 200
        assert _data(owner_resp)["subscription"] is not None
        assert _data(owner_resp)["subscription"]["plan_code"] == "premium"

        invite_code = _data(owner_resp)["invite_code"]
        await _make_user(db_session, "additive-emp@example.com", "Employee")
        emp_headers = await _login(client, "additive-emp@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=emp_headers)

        emp_view = await client.get(f"/api/v1/organizations/{org_id}", headers=emp_headers)
        assert emp_view.status_code == 200
        assert _data(emp_view)["subscription"] is None
        # Существующие поля контракта не пострадали (additive-проверка).
        assert "id" in _data(emp_view)
        assert "invite_code" in _data(emp_view)


class TestAdminSubscriptionEndpoints:
    async def test_registry_lists_organization_and_excludes_deleted(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        resp = await client.get("/api/v1/admin/subscriptions", headers=super_admin_headers)
        assert resp.status_code == 200
        items = _data(resp)["items"]
        assert any(i["organization_id"] == org_id for i in items)
        row = next(i for i in items if i["organization_id"] == org_id)
        assert row["status"] == "trialing"
        assert row["plan_code"] == "premium"
        assert "usage" in row
        assert row["usage"] == {"employees": 0, "locations": 0}

        await client.delete(f"/api/v1/organizations/{org_id}", headers=super_admin_headers)
        resp2 = await client.get("/api/v1/admin/subscriptions", headers=super_admin_headers)
        assert not any(i["organization_id"] == org_id for i in _data(resp2)["items"])

    async def test_registry_filters_by_status(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        resp = await client.get(
            "/api/v1/admin/subscriptions",
            headers=super_admin_headers,
            params={"status": "active"},
        )
        assert resp.status_code == 200
        assert not any(i["organization_id"] == org_id for i in _data(resp)["items"])

    async def test_patch_requires_current_period_end_for_active(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        resp = await client.patch(
            f"/api/v1/admin/organizations/{org_id}/subscription",
            headers=super_admin_headers,
            json={"status": "active"},
        )
        assert resp.status_code == 422
        assert _err(resp) == "VALIDATION_ERROR"

    async def test_patch_requires_trial_ends_at_for_trialing(
        self, client: AsyncClient, super_admin_headers: dict[str, str], db_session: AsyncSession
    ):
        """code-review: подписки, засеянные data-миграцией `cd0999882d68`, имеют
        `status=active`/`trial_ends_at=NULL` — без этой проверки PATCH в
        `trialing` без даты тихо переводил бы организацию в `suspended`
        (read-only) вместо ожидаемого триала."""
        org_id = await _create_org_via_api(client, super_admin_headers)
        sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == uuid.UUID(org_id))
            )
        ).scalar_one()
        sub.trial_ends_at = None
        sub.status = "active"
        sub.current_period_end = datetime.now(UTC) + timedelta(days=30)
        await db_session.commit()

        resp = await client.patch(
            f"/api/v1/admin/organizations/{org_id}/subscription",
            headers=super_admin_headers,
            json={"status": "trialing"},
        )
        assert resp.status_code == 422
        assert _err(resp) == "VALIDATION_ERROR"

    async def test_patch_reactivation_resets_notification_antidupe_sentinel(
        self, client: AsyncClient, super_admin_headers: dict[str, str], db_session: AsyncSession
    ):
        """code-review: PATCH-реактивация (в отличие от extend) раньше не сбрасывала
        `last_expiry_notice_days`, из-за чего организация, авто-приостановленная
        Celery-задачей (сентинел 0) и затем вручную реактивированная через PATCH
        (не extend), навсегда переставала получать любые тарифные уведомления."""
        org_id = await _create_org_via_api(client, super_admin_headers)
        sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == uuid.UUID(org_id))
            )
        ).scalar_one()
        sub.last_expiry_notice_days = 0  # сентинел авто-приостановки
        await db_session.commit()

        resp = await client.patch(
            f"/api/v1/admin/organizations/{org_id}/subscription",
            headers=super_admin_headers,
            json={
                "status": "active",
                "current_period_end": (datetime.now(UTC) + timedelta(days=5)).isoformat(),
            },
        )
        assert resp.status_code == 200, resp.text

        await db_session.refresh(sub)
        assert sub.last_expiry_notice_days is None

    async def test_patch_plan_not_found(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        resp = await client.patch(
            f"/api/v1/admin/organizations/{org_id}/subscription",
            headers=super_admin_headers,
            json={"plan_code": "does-not-exist"},
        )
        assert resp.status_code == 404
        assert _err(resp) == "PLAN_NOT_FOUND"

    async def test_patch_org_not_found(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        resp = await client.patch(
            f"/api/v1/admin/organizations/{uuid.uuid4()}/subscription",
            headers=super_admin_headers,
            json={"note": "x"},
        )
        assert resp.status_code == 404
        assert _err(resp) == "ORG_NOT_FOUND"

    async def test_patch_sets_plan_and_writes_event(
        self, client: AsyncClient, super_admin_headers: dict[str, str], db_session: AsyncSession
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        resp = await client.patch(
            f"/api/v1/admin/organizations/{org_id}/subscription",
            headers=super_admin_headers,
            json={
                "plan_code": "standard",
                "status": "active",
                "current_period_end": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                "note": "Оплачено вручную",
            },
        )
        assert resp.status_code == 200, resp.text
        data = _data(resp)
        assert data["plan_code"] == "standard"
        assert data["status"] == "active"

        events_resp = await client.get(
            f"/api/v1/admin/organizations/{org_id}/subscription/events",
            headers=super_admin_headers,
        )
        events = _data(events_resp)["items"]
        assert events[0]["type"] == "plan_changed"
        assert events[0]["to_plan_code"] == "standard"
        assert events[0]["actor"]["email"] is not None

    async def test_extend_advances_period_and_resets_antidupe(
        self, client: AsyncClient, super_admin_headers: dict[str, str], db_session: AsyncSession
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == uuid.UUID(org_id))
            )
        ).scalar_one()
        sub.last_expiry_notice_days = 3
        await db_session.commit()

        resp = await client.post(
            f"/api/v1/admin/organizations/{org_id}/subscription/extend",
            headers=super_admin_headers,
            json={"months": 1, "plan_code": "premium"},
        )
        assert resp.status_code == 200, resp.text
        data = _data(resp)
        assert data["status"] == "active"
        assert data["current_period_end"] is not None
        assert data["price_minor"] == 1000000

        await db_session.refresh(sub)
        assert sub.last_expiry_notice_days is None
        assert sub.current_period_start is not None

        events_resp = await client.get(
            f"/api/v1/admin/organizations/{org_id}/subscription/events",
            headers=super_admin_headers,
        )
        events = _data(events_resp)["items"]
        assert events[0]["type"] == "extended"
        assert events[0]["months"] == 1
        assert events[0]["amount_minor"] == 1000000

    async def test_extend_default_amount_is_price_times_months(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        resp = await client.post(
            f"/api/v1/admin/organizations/{org_id}/subscription/extend",
            headers=super_admin_headers,
            json={"months": 3},
        )
        assert resp.status_code == 200
        events_resp = await client.get(
            f"/api/v1/admin/organizations/{org_id}/subscription/events",
            headers=super_admin_headers,
        )
        events = _data(events_resp)["items"]
        assert events[0]["amount_minor"] == 1000000 * 3

    async def test_registry_default_sort_puts_soon_expiring_before_stale_suspended(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        """code-review: `period_reference()` для suspended, наступившего по датам,
        хранит СТАРУЮ дату (нужна аудиту авто-приостановки), а не None — без
        отдельной проверки статуса в сортировке организация, простаивающая уже
        два месяца, всплывала бы выше той, что истекает через два дня."""
        soon_org_id = await _create_org_via_api(client, super_admin_headers)
        await client.patch(
            f"/api/v1/admin/organizations/{soon_org_id}/subscription",
            headers=super_admin_headers,
            json={
                "status": "active",
                "current_period_end": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            },
        )
        stale_org_id = await _create_org_via_api(client, super_admin_headers)
        await client.patch(
            f"/api/v1/admin/organizations/{stale_org_id}/subscription",
            headers=super_admin_headers,
            json={
                "status": "active",
                "current_period_end": (datetime.now(UTC) - timedelta(days=60)).isoformat(),
            },
        )

        resp = await client.get("/api/v1/admin/subscriptions", headers=super_admin_headers)
        assert resp.status_code == 200
        items = _data(resp)["items"]
        stale_row = next(i for i in items if i["organization_id"] == stale_org_id)
        assert stale_row["status"] == "suspended"

        ids = [i["organization_id"] for i in items]
        assert ids.index(soon_org_id) < ids.index(stale_org_id)

    async def test_summary_reflects_active_org_in_mrr(
        self, client: AsyncClient, super_admin_headers: dict[str, str]
    ):
        org_id = await _create_org_via_api(client, super_admin_headers)
        await client.post(
            f"/api/v1/admin/organizations/{org_id}/subscription/extend",
            headers=super_admin_headers,
            json={"months": 1, "plan_code": "standard"},
        )
        resp = await client.get("/api/v1/admin/subscriptions/summary", headers=super_admin_headers)
        assert resp.status_code == 200
        data = _data(resp)
        assert data["by_status"]["active"] >= 1
        assert data["by_plan"].get("standard", 0) >= 1
        assert data["mrr_minor"] >= 500000

    async def test_non_super_admin_forbidden_on_admin_endpoints(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ):
        resp = await client.get("/api/v1/admin/subscriptions", headers=auth_headers)
        assert resp.status_code == 403

    async def test_expiring_soon_finds_trialing_org_with_null_current_period_end(
        self, client: AsyncClient, super_admin_headers: dict[str, str], db_session: AsyncSession
    ):
        """Техдолг backend.md: свежесозданная trialing-организация никогда не
        имеет `current_period_end` (только `trial_ends_at`) — это и был
        исходный баг клиентского фильтра. `expiring_soon` обязан находить её
        по `days_left`, а не спотыкаться о NULL в колонке."""
        org_id = await _create_org_via_api(client, super_admin_headers)
        sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == uuid.UUID(org_id))
            )
        ).scalar_one()
        sub.trial_ends_at = datetime.now(UTC) + timedelta(days=3)
        await db_session.commit()
        assert sub.current_period_end is None

        resp = await client.get(
            "/api/v1/admin/subscriptions",
            headers=super_admin_headers,
            params={"expiring_soon": "true"},
        )
        assert resp.status_code == 200
        items = _data(resp)["items"]
        row = next((i for i in items if i["organization_id"] == org_id), None)
        assert row is not None, "горящий триал с NULL current_period_end не найден фильтром"
        assert row["status"] == "trialing"
        assert row["current_period_end"] is None

    async def test_expiring_soon_excludes_suspended_canceled_and_far_future(
        self, client: AsyncClient, super_admin_headers: dict[str, str], db_session: AsyncSession
    ):
        far_org_id = await _create_org_via_api(client, super_admin_headers)  # trial через 14 дней
        suspended_org_id = await _create_org_via_api(client, super_admin_headers)
        canceled_org_id = await _create_org_via_api(client, super_admin_headers)

        suspended_sub = (
            await db_session.execute(
                select(Subscription).where(
                    Subscription.organization_id == uuid.UUID(suspended_org_id)
                )
            )
        ).scalar_one()
        suspended_sub.status = "active"
        suspended_sub.current_period_end = datetime.now(UTC) - timedelta(days=60)

        canceled_sub = (
            await db_session.execute(
                select(Subscription).where(
                    Subscription.organization_id == uuid.UUID(canceled_org_id)
                )
            )
        ).scalar_one()
        canceled_sub.status = "canceled"
        await db_session.commit()

        resp = await client.get(
            "/api/v1/admin/subscriptions",
            headers=super_admin_headers,
            params={"expiring_soon": "true"},
        )
        assert resp.status_code == 200
        ids = [i["organization_id"] for i in _data(resp)["items"]]
        assert far_org_id not in ids
        assert suspended_org_id not in ids
        assert canceled_org_id not in ids

    async def test_expiring_soon_combines_with_status_and_plan_code_filters(
        self, client: AsyncClient, super_admin_headers: dict[str, str], db_session: AsyncSession
    ):
        trial_org_id = await _create_org_via_api(client, super_admin_headers)
        active_org_id = await _create_org_via_api(client, super_admin_headers)

        trial_sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == uuid.UUID(trial_org_id))
            )
        ).scalar_one()
        trial_sub.trial_ends_at = datetime.now(UTC) + timedelta(days=2)

        active_sub = (
            await db_session.execute(
                select(Subscription).where(
                    Subscription.organization_id == uuid.UUID(active_org_id)
                )
            )
        ).scalar_one()
        active_sub.status = "active"
        active_sub.plan_code = "standard"
        active_sub.current_period_end = datetime.now(UTC) + timedelta(days=5)
        await db_session.commit()

        resp_active = await client.get(
            "/api/v1/admin/subscriptions",
            headers=super_admin_headers,
            params={"expiring_soon": "true", "status": "active"},
        )
        ids_active = [i["organization_id"] for i in _data(resp_active)["items"]]
        assert active_org_id in ids_active
        assert trial_org_id not in ids_active

        resp_trialing = await client.get(
            "/api/v1/admin/subscriptions",
            headers=super_admin_headers,
            params={"expiring_soon": "true", "status": "trialing"},
        )
        ids_trialing = [i["organization_id"] for i in _data(resp_trialing)["items"]]
        assert trial_org_id in ids_trialing
        assert active_org_id not in ids_trialing

        resp_plan = await client.get(
            "/api/v1/admin/subscriptions",
            headers=super_admin_headers,
            params={"expiring_soon": "true", "plan_code": "standard"},
        )
        ids_plan = [i["organization_id"] for i in _data(resp_plan)["items"]]
        assert active_org_id in ids_plan
        assert trial_org_id not in ids_plan

    async def test_expiring_soon_count_matches_summary_expiring_in_7_days(
        self, client: AsyncClient, super_admin_headers: dict[str, str], db_session: AsyncSession
    ):
        """Защита от расхождения: фильтр реестра и счётчик сводки обязаны
        считать одно и то же — оба переиспользуют `entitlements.
        is_expiring_soon`."""
        stale_trial_id = await _create_org_via_api(
            client, super_admin_headers
        )  # 14 дней, не горит
        soon_trial_id = await _create_org_via_api(client, super_admin_headers)
        soon_active_id = await _create_org_via_api(client, super_admin_headers)
        stale_suspended_id = await _create_org_via_api(client, super_admin_headers)

        soon_trial_sub = (
            await db_session.execute(
                select(Subscription).where(
                    Subscription.organization_id == uuid.UUID(soon_trial_id)
                )
            )
        ).scalar_one()
        soon_trial_sub.trial_ends_at = datetime.now(UTC) + timedelta(days=1)

        soon_active_sub = (
            await db_session.execute(
                select(Subscription).where(
                    Subscription.organization_id == uuid.UUID(soon_active_id)
                )
            )
        ).scalar_one()
        soon_active_sub.status = "active"
        soon_active_sub.current_period_end = datetime.now(UTC) + timedelta(days=6)

        stale_suspended_sub = (
            await db_session.execute(
                select(Subscription).where(
                    Subscription.organization_id == uuid.UUID(stale_suspended_id)
                )
            )
        ).scalar_one()
        stale_suspended_sub.status = "active"
        stale_suspended_sub.current_period_end = datetime.now(UTC) - timedelta(days=100)
        await db_session.commit()

        list_resp = await client.get(
            "/api/v1/admin/subscriptions",
            headers=super_admin_headers,
            params={"expiring_soon": "true", "limit": 100},
        )
        assert list_resp.status_code == 200
        list_data = _data(list_resp)
        ids = {i["organization_id"] for i in list_data["items"]}

        summary_resp = await client.get(
            "/api/v1/admin/subscriptions/summary", headers=super_admin_headers
        )
        assert summary_resp.status_code == 200
        summary_data = _data(summary_resp)

        assert ids == {soon_trial_id, soon_active_id}
        assert list_data["total"] == 2
        assert summary_data["expiring_in_7_days"] == 2
        assert list_data["total"] == summary_data["expiring_in_7_days"]
        assert stale_trial_id not in ids
        assert stale_suspended_id not in ids

    async def test_registry_limits_for_trialing_org_are_premium_regardless_of_plan_code(
        self, client: AsyncClient, super_admin_headers: dict[str, str], db_session: AsyncSession
    ):
        """Расхождение из backend.md: `limits` в элементе списка обязаны быть
        ЭФФЕКТИВНЫМИ, как в п.2 ТЗ — в `trialing` от `premium`, даже если у
        подписки сохранён `plan_code=standard` (например, после ручной правки
        супер-админом)."""
        trial_org_id = await _create_org_via_api(client, super_admin_headers)
        trial_sub = (
            await db_session.execute(
                select(Subscription).where(Subscription.organization_id == uuid.UUID(trial_org_id))
            )
        ).scalar_one()
        trial_sub.plan_code = "standard"
        await db_session.commit()
        assert trial_sub.status == "trialing"

        standard_org_id = await _create_org_via_api(client, super_admin_headers)
        await client.post(
            f"/api/v1/admin/organizations/{standard_org_id}/subscription/extend",
            headers=super_admin_headers,
            json={"months": 1, "plan_code": "standard"},
        )

        resp = await client.get("/api/v1/admin/subscriptions", headers=super_admin_headers)
        assert resp.status_code == 200
        items = _data(resp)["items"]

        trial_row = next(i for i in items if i["organization_id"] == trial_org_id)
        assert trial_row["plan_code"] == "standard"
        assert trial_row["status"] == "trialing"
        assert trial_row["limits"] == {"max_employees": None, "max_locations": None}

        standard_row = next(i for i in items if i["organization_id"] == standard_org_id)
        assert standard_row["plan_code"] == "standard"
        assert standard_row["status"] == "active"
        assert standard_row["limits"] == {"max_employees": 15, "max_locations": 3}
