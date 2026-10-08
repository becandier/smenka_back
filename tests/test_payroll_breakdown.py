# tests/test_payroll_breakdown.py
"""Фича payroll_breakdown: категории начислений, разбивка отчёта payroll и Excel."""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.adjustment import PayrollAdjustmentCategory
from src.app.models.audit_log import AuditLog
from src.app.models.member_rate import OrganizationMemberRate, RateType
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.penalty import Penalty
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.shift_overtime_request import OvertimeRequestStatus, ShiftOvertimeRequest
from src.app.models.user import User
from src.app.services import adjustment as adjustment_service
from src.app.services.adjustment import AdjustmentError

RATE_EFF_JAN = datetime(2026, 1, 1, tzinfo=UTC)
JUNE = {"date_from": "2026-06-01T00:00:00Z", "date_to": "2026-06-30T23:59:59Z"}


def _data(resp: Any) -> Any:
    return resp.json()["data"]


def _err(resp: Any) -> str:
    return resp.json()["error"]["code"]


# --- fixtures ------------------------------------------------------------------
async def _make_user(db_session: AsyncSession, email: str, name: str) -> User:
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
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "pb_owner@example.com", "Owner")


@pytest.fixture
async def owner_headers(owner: User, client: AsyncClient) -> dict[str, str]:
    return await _login(client, "pb_owner@example.com")


@pytest.fixture
async def emp2_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "pb_emp2@example.com", "Anna Second")


@pytest.fixture
async def emp2_headers(emp2_user: User, client: AsyncClient) -> dict[str, str]:
    return await _login(client, "pb_emp2@example.com")


@pytest.fixture
async def org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="Breakdown Org", owner_id=owner.id)
    db_session.add(organization)
    await db_session.commit()
    return organization


@pytest.fixture
async def other_org(db_session: AsyncSession, owner: User) -> Organization:
    organization = Organization(name="Other Org", owner_id=owner.id)
    db_session.add(organization)
    await db_session.commit()
    return organization


@pytest.fixture
async def employee_member(
    db_session: AsyncSession, org: Organization, verified_user: User
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id, user_id=verified_user.id, role=MemberRole.employee
    )
    db_session.add(member)
    await db_session.commit()
    return member


@pytest.fixture
async def emp2_member(
    db_session: AsyncSession, org: Organization, emp2_user: User
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org.id, user_id=emp2_user.id, role=MemberRole.employee
    )
    db_session.add(member)
    await db_session.commit()
    return member


async def _make_finished_shift(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    started_at: datetime,
    finished_at: datetime,
) -> Shift:
    shift = Shift(
        user_id=user_id,
        organization_id=org_id,
        started_at=started_at,
        finished_at=finished_at,
        status=ShiftStatus.finished,
    )
    db_session.add(shift)
    await db_session.commit()
    return shift


async def _approve_overtime(db_session: AsyncSession, shift_id: uuid.UUID, minutes: int) -> None:
    db_session.add(
        ShiftOvertimeRequest(
            shift_id=shift_id,
            minutes=minutes,
            comment="Задержался",
            status=OvertimeRequestStatus.approved,
        )
    )
    await db_session.commit()


async def _make_rate(
    db_session: AsyncSession,
    member_id: uuid.UUID,
    amount: int,
    rate_type: RateType = RateType.hourly,
) -> None:
    db_session.add(
        OrganizationMemberRate(
            member_id=member_id,
            rate_amount_minor=amount,
            rate_type=rate_type,
            currency="RUB",
            effective_from=RATE_EFF_JAN,
        )
    )
    await db_session.commit()


async def _make_penalty(
    db_session: AsyncSession,
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    created_by: uuid.UUID,
    amount: int,
    occurred_at: datetime,
    *,
    reason: str = "Опоздание",
    shift_id: uuid.UUID | None = None,
    is_deleted: bool = False,
) -> Penalty:
    penalty = Penalty(
        organization_id=org_id,
        member_id=member_id,
        shift_id=shift_id,
        reason=reason,
        amount_minor=amount,
        occurred_at=occurred_at,
        created_by_user_id=created_by,
        is_deleted=is_deleted,
    )
    db_session.add(penalty)
    await db_session.commit()
    return penalty


