"""Слой энтайтлментов (`tariffs`): роль отвечает «кому можно», этот модуль —
«что оплачено». Архитектурная рамка — ADR-004 `docs/decisions/
004-access-role-times-plan.md`.

Три функции ниже — то, что ТЗ называет «зависимостями FastAPI»
(`require_active_subscription`, `require_feature`, `require_capacity`).
Реализованы как обычные асинхронные функции сервисного слоя, а не как
буквальный FastAPI `Depends()` — сознательное отступление от формулировки ТЗ:

Все ролевые проверки в этой кодовой базе (`ensure_owner`/`ensure_admin_or_
owner`/`ensure_member` из `services/common.py`) уже живут в сервисном слое, а
не как `Depends()` в роутерах. Обязательный порядок проверок ADR-004 —
`401 → 403 (роль) → 402 (подписка) → 402 (лимит/фича)` — критичен для
безопасности: сотруднику без доступа нельзя сообщать, что «эта функция
доступна на Премиуме» (см. `backend.md`, «Порядок проверок фиксирован»).
Если бы эти три проверки были обычными `Depends()` в сигнатуре эндпоинта,
FastAPI резолвил бы их ДО тела хендлера — то есть ДО ролевой проверки внутри
сервиса, и порядок 403→402 ломался бы (утечка факта неактивной подписки
пользователю, которому и так нельзя). Поэтому все три функции вызываются из
сервисного слоя СРАЗУ ПОСЛЕ существующей ролевой проверки — единая точка
правды сохраняется, просто не в виде `Depends()`.

Тариф никогда не проверяется раньше роли — вызывающий код обязан соблюдать
порядок (см. docstring каждой функции).
"""

import enum
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.models.organization import Organization, OrganizationMember
from src.app.models.plan import Plan
from src.app.models.subscription import (
    Subscription,
    SubscriptionEvent,
    SubscriptionEventType,
    SubscriptionStatus,
)
from src.app.models.work_location import WorkLocation
from src.app.services.common import is_super_admin

GRACE_DAYS = 7
PREMIUM_PLAN_CODE = "premium"


class EffectiveStatus(enum.StrEnum):
    """Пять значений, которые всегда отдаются в API (backend.md, «Эффективный
    статус — производная, а не колонка»)."""

    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    suspended = "suspended"
    canceled = "canceled"


READ_ONLY_STATUSES = frozenset({EffectiveStatus.suspended, EffectiveStatus.canceled})


class PlanFeature(enum.StrEnum):
    """Единственный источник правды по платным фичам — колонки `plans.feature_*`
    соответствуют этому перечислению один в один (проверяется тестом
    `test_plan_feature_matches_plans_columns`)."""

    fines = "fines"
    test_import = "test_import"


# code → колонка `plans.feature_*`, читается через getattr(plan, ...).
FEATURE_COLUMNS: dict[PlanFeature, str] = {
    PlanFeature.fines: "feature_fines",
    PlanFeature.test_import: "feature_test_import",
}

_FEATURE_LABELS: dict[PlanFeature, str] = {
    PlanFeature.fines: "Штрафы",
    PlanFeature.test_import: "Импорт теста из JSON",
}


class LimitKind(enum.StrEnum):
    employees = "employees"
    locations = "locations"


_LIMIT_LABELS: dict[LimitKind, str] = {
    LimitKind.employees: "сотрудников",
    LimitKind.locations: "рабочих точек",
}


class SubscriptionError(Exception):
    """Маппится в {data,error} в main.py. `status_code` по умолчанию 402 —
    подавляющее большинство ошибок этого модуля именно тарифные; 404
    (`SUBSCRIPTION_NOT_FOUND`/`PLAN_NOT_FOUND`) передаётся явно."""

    def __init__(self, code: str, message: str, status_code: int = 402):
        self.code = code
        self.message = message
        self.status_code = status_code


# --- Эффективный статус: чистая функция от дат -------------------------------
@dataclass(frozen=True, slots=True)
class _Resolution:
    status: EffectiveStatus
    # Дата, от которой отсчитывается grace/дни (None для canceled и для
    # некорректных данных без обеих дат).
    reference: datetime | None


def _resolve(sub: Subscription, now: datetime) -> _Resolution:
    if sub.status == SubscriptionStatus.canceled.value:
        return _Resolution(EffectiveStatus.canceled, None)

    reference: datetime | None
    if sub.status == SubscriptionStatus.trialing.value:
        reference = sub.trial_ends_at
        if reference is not None and now <= reference:
            return _Resolution(EffectiveStatus.trialing, reference)
    elif sub.status == SubscriptionStatus.active.value:
        reference = sub.current_period_end
        if reference is not None and now <= reference:
            return _Resolution(EffectiveStatus.active, reference)
    else:
        reference = None

    if reference is not None and now <= reference + timedelta(days=GRACE_DAYS):
        return _Resolution(EffectiveStatus.past_due, reference)
    return _Resolution(EffectiveStatus.suspended, reference)


