"""Бизнес-логика подписок (`tariffs`): витрина тарифов, состояние подписки
организации, супер-админские операции (реестр/правка/продление/журнал/сводка).

Ролевые проверки владелец/админ — там, где они нужны (эндпоинты 2 и 3) —
выполняет вызывающий роутер через `services.common.ensure_admin_or_owner` /
`ensure_super_admin`, как и во всех остальных фичах; здесь — только данные.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.organization import Organization, OrganizationMember
from src.app.models.plan import Plan
from src.app.models.subscription import (
    Subscription,
    SubscriptionEvent,
    SubscriptionEventType,
    SubscriptionStatus,
)
from src.app.models.user import User
from src.app.models.work_location import WorkLocation
from src.app.services import entitlements
from src.app.services import organization as org_service
from src.app.services.entitlements import EffectiveStatus, SubscriptionError
from src.app.services.shift import ensure_utc

logger = get_logger(__name__)

_EXTEND_MIN_MONTHS = 1
_EXTEND_MAX_MONTHS = 24


# --- 1. Витрина тарифов -------------------------------------------------------
async def list_plans(session: AsyncSession) -> list[Plan]:
    return await entitlements.list_active_plans(session)


# --- 2/3. Состояние подписки организации (общая полезная нагрузка) -----------
async def build_subscription_payload(session: AsyncSession, org_id: uuid.UUID) -> dict[str, Any]:
    """Формирует объект из п.2 ТЗ. `plan_code`/`plan_name`/`price_minor`/
    `currency` — СТОРОННИЙ (сохранённый) план организации; `limits`/`features`
    — ЭФФЕКТИВНЫЕ (в `trialing` — от `premium`, независимо от `plan_code`)."""
    sub = await entitlements.get_subscription(session, org_id)
    status = entitlements.compute_effective_status(sub)
    stored_plan = await entitlements.get_plan(session, sub.plan_code)
    effective_plan = await entitlements.get_effective_plan(session, sub, status)
    employees = await entitlements.count_employees(session, org_id)
    locations = await entitlements.count_locations(session, org_id)

    return {
        "plan_code": stored_plan.code,
        "plan_name": stored_plan.name,
        "status": status.value,
        "trial_ends_at": sub.trial_ends_at,
        "current_period_start": sub.current_period_start,
        "current_period_end": sub.current_period_end,
        "grace_ends_at": entitlements.grace_ends_at(sub),
        "days_left": entitlements.days_left(sub),
        "is_read_only": status in entitlements.READ_ONLY_STATUSES,
        "limits": {
            "max_employees": effective_plan.max_employees,
            "max_locations": effective_plan.max_locations,
        },
        "usage": {"employees": employees, "locations": locations},
        "features": {
            "fines": effective_plan.feature_fines,
            "test_import": effective_plan.feature_test_import,
        },
        "price_minor": stored_plan.price_minor,
        "currency": stored_plan.currency,
    }


# --- 4. Реестр подписок (super_admin) -----------------------------------------
@dataclass(frozen=True, slots=True)
class AdminSubscriptionRow:
    org: Organization
    owner_email: str
    owner_login: str | None
    sub: Subscription
    plan: Plan  # сохранённый план организации (Subscription.plan_code), НЕ эффективный
    # эффективный план — как в limits/features п.2 ТЗ: в trialing это premium
    # независимо от plan.code
    effective_plan: Plan
    status: EffectiveStatus
    employees: int
    locations: int


async def _usage_maps(
    session: AsyncSession, org_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    if not org_ids:
        return {}, {}
    employees_result = await session.execute(
        select(OrganizationMember.organization_id, func.count(OrganizationMember.id))
        .where(OrganizationMember.organization_id.in_(org_ids))
        .group_by(OrganizationMember.organization_id)
    )
    employees_map = dict(employees_result.tuples().all())
    locations_result = await session.execute(
        select(WorkLocation.organization_id, func.count(WorkLocation.id))
        .where(WorkLocation.organization_id.in_(org_ids))
        .group_by(WorkLocation.organization_id)
    )
    locations_map = dict(locations_result.tuples().all())
    return employees_map, locations_map


def _registry_sort_key(row: AdminSubscriptionRow, sort: str | None, now: datetime) -> Any:
    if sort == "organization_name":
        return (0, row.org.name.lower())
    if sort == "current_period_end":
        ref = row.sub.current_period_end
        return (0, ref) if ref is not None else (1, now)
    # default: ближайшее окончание сверху (trial_ends_at для trialing,
    # current_period_end для active/past_due), read-only (suspended/canceled) — в
    # конец. `period_reference()` для suspended, наступившего по датам (а не через
    # canceled), НЕ обнуляется в None — та же дата нужна `grace_ends_at`/событию
    # `auto_suspended` в `tasks/subscriptions.py`. Поэтому здесь статус проверяем
    # явно, а не полагаемся на None (иначе suspended-организации с давно
    # истёкшей датой всплывали бы выше объектов, которые истекают через пару
    # дней — см. code-review).
    if row.status in entitlements.READ_ONLY_STATUSES:
        return (1, now)
    ref = entitlements.period_reference(row.sub, now)
    return (0, ref) if ref is not None else (1, now)


async def list_admin_subscriptions(
    session: AsyncSession,
    *,
    statuses: list[str] | None = None,
    plan_code: str | None = None,
    q: str | None = None,
    expiring_soon: bool = False,
    limit: int = 20,
    offset: int = 0,
    sort: str | None = None,
) -> tuple[list[AdminSubscriptionRow], int]:
    """`expiring_soon=True` фильтрует на всей выборке (до пагинации) по
    `entitlements.is_expiring_soon` — тому же предикату, что даёт
    `expiring_in_7_days` в `get_summary`, чтобы фильтр реестра и счётчик
    сводки никогда не расходились."""
    query = (
        select(Organization, User.email, User.login, Subscription, Plan)
        .join(Subscription, Subscription.organization_id == Organization.id)
        .join(Plan, Plan.code == Subscription.plan_code)
        .join(User, User.id == Organization.owner_id)
        .where(Organization.is_deleted.is_(False))
    )
    if plan_code is not None:
        query = query.where(Subscription.plan_code == plan_code)
    if q:
        query = query.where(Organization.name.ilike(f"%{q}%"))

    rows = (await session.execute(query)).all()
    org_ids = [org.id for org, *_ in rows]
    employees_map, locations_map = await _usage_maps(session, org_ids)

    now = datetime.now(UTC)
    built: list[AdminSubscriptionRow] = []
    for org, owner_email, owner_login, sub, plan in rows:
        status = entitlements.compute_effective_status(sub, now)
        effective_plan = await entitlements.get_effective_plan(session, sub, status)
        built.append(
            AdminSubscriptionRow(
                org=org,
                owner_email=owner_email or "",
                owner_login=owner_login,
                sub=sub,
                plan=plan,
                effective_plan=effective_plan,
                status=status,
                employees=employees_map.get(org.id, 0),
                locations=locations_map.get(org.id, 0),
            )
        )

    if statuses:
        status_set = set(statuses)
        built = [b for b in built if b.status.value in status_set]
    if expiring_soon:
        built = [b for b in built if entitlements.is_expiring_soon(b.sub, now)]

    total = len(built)
    built.sort(key=lambda b: _registry_sort_key(b, sort, now))
    return built[offset : offset + limit], total


# --- 5. Ручная правка (super_admin) -------------------------------------------
async def patch_subscription(
    session: AsyncSession,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    plan_code: str | None,
    status: str | None,
    trial_ends_at: datetime | None,
    current_period_start: datetime | None,
    current_period_end: datetime | None,
    note: str | None,
) -> Subscription:
    await org_service.get_organization(session, org_id)  # 404 ORG_NOT_FOUND
    sub = await entitlements.get_subscription(session, org_id)

    from_plan_code = sub.plan_code
    from_status = sub.status
    period_end_before = sub.current_period_end
    plan_changed = False

    if plan_code is not None:
        plan = await entitlements.get_active_plan(session, plan_code)
        plan_changed = plan.code != sub.plan_code
        sub.plan_code = plan.code

    if status is not None:
        valid_statuses = {s.value for s in SubscriptionStatus}
        if status not in valid_statuses:
            raise SubscriptionError(
                "VALIDATION_ERROR",
                f"status должен быть: {', '.join(sorted(valid_statuses))}",
                422,
            )
        sub.status = status

    if trial_ends_at is not None:
        sub.trial_ends_at = ensure_utc(trial_ends_at)
    if current_period_start is not None:
        sub.current_period_start = ensure_utc(current_period_start)
    if current_period_end is not None:
        sub.current_period_end = ensure_utc(current_period_end)
    if note is not None:
        sub.note = note

    if sub.status == SubscriptionStatus.active.value and sub.current_period_end is None:
        raise SubscriptionError(
            "VALIDATION_ERROR",
            "current_period_end обязателен при status=active",
            422,
        )
    if sub.status == SubscriptionStatus.trialing.value and sub.trial_ends_at is None:
        raise SubscriptionError(
            "VALIDATION_ERROR",
            "trial_ends_at обязателен при status=trialing",
            422,
        )

    if (
        status is not None
        or trial_ends_at is not None
        or current_period_start is not None
        or current_period_end is not None
    ):
        # Сброс антидубля Celery-задачи (`tasks/subscriptions.py`): ручная правка
        # дат/статуса могла сдвинуть эффективный статус в любую сторону, старое
        # значение (включая сентинел `0` авто-приостановки) больше не актуально —
        # иначе организация, реактивированная через PATCH (а не extend), могла бы
        # навсегда остаться без уведомлений (см. code-review).
        sub.last_expiry_notice_days = None

    sub.updated_by_user_id = actor_id
    await session.flush()

    event_type = (
        SubscriptionEventType.plan_changed
        if plan_changed
        else SubscriptionEventType.status_changed
    )
    session.add(
        SubscriptionEvent(
            organization_id=org_id,
            type=event_type.value,
            from_plan_code=from_plan_code,
            to_plan_code=sub.plan_code,
            from_status=from_status,
            to_status=sub.status,
            period_end_before=period_end_before,
            period_end_after=sub.current_period_end,
            note=note,
            actor_user_id=actor_id,
        )
    )
    await session.flush()
    logger.info("subscription_patched", org_id=str(org_id), actor_id=str(actor_id))
    return sub


# --- 6. Продление («оплачено») (super_admin) ----------------------------------
async def extend_subscription(
    session: AsyncSession,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    months: int,
    plan_code: str | None,
    amount_minor: int | None,
    note: str | None,
) -> Subscription:
    if not (_EXTEND_MIN_MONTHS <= months <= _EXTEND_MAX_MONTHS):
        raise SubscriptionError(
            "VALIDATION_ERROR",
            f"months должен быть от {_EXTEND_MIN_MONTHS} до {_EXTEND_MAX_MONTHS}",
            422,
        )

    await org_service.get_organization(session, org_id)  # 404 ORG_NOT_FOUND
    sub = await entitlements.get_subscription(session, org_id)

    from_plan_code = sub.plan_code
    from_status = sub.status
    period_end_before = sub.current_period_end

    plan_changed = False
    if plan_code is not None:
        target_plan = await entitlements.get_active_plan(session, plan_code)
        plan_changed = target_plan.code != sub.plan_code
    else:
        target_plan = await entitlements.get_plan(session, sub.plan_code)

    now = datetime.now(UTC)
    reference = sub.current_period_end if sub.current_period_end is not None else sub.trial_ends_at
    base = max(now, reference) if reference is not None else now
    new_period_end = entitlements.add_months(base, months)

    if sub.current_period_start is None:
        sub.current_period_start = now

    final_amount = amount_minor if amount_minor is not None else target_plan.price_minor * months

    sub.plan_code = target_plan.code
    sub.status = SubscriptionStatus.active.value
    sub.current_period_end = new_period_end
    sub.last_expiry_notice_days = None
    if note is not None:
        sub.note = note
    sub.updated_by_user_id = actor_id
    await session.flush()

    event_type = (
        SubscriptionEventType.plan_changed if plan_changed else SubscriptionEventType.extended
    )
    session.add(
        SubscriptionEvent(
            organization_id=org_id,
            type=event_type.value,
            from_plan_code=from_plan_code,
            to_plan_code=sub.plan_code,
            from_status=from_status,
            to_status=sub.status,
            period_end_before=period_end_before,
            period_end_after=sub.current_period_end,
            months=months,
            amount_minor=final_amount,
            note=note,
            actor_user_id=actor_id,
        )
    )
    await session.flush()
    logger.info(
        "subscription_extended",
        org_id=str(org_id),
        actor_id=str(actor_id),
        months=months,
        amount_minor=final_amount,
    )
    return sub


# --- 7. История подписки (super_admin) ----------------------------------------
async def list_events(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[SubscriptionEvent], int, dict[uuid.UUID, User]]:
    await org_service.get_organization(session, org_id)  # 404 ORG_NOT_FOUND

    total = (
        await session.execute(
            select(func.count())
            .select_from(SubscriptionEvent)
            .where(SubscriptionEvent.organization_id == org_id)
        )
    ).scalar_one()

    result = await session.execute(
        select(SubscriptionEvent)
        .where(SubscriptionEvent.organization_id == org_id)
        .order_by(SubscriptionEvent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    events = list(result.scalars().all())

    actor_ids = {e.actor_user_id for e in events if e.actor_user_id is not None}
    actors: dict[uuid.UUID, User] = {}
    if actor_ids:
        users_result = await session.execute(select(User).where(User.id.in_(actor_ids)))
        actors = {u.id: u for u in users_result.scalars().all()}
    return events, total, actors


# --- 8. Сводка по монетизации (super_admin) -----------------------------------
async def get_summary(session: AsyncSession) -> dict[str, Any]:
    query = (
        select(Organization.id, Subscription, Plan.price_minor)
        .join(Subscription, Subscription.organization_id == Organization.id)
        .join(Plan, Plan.code == Subscription.plan_code)
        .where(Organization.is_deleted.is_(False))
    )
    rows = (await session.execute(query)).all()

    now = datetime.now(UTC)
    by_status: dict[str, int] = {s.value: 0 for s in EffectiveStatus}
    by_plan: dict[str, int] = {}
    mrr_minor = 0
    expiring_in_7_days = 0

    for _org_id, sub, price_minor in rows:
        status = entitlements.compute_effective_status(sub, now)
        by_status[status.value] += 1
        by_plan[sub.plan_code] = by_plan.get(sub.plan_code, 0) + 1
        if status == EffectiveStatus.active:
            mrr_minor += price_minor
        if entitlements.is_expiring_soon(sub, now):
            expiring_in_7_days += 1

    return {
        "by_status": by_status,
        "by_plan": by_plan,
        "mrr_minor": mrr_minor,
        "expiring_in_7_days": expiring_in_7_days,
    }