async def _create_category(
    client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID, name: str
) -> Any:
    return await client.post(
        f"/api/v1/organizations/{org_id}/adjustment-categories",
        headers=headers,
        json={"name": name},
    )


async def _category_id(
    client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID, name: str
) -> str:
    resp = await _create_category(client, headers, org_id, name)
    assert resp.status_code == 201, resp.text
    return str(_data(resp)["id"])


async def _create_adjustment(
    client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID, **body: Any
) -> Any:
    body.setdefault("reason", "Корректировка")
    body.setdefault("occurred_at", "2026-06-15T10:00:00Z")
    return await client.post(
        f"/api/v1/organizations/{org_id}/adjustments", headers=headers, json=body
    )


async def _adjustment(
    client: AsyncClient, headers: dict[str, str], org_id: uuid.UUID, **body: Any
) -> dict[str, Any]:
    resp = await _create_adjustment(client, headers, org_id, **body)
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = _data(resp)
    return data


# --- CRUD категорий -------------------------------------------------------------
async def test_category_crud_and_sorting(client, owner_headers, org, employee_member):
    base = f"/api/v1/organizations/{org.id}/adjustment-categories"
    resp = await _create_category(client, owner_headers, org.id, "  премия  ")
    assert resp.status_code == 201, resp.text
    bonus = _data(resp)
    assert bonus["name"] == "премия"  # trim
    assert bonus["organization_id"] == str(org.id)
    assert bonus["is_deleted"] is False
    assert bonus["deleted_at"] is None
    assert bonus["adjustments_count"] == 0

    await _category_id(client, owner_headers, org.id, "Компенсация")
    await _category_id(client, owner_headers, org.id, "Аванс")

    resp = await client.get(base, headers=owner_headers)
    assert resp.status_code == 200
    names = [c["name"] for c in _data(resp)["items"]]
    assert names == ["Аванс", "Компенсация", "премия"]  # lower(name)

    # adjustments_count — только неотменённые начисления
    a1 = await _adjustment(
        client,
        owner_headers,
        org.id,
        member_id=str(employee_member.id),
        amount_minor=100,
        category_id=bonus["id"],
    )
    await _adjustment(
        client,
        owner_headers,
        org.id,
        member_id=str(employee_member.id),
        amount_minor=200,
        category_id=bonus["id"],
    )
    await client.delete(
        f"/api/v1/organizations/{org.id}/adjustments/{a1['id']}", headers=owner_headers
    )

    resp = await client.patch(
        f"{base}/{bonus['id']}", headers=owner_headers, json={"name": "Премия"}
    )
    assert resp.status_code == 200, resp.text
    assert _data(resp)["name"] == "Премия"
    assert _data(resp)["adjustments_count"] == 1

    resp = await client.delete(f"{base}/{bonus['id']}", headers=owner_headers)
    assert resp.status_code == 200, resp.text
    assert _data(resp) == {"deleted": True}

    resp = await client.get(base, headers=owner_headers)
    assert [c["name"] for c in _data(resp)["items"]] == ["Аванс", "Компенсация"]

    resp = await client.get(base, headers=owner_headers, params={"include_deleted": "true"})
    deleted = next(c for c in _data(resp)["items"] if c["id"] == bonus["id"])
    assert deleted["is_deleted"] is True
    assert deleted["deleted_at"] is not None
    assert deleted["adjustments_count"] == 1


async def test_category_duplicate_case_insensitive_409(client, owner_headers, org):
    await _category_id(client, owner_headers, org.id, "Премия")
    resp = await _create_category(client, owner_headers, org.id, " ПРЕМИЯ ")
    assert resp.status_code == 409
    assert _err(resp) == "ADJUSTMENT_CATEGORY_DUPLICATE"

    other = await _category_id(client, owner_headers, org.id, "Компенсация")
    resp = await client.patch(
        f"/api/v1/organizations/{org.id}/adjustment-categories/{other}",
        headers=owner_headers,
        json={"name": "премия"},
    )
    assert resp.status_code == 409
    assert _err(resp) == "ADJUSTMENT_CATEGORY_DUPLICATE"

    # переименование в то же имя с другим регистром у самой себя — не дубль
    resp = await client.patch(
        f"/api/v1/organizations/{org.id}/adjustment-categories/{other}",
        headers=owner_headers,
        json={"name": "КОМПЕНСАЦИЯ"},
    )
    assert resp.status_code == 200, resp.text