def compute_effective_status(sub: Subscription, now: datetime | None = None) -> EffectiveStatus:
    """`effective_status(sub, now)` из backend.md — единственный источник
    правды, границы (последняя секунда триала/первый день grace/конец grace)
    инклюзивны по `now <= reference[+ GRACE_DAYS]`."""
    return _resolve(sub, now or datetime.now(UTC)).status


def grace_ends_at(sub: Subscription, now: datetime | None = None) -> datetime | None:
    """`period_reference + GRACE_DAYS`; `None` для `canceled` и когда обе даты пусты."""
    res = _resolve(sub, now or datetime.now(UTC))
    if res.reference is None:
        return None
    return res.reference + timedelta(days=GRACE_DAYS)


def days_left(sub: Subscription, now: datetime | None = None) -> int | None:
    """Целое число полных суток до `reference` (отрицательное в `past_due`,
    `None` в `suspended`/`canceled`). `timedelta.days` — математический floor,
    единая формула для положительной и отрицательной разницы."""
    moment = now or datetime.now(UTC)
    res = _resolve(sub, moment)
    if res.status in READ_ONLY_STATUSES or res.reference is None:
        return None
    return (res.reference - moment).days


def period_reference(sub: Subscription, now: datetime | None = None) -> datetime | None:
    """Дата, релевантная для сортировки/представления «когда истекает»:
    `trial_ends_at` для `trialing`, `current_period_end` для `active`/
    `past_due`, `None` для `suspended`/`canceled` (реестр подписок super_admin,
    сортировка по умолчанию — ближайшее окончание сверху)."""
    return _resolve(sub, now or datetime.now(UTC)).reference


# --- Доступ к БД: планы, подписка, usage --------------------------------------
async def get_plan(session: AsyncSession, plan_code: str) -> Plan:
    plan = await session.get(Plan, plan_code)
    if plan is None:
        raise SubscriptionError("PLAN_NOT_FOUND", "Тариф не найден", 404)
    return plan


async def get_active_plan(session: AsyncSession, plan_code: str) -> Plan:
    """Для назначения плана организации (PATCH/extend) — план должен
    существовать И быть активным (`is_active=true`)."""
    plan = await get_plan(session, plan_code)
    if not plan.is_active:
        raise SubscriptionError("PLAN_NOT_FOUND", "Тариф недоступен для назначения", 404)
    return plan


async def list_active_plans(session: AsyncSession) -> list[Plan]:
    result = await session.execute(
        select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort_order)
    )
    return list(result.scalars().all())


async def _get_subscription_or_none(
    session: AsyncSession, org_id: uuid.UUID
) -> Subscription | None:
    result = await session.execute(
        select(Subscription).where(Subscription.organization_id == org_id)
    )
    return result.scalar_one_or_none()


async def get_subscription(session: AsyncSession, org_id: uuid.UUID) -> Subscription:
    sub = await _get_subscription_or_none(session, org_id)
    if sub is None:
        raise SubscriptionError("SUBSCRIPTION_NOT_FOUND", "Подписка организации не найдена", 404)
    return sub


async def get_effective_plan(
    session: AsyncSession,
    sub: Subscription,
    effective_status: EffectiveStatus | None = None,
) -> Plan:
    """В `trialing` фичи/лимиты берутся от `premium` независимо от `plan_code`."""
    status = effective_status if effective_status is not None else compute_effective_status(sub)
    plan_code = PREMIUM_PLAN_CODE if status == EffectiveStatus.trialing else sub.plan_code
    return await get_plan(session, plan_code)


async def count_employees(session: AsyncSession, org_id: uuid.UUID) -> int:
    """`admin` + `employee`. Owner не считается — он не member (ADR-001)."""
    result = await session.execute(
        select(func.count())
        .select_from(OrganizationMember)
        .where(OrganizationMember.organization_id == org_id)
    )
    return result.scalar_one()


async def count_locations(session: AsyncSession, org_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(WorkLocation)
        .where(WorkLocation.organization_id == org_id)
    )
    return result.scalar_one()


async def _usage_for(session: AsyncSession, org_id: uuid.UUID, kind: LimitKind) -> int:
    if kind == LimitKind.employees:
        return await count_employees(session, org_id)
    return await count_locations(session, org_id)


def _limit_for(plan: Plan, kind: LimitKind) -> int | None:
    return plan.max_employees if kind == LimitKind.employees else plan.max_locations