async def test_category_recreate_after_delete(client, owner_headers, org):
    first = await _category_id(client, owner_headers, org.id, "Премия")
    await client.delete(
        f"/api/v1/organizations/{org.id}/adjustment-categories/{first}", headers=owner_headers
    )
    resp = await _create_category(client, owner_headers, org.id, "премия")
    assert resp.status_code == 201, resp.text
    assert _data(resp)["id"] != first


async def test_category_same_name_in_other_org_allowed(client, owner_headers, org, other_org):
    await _category_id(client, owner_headers, org.id, "Премия")
    resp = await _create_category(client, owner_headers, other_org.id, "Премия")
    assert resp.status_code == 201, resp.text


async def test_category_race_unique_index_409(db_session, org, owner):
    """Гонка: проверка «имя свободно» прошла, но живая запись уже вставлена
    параллельно → нарушение частичного уникального индекса → 409, не 500."""
    db_session.add(
        PayrollAdjustmentCategory(
            organization_id=org.id, name="Премия", created_by_user_id=owner.id
        )
    )
    await db_session.commit()

    original = adjustment_service._ensure_category_name_free

    async def _skip_check(*args: Any, **kwargs: Any) -> None:
        return None

    adjustment_service._ensure_category_name_free = _skip_check  # type: ignore[assignment]
    try:
        with pytest.raises(AdjustmentError) as exc_info:
            await adjustment_service.create_category(db_session, org.id, owner.id, name="ПРЕМИЯ")
    finally:
        adjustment_service._ensure_category_name_free = original  # type: ignore[assignment]
        await db_session.rollback()
    assert exc_info.value.code == "ADJUSTMENT_CATEGORY_DUPLICATE"
    assert exc_info.value.status_code == 409


async def test_category_not_found_cases(client, owner_headers, org, other_org):
    base = f"/api/v1/organizations/{org.id}/adjustment-categories"
    foreign = await _category_id(client, owner_headers, other_org.id, "Чужая")
    for resp in (
        await client.patch(f"{base}/{foreign}", headers=owner_headers, json={"name": "X"}),
        await client.delete(f"{base}/{foreign}", headers=owner_headers),
        await client.patch(f"{base}/{uuid.uuid4()}", headers=owner_headers, json={"name": "X"}),
    ):
        assert resp.status_code == 404
        assert _err(resp) == "ADJUSTMENT_CATEGORY_NOT_FOUND"

    own = await _category_id(client, owner_headers, org.id, "Своя")
    assert (await client.delete(f"{base}/{own}", headers=owner_headers)).status_code == 200
    resp = await client.delete(f"{base}/{own}", headers=owner_headers)
    assert resp.status_code == 404
    assert _err(resp) == "ADJUSTMENT_CATEGORY_NOT_FOUND"
    resp = await client.patch(f"{base}/{own}", headers=owner_headers, json={"name": "Новая"})
    assert resp.status_code == 404
    assert _err(resp) == "ADJUSTMENT_CATEGORY_NOT_FOUND"


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
async def test_category_name_validation_422(client, owner_headers, org, name):
    resp = await _create_category(client, owner_headers, org.id, name)
    assert resp.status_code == 422
    assert _err(resp) == "VALIDATION_ERROR"


async def test_category_name_max_length_ok(client, owner_headers, org):
    resp = await _create_category(client, owner_headers, org.id, "я" * 100)
    assert resp.status_code == 201, resp.text


async def test_category_rbac(
    client, owner_headers, auth_headers, super_admin_headers, org, employee_member
):
    cat = await _category_id(client, owner_headers, org.id, "Премия")
    base = f"/api/v1/organizations/{org.id}/adjustment-categories"
    for headers in (auth_headers, super_admin_headers):
        for resp in (
            await client.get(base, headers=headers),
            await _create_category(client, headers, org.id, "Y"),
            await client.patch(f"{base}/{cat}", headers=headers, json={"name": "Z"}),
            await client.delete(f"{base}/{cat}", headers=headers),
        ):
            assert resp.status_code == 403, resp.text
            assert _err(resp) == "FORBIDDEN"