# --- Три проверки-«зависимости» -----------------------------------------------
async def require_active_subscription(
    session: AsyncSession,
    org: Organization,
    actor_id: uuid.UUID,
) -> None:
    """402 `SUBSCRIPTION_INACTIVE`, если организация в read-only
    (эффективный статус `suspended`/`canceled`).

    ВЫЗЫВАТЬ СТРОГО ПОСЛЕ ролевой проверки (`ensure_owner`/`ensure_admin_or_
    owner`/...) в сервисе — сама эта функция роль не проверяет (см. docstring
    модуля про порядок 403→402).

    `super_admin` — единственное исключение из read-only (backend.md,
    «Любые операции super_admin» + `docs/BILLING.md` §7): он и есть тот, кто
    возвращает организации доступ.

    Организация без подписки вообще (нарушение инварианта «подписка создаётся
    вместе с организацией» — в проде не должно происходить: автосоздание +
    data-миграция гарантируют строку на каждую активную org) — fail-open, без
    ограничения: отсутствие подписки не должно превращаться в отказ всей
    организации в обслуживании из-за бага в провижининге.
    """
    if await is_super_admin(session, actor_id):
        return
    sub = await _get_subscription_or_none(session, org.id)
    if sub is None:
        return
    status = compute_effective_status(sub)
    if status in READ_ONLY_STATUSES:
        raise SubscriptionError(
            "SUBSCRIPTION_INACTIVE",
            "Организация в режиме только для чтения — подписка не активна",
            402,
        )


async def require_feature(
    session: AsyncSession,
    org: Organization,
    feature: PlanFeature,
) -> None:
    """402 `PLAN_FEATURE_UNAVAILABLE`, если тариф организации (эффективный —
    в `trialing` premium) не включает `feature`. Роль уже должна быть
    проверена вызывающим кодом (см. docstring модуля). Организация без
    подписки — fail-open (см. `require_active_subscription`)."""
    sub = await _get_subscription_or_none(session, org.id)
    if sub is None:
        return
    status = compute_effective_status(sub)
    plan = await get_effective_plan(session, sub, status)
    if not getattr(plan, FEATURE_COLUMNS[feature]):
        raise SubscriptionError(
            "PLAN_FEATURE_UNAVAILABLE",
            f"{_FEATURE_LABELS[feature]} доступны на тарифе Премиум",
            402,
        )


async def require_capacity(
    session: AsyncSession,
    org: Organization,
    kind: LimitKind,
) -> None:
    """402 `PLAN_LIMIT_REACHED`, если `usage >= limit` эффективного плана.
    `limit is None` — без лимита (Премиум). Роль уже должна быть проверена
    вызывающим кодом (см. docstring модуля). Организация без подписки —
    fail-open (см. `require_active_subscription`)."""
    sub = await _get_subscription_or_none(session, org.id)
    if sub is None:
        return
    status = compute_effective_status(sub)
    plan = await get_effective_plan(session, sub, status)
    limit = _limit_for(plan, kind)
    if limit is None:
        return
    usage = await _usage_for(session, org.id, kind)
    if usage >= limit:
        raise SubscriptionError(
            "PLAN_LIMIT_REACHED",
            f"В тарифе {plan.name} доступно {limit} {_LIMIT_LABELS[kind]}",
            402,
        )


# --- Автосоздание подписки при создании организации ---------------------------
DEFAULT_TRIAL_DAYS = 14


async def create_subscription_for_org(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    actor_user_id: uuid.UUID | None = None,
    trial_days: int = DEFAULT_TRIAL_DAYS,
) -> Subscription:
    """Автосоздание при `POST /organizations` (backend.md, «Создание
    подписки»): `premium`/`trialing`/14 дней. Пишет событие `created` в
    журнал (каждое изменение подписки должно там отражаться, backend.md
    «Приёмка»). Не коммитит — вызывающий код (тот же, что создаёт саму
    организацию) сам управляет транзакцией."""
    now = datetime.now(UTC)
    trial_ends_at = now + timedelta(days=trial_days)
    sub = Subscription(
        organization_id=org_id,
        plan_code=PREMIUM_PLAN_CODE,
        status=SubscriptionStatus.trialing.value,
        trial_ends_at=trial_ends_at,
    )
    session.add(sub)
    await session.flush()

    session.add(
        SubscriptionEvent(
            organization_id=org_id,
            type=SubscriptionEventType.created.value,
            to_plan_code=PREMIUM_PLAN_CODE,
            to_status=SubscriptionStatus.trialing.value,
            period_end_after=trial_ends_at,
            actor_user_id=actor_user_id,
        )
    )
    await session.flush()
    return sub