async def test_category_admin_member_allowed(
    client, db_session, owner_headers, org, emp2_user, emp2_headers
):
    db_session.add(
        OrganizationMember(organization_id=org.id, user_id=emp2_user.id, role=MemberRole.admin)
    )
    await db_session.commit()
    resp = await _create_category(client, emp2_headers, org.id, "Премия")
    assert resp.status_code == 201, resp.text


# --- Категория в ручных начислениях ---------------------------------------------
async def test_adjustment_create_with_category(
    client, owner_headers, org, employee_member, db_session
):
    cat = await _category_id(client, owner_headers, org.id, "Премия")
    data = await _adjustment(
        client,
        owner_headers,
        org.id,
        member_id=str(employee_member.id),
        amount_minor=5000,
        category_id=cat,
    )
    assert data["category_id"] == cat
    assert data["category_name"] == "Премия"

    plain = await _adjustment(
        client, owner_headers, org.id, member_id=str(employee_member.id), amount_minor=100
    )
    assert plain["category_id"] is None
    assert plain["category_name"] is None

    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "adjustment.create",
                AuditLog.resource_id == uuid.UUID(data["id"]),
            )
        )
    ).scalar_one()
    assert audit.summary["category_id"] == cat


async def test_adjustment_create_foreign_or_deleted_category_404(
    client, owner_headers, org, other_org, employee_member
):
    foreign = await _category_id(client, owner_headers, other_org.id, "Чужая")
    deleted = await _category_id(client, owner_headers, org.id, "Удалённая")
    await client.delete(
        f"/api/v1/organizations/{org.id}/adjustment-categories/{deleted}", headers=owner_headers
    )
    for cat in (foreign, deleted, str(uuid.uuid4())):
        resp = await _create_adjustment(
            client,
            owner_headers,
            org.id,
            member_id=str(employee_member.id),
            amount_minor=100,
            category_id=cat,
        )
        assert resp.status_code == 404
        assert _err(resp) == "ADJUSTMENT_CATEGORY_NOT_FOUND"


async def test_adjustment_update_category_set_reset_keep(
    client, owner_headers, org, employee_member, db_session
):
    cat_a = await _category_id(client, owner_headers, org.id, "Премия")
    cat_b = await _category_id(client, owner_headers, org.id, "Компенсация")
    adj = await _adjustment(
        client,
        owner_headers,
        org.id,
        member_id=str(employee_member.id),
        amount_minor=100,
        category_id=cat_a,
    )
    url = f"/api/v1/organizations/{org.id}/adjustments/{adj['id']}"

    # не передано — категория не меняется
    resp = await client.patch(url, headers=owner_headers, json={"reason": "Новая причина"})
    assert resp.status_code == 200, resp.text
    assert _data(resp)["category_id"] == cat_a

    # другая категория
    resp = await client.patch(url, headers=owner_headers, json={"category_id": cat_b})
    assert _data(resp)["category_id"] == cat_b
    assert _data(resp)["category_name"] == "Компенсация"
    audit = (
        (
            await db_session.execute(
                select(AuditLog)
                .where(AuditLog.action == "adjustment.update")
                .order_by(AuditLog.created_at.desc())
            )
        )
        .scalars()
        .first()
    )
    assert audit is not None
    assert audit.summary["changed"]["category_id"] == {"from": cat_a, "to": cat_b}

    # явный null — сброс
    resp = await client.patch(url, headers=owner_headers, json={"category_id": None})
    assert resp.status_code == 200, resp.text
    assert _data(resp)["category_id"] is None
    assert _data(resp)["category_name"] is None


async def test_adjustment_on_deleted_category_stays_valid(
    client, owner_headers, org, employee_member
):
    cat = await _category_id(client, owner_headers, org.id, "Премия")
    other = await _category_id(client, owner_headers, org.id, "Другая")
    adj = await _adjustment(
        client,
        owner_headers,
        org.id,
        member_id=str(employee_member.id),
        amount_minor=100,
        category_id=cat,
    )
    await client.delete(
        f"/api/v1/organizations/{org.id}/adjustment-categories/{cat}", headers=owner_headers
    )
    await client.patch(
        f"/api/v1/organizations/{org.id}/adjustment-categories/{other}",
        headers=owner_headers,
        json={"name": "Другая2"},
    )
    url = f"/api/v1/organizations/{org.id}/adjustments/{adj['id']}"

    # правка другого поля и даже повтор той же (удалённой) категории — валидны
    resp = await client.patch(url, headers=owner_headers, json={"amount_minor": 300})
    assert resp.status_code == 200, resp.text
    assert _data(resp)["category_id"] == cat
    assert _data(resp)["category_name"] == "Премия"
    resp = await client.patch(url, headers=owner_headers, json={"category_id": cat})
    assert resp.status_code == 200, resp.text

    # список показывает имя удалённой категории
    resp = await client.get(f"/api/v1/organizations/{org.id}/adjustments", headers=owner_headers)
    item = next(i for i in _data(resp)["items"] if i["id"] == adj["id"])
    assert item["category_name"] == "Премия"

    # назначить удалённую категорию другому начислению — нельзя
    adj2 = await _adjustment(
        client, owner_headers, org.id, member_id=str(employee_member.id), amount_minor=100
    )
    resp = await client.patch(
        f"/api/v1/organizations/{org.id}/adjustments/{adj2['id']}",
        headers=owner_headers,
        json={"category_id": cat},
    )
    assert resp.status_code == 404
    assert _err(resp) == "ADJUSTMENT_CATEGORY_NOT_FOUND"


async def test_adjustment_rename_category_reflected(client, owner_headers, org, employee_member):
    cat = await _category_id(client, owner_headers, org.id, "Премия")
    adj = await _adjustment(
        client,
        owner_headers,
        org.id,
        member_id=str(employee_member.id),
        amount_minor=100,
        category_id=cat,
    )
    await client.patch(
        f"/api/v1/organizations/{org.id}/adjustment-categories/{cat}",
        headers=owner_headers,
        json={"name": "Бонус"},
    )
    resp = await client.get(f"/api/v1/organizations/{org.id}/adjustments", headers=owner_headers)
    item = next(i for i in _data(resp)["items"] if i["id"] == adj["id"])
    assert item["category_name"] == "Бонус"


async def test_adjustment_list_category_filter(client, owner_headers, org, employee_member):
    cat_a = await _category_id(client, owner_headers, org.id, "Премия")
    cat_b = await _category_id(client, owner_headers, org.id, "Компенсация")
    member = str(employee_member.id)
    a = await _adjustment(
        client, owner_headers, org.id, member_id=member, amount_minor=1, category_id=cat_a
    )
    b = await _adjustment(
        client, owner_headers, org.id, member_id=member, amount_minor=2, category_id=cat_b
    )
    n = await _adjustment(client, owner_headers, org.id, member_id=member, amount_minor=3)
    url = f"/api/v1/organizations/{org.id}/adjustments"

    resp = await client.get(url, headers=owner_headers, params={"category_id": cat_a})
    assert [i["id"] for i in _data(resp)["items"]] == [a["id"]]
    assert _data(resp)["total"] == 1
    resp = await client.get(url, headers=owner_headers, params={"category_id": cat_b})
    assert [i["id"] for i in _data(resp)["items"]] == [b["id"]]
    for token in ("none", "NONE"):
        resp = await client.get(url, headers=owner_headers, params={"category_id": token})
        assert [i["id"] for i in _data(resp)["items"]] == [n["id"]]
    resp = await client.get(url, headers=owner_headers, params={"category_id": "garbage"})
    assert resp.status_code == 422
    assert _err(resp) == "VALIDATION_ERROR"


async def test_my_adjustments_include_category(
    client, owner_headers, auth_headers, org, employee_member
):
    cat = await _category_id(client, owner_headers, org.id, "Премия")
    await _adjustment(
        client,
        owner_headers,
        org.id,
        member_id=str(employee_member.id),
        amount_minor=100,
        category_id=cat,
    )
    await _adjustment(
        client,
        owner_headers,
        org.id,
        member_id=str(employee_member.id),
        amount_minor=-50,
        occurred_at="2026-06-14T10:00:00Z",
    )
    resp = await client.get(f"/api/v1/organizations/{org.id}/my-adjustments", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    items = _data(resp)["items"]
    assert items[0]["category_id"] == cat
    assert items[0]["category_name"] == "Премия"
    assert items[1]["category_id"] is None
    assert items[1]["category_name"] is None
