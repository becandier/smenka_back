"""Бизнес-логика онлайн-оплаты подписки через ЮKassa (`online_payments`).

Ролевые проверки (owner/admin, не employee) выполняет вызывающий роутер через
`services.common.ensure_admin_or_owner` — как и во всех соседних биллинговых
фичах (`services/subscription.py`). Здесь — только расчёты, интеграция с
провайдером и применение платежа.

Эндпоинты `billing/*` НЕ проверяются на `require_active_subscription`
(backend.md, «Read-only режим и оплата») — организация в `suspended` обязана
иметь возможность заплатить, поэтому этот модуль ни разу не вызывает
`entitlements.require_active_subscription`.
"""

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.config import Settings
from src.app.core.logging import get_logger
from src.app.models.billing_period import BillingPeriod
from src.app.models.organization import Organization
from src.app.models.payment import Payment, PaymentKind, PaymentStatus
from src.app.models.plan import Plan
from src.app.models.subscription import Subscription, SubscriptionEvent, SubscriptionEventType
from src.app.models.user import User
from src.app.services import billing_calc, entitlements, yookassa_client
from src.app.services.entitlements import EffectiveStatus
from src.app.services.shift import ensure_utc

logger = get_logger(__name__)

_PENDING_REUSE_WINDOW = timedelta(minutes=15)
_POLL_RECONCILE_AFTER_SECONDS = 10

_UPGRADE_REASON_MESSAGES = {
    "already_premium": "Организация уже на тарифе Премиум",
    "not_applicable": "Апгрейд недоступен в текущем статусе подписки",
    "no_paid_period": "У организации нет оплаченного периода для апгрейда",
}


class PaymentError(Exception):
    """Маппится в {data,error} в main.py."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


# --- 1. Конфигурация модуля ----------------------------------------------------
def get_billing_config(settings: Settings) -> dict[str, Any]:
    return {
        "enabled": settings.yookassa_enabled,
        "mode": settings.yookassa_mode,
        "provider": "yookassa",
    }


def _require_enabled(settings: Settings) -> None:
    if not settings.yookassa_enabled:
        raise PaymentError("BILLING_DISABLED", "Онлайн-оплата подписки сейчас недоступна", 503)


# --- Периоды продажи -------------------------------------------------------------
async def list_active_billing_periods(session: AsyncSession) -> list[BillingPeriod]:
    result = await session.execute(
        select(BillingPeriod)
        .where(BillingPeriod.is_active.is_(True))
        .order_by(BillingPeriod.sort_order)
    )
    return list(result.scalars().all())


async def _get_active_billing_period(session: AsyncSession, months: int | None) -> BillingPeriod:
    if months is None:
        raise PaymentError(
            "VALIDATION_ERROR", "months обязателен для продления (kind=extend)", 422
        )
    period = await session.get(BillingPeriod, months)
    if period is None or not period.is_active:
        raise PaymentError(
            "VALIDATION_ERROR",
            f"Период {months} мес недоступен для оплаты",
            422,
        )
    return period


# --- Апгрейд: доступность + расчёт (общее для витрины и checkout) --------------
def _upgrade_unavailable_reason(sub: Subscription, status: EffectiveStatus) -> str | None:
    if sub.plan_code == entitlements.PREMIUM_PLAN_CODE:
        return "already_premium"
    if status not in (EffectiveStatus.active, EffectiveStatus.past_due):
        return "not_applicable"
    if sub.current_period_end is None:
        return "no_paid_period"
    return None


@dataclass(frozen=True, slots=True)
class UpgradeOption:
    available: bool
    reason: str | None = None
    from_plan_code: str | None = None
    to_plan_code: str | None = None
    to_plan_name: str | None = None
    months_remaining: int | None = None
    amount_minor: int | None = None
    current_period_end: datetime | None = None


async def _build_upgrade_option(
    session: AsyncSession, sub: Subscription, now: datetime
) -> UpgradeOption:
    status = entitlements.compute_effective_status(sub, now)
    reason = _upgrade_unavailable_reason(sub, status)
    if reason is not None:
        return UpgradeOption(available=False, reason=reason)

    current_period_end = sub.current_period_end
    if current_period_end is None:
        # Не должно происходить: `_upgrade_unavailable_reason` уже вернула бы
        # `no_paid_period` выше. Защита от рассинхронизации двух функций,
        # без падения — так же, как fail-open в `entitlements.py`.
        return UpgradeOption(available=False, reason="no_paid_period")

    standard_plan = await entitlements.get_plan(session, "standard")
    premium_plan = await entitlements.get_plan(session, entitlements.PREMIUM_PLAN_CODE)
    months_remaining = billing_calc.compute_upgrade_months_remaining(current_period_end, now)
    amount_minor = billing_calc.compute_upgrade_amount(
        standard_plan.price_minor, premium_plan.price_minor, months_remaining
    )
    return UpgradeOption(
        available=True,
        from_plan_code=standard_plan.code,
        to_plan_code=premium_plan.code,
        to_plan_name=premium_plan.name,
        months_remaining=months_remaining,
        amount_minor=amount_minor,
        current_period_end=current_period_end,
    )


# --- 2. Витрина: что и почём можно оплатить -------------------------------------
async def get_billing_options(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID
) -> dict[str, Any]:
    sub = await entitlements.get_subscription(session, org_id)
    now = datetime.now(UTC)

    plans = await entitlements.list_active_plans(session)
    periods = await list_active_billing_periods(session)

    extend_items: list[dict[str, Any]] = []
    for plan in plans:
        for period in periods:
            calc = billing_calc.compute_extend_amount(
                plan.price_minor, period.months, period.discount_percent
            )
            if calc.amount_minor > settings.billing_max_payment_minor:
                continue
            extend_items.append(
                {
                    "plan_code": plan.code,
                    "plan_name": plan.name,
                    "months": period.months,
                    "base_amount_minor": calc.base_amount_minor,
                    "discount_percent": calc.discount_percent,
                    "amount_minor": calc.amount_minor,
                    "savings_minor": calc.base_amount_minor - calc.amount_minor,
                    "monthly_minor": calc.monthly_minor,
                }
            )

    upgrade = await _build_upgrade_option(session, sub, now)

    return {
        "currency": "RUB",
        "current_plan_code": sub.plan_code,
        "extend": extend_items,
        "upgrade": asdict(upgrade),
    }


# --- 3. Checkout: создание платежа ------------------------------------------------
def _build_description(kind: str, plan_code: str, months: int) -> str:
    if kind == PaymentKind.extend.value:
        return f"Smenka — продление тарифа {plan_code}, {months} мес"
    return f"Smenka — апгрейд до тарифа {plan_code}, доплата за {months} мес"


async def _find_reusable_pending_payment(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    kind: str,
    plan_code: str,
    months: int,
    now: datetime,
) -> Payment | None:
    cutoff = now - _PENDING_REUSE_WINDOW
    result = await session.execute(
        select(Payment)
        .where(
            Payment.organization_id == org_id,
            Payment.status == PaymentStatus.pending.value,
            Payment.kind == kind,
            Payment.plan_code == plan_code,
            Payment.months == months,
            Payment.created_at >= cutoff,
        )
        .order_by(Payment.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_checkout(
    session: AsyncSession,
    settings: Settings,
    org_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    kind: str,
    plan_code: str,
    months: int | None,
) -> Payment:
    _require_enabled(settings)
    if kind not in (PaymentKind.extend.value, PaymentKind.upgrade.value):
        raise PaymentError("VALIDATION_ERROR", "kind должен быть extend или upgrade", 422)

    # Существование организации уже проверил роутер (org_service.get_organization)
    # перед ролевой проверкой ensure_admin_or_owner — как в subscriptions.py.
    sub = await entitlements.get_subscription(session, org_id)  # 404 SUBSCRIPTION_NOT_FOUND
    now = datetime.now(UTC)

    if kind == PaymentKind.extend.value:
        plan = await entitlements.get_active_plan(session, plan_code)  # 404 PLAN_NOT_FOUND
        period = await _get_active_billing_period(session, months)
        calc = billing_calc.compute_extend_amount(
            plan.price_minor, period.months, period.discount_percent
        )
        final_months = period.months
        base_amount_minor = calc.base_amount_minor
        discount_percent = calc.discount_percent
        amount_minor = calc.amount_minor
        target_plan_code = plan.code
    else:
        if plan_code not in (entitlements.PREMIUM_PLAN_CODE,):
            raise PaymentError("VALIDATION_ERROR", "Апгрейд доступен только на тариф Премиум", 422)
        option = await _build_upgrade_option(session, sub, now)
        months_remaining = option.months_remaining
        upgrade_amount_minor = option.amount_minor
        upgrade_plan_code = option.to_plan_code
        if (
            not option.available
            or months_remaining is None
            or upgrade_amount_minor is None
            or upgrade_plan_code is None
        ):
            reason = option.reason or "not_applicable"
            raise PaymentError("UPGRADE_NOT_APPLICABLE", _UPGRADE_REASON_MESSAGES[reason], 409)
        final_months = months_remaining
        amount_minor = upgrade_amount_minor
        base_amount_minor = upgrade_amount_minor
        discount_percent = 0
        target_plan_code = upgrade_plan_code

    if amount_minor > settings.billing_max_payment_minor:
        raise PaymentError("PAYMENT_AMOUNT_LIMIT", "Сумма платежа превышает допустимый лимит", 422)

    reusable = await _find_reusable_pending_payment(
        session,
        org_id,
        kind=kind,
        plan_code=target_plan_code,
        months=final_months,
        now=now,
    )
    if reusable is not None:
        logger.info(
            "billing_checkout_reused_pending",
            org_id=str(org_id),
            payment_id=str(reusable.id),
        )
        return reusable

    payment = Payment(
        organization_id=org_id,
        kind=kind,
        plan_code=target_plan_code,
        months=final_months,
        base_amount_minor=base_amount_minor,
        discount_percent=discount_percent,
        amount_minor=amount_minor,
        currency="RUB",
        status=PaymentStatus.pending.value,
        provider="yookassa",
        idempotence_key=uuid.uuid4(),
        is_test=settings.yookassa_mode != "live",
        created_by_user_id=actor_id,
    )
    session.add(payment)
    await session.flush()

    return_url = f"{settings.yookassa_return_url_base}/#/tariff?payment={payment.id}"
    metadata = {
        "payment_id": str(payment.id),
        "organization_id": str(org_id),
        "kind": kind,
        "plan_code": target_plan_code,
        "months": str(final_months),
    }
    description = _build_description(kind, target_plan_code, final_months)

    try:
        provider_response = await yookassa_client.create_payment(
            settings,
            idempotence_key=payment.idempotence_key,
            amount_minor=amount_minor,
            currency="RUB",
            description=description,
            return_url=return_url,
            metadata=metadata,
        )
    except yookassa_client.YooKassaClientError as exc:
        logger.error(
            "yookassa_create_payment_failed",
            org_id=str(org_id),
            payment_id=str(payment.id),
            error=repr(exc),
        )
        raise PaymentError(
            "PAYMENT_PROVIDER_ERROR", "Платёжный провайдер недоступен", 502
        ) from exc

    payment.provider_payment_id = provider_response.get("id")
    confirmation = provider_response.get("confirmation") or {}
    payment.confirmation_url = confirmation.get("confirmation_url")
    payment.provider_payload = provider_response
    await session.flush()
    logger.info(
        "billing_checkout_created",
        org_id=str(org_id),
        payment_id=str(payment.id),
        kind=kind,
        amount_minor=amount_minor,
    )
    return payment


# --- Применение платежа (вебхук + поллинг) ---------------------------------------
def _amount_from_provider(provider_data: dict[str, Any]) -> int | None:
    amount = provider_data.get("amount") or {}
    value = amount.get("value")
    if value is None:
        return None
    try:
        return yookassa_client.amount_value_to_minor(value)
    except (ValueError, ArithmeticError):
        return None


def _verify_provider_data(payment: Payment, provider_data: dict[str, Any]) -> bool:
    """Сверка `metadata`/`amount` из ответа провайдера с нашей записью
    (backend.md, п.4 «Уведомления провайдера», шаг 4). Расхождение → платёж
    не применяется вызывающим кодом."""
    metadata = provider_data.get("metadata") or {}
    if metadata.get("payment_id") != str(payment.id):
        return False
    if metadata.get("organization_id") != str(payment.organization_id):
        return False
    provider_amount_minor = _amount_from_provider(provider_data)
    return provider_amount_minor == payment.amount_minor


def _parse_paid_at(provider_data: dict[str, Any]) -> datetime:
    captured_at = provider_data.get("captured_at") or provider_data.get("created_at")
    if captured_at:
        try:
            return ensure_utc(datetime.fromisoformat(captured_at.replace("Z", "+00:00")))
        except ValueError:
            pass
    return datetime.now(UTC)


async def get_payment_by_provider_id(
    session: AsyncSession, provider_payment_id: str
) -> Payment | None:
    result = await session.execute(
        select(Payment).where(Payment.provider_payment_id == provider_payment_id)
    )
    return result.scalar_one_or_none()


async def apply_payment(
    session: AsyncSession, payment_id: uuid.UUID, provider_data: dict[str, Any]
) -> Payment:
    """Применение успешного платежа к подписке. Требования из backend.md
    («Применение платежа»):

    1. Блокировка строки подписки (`SELECT ... FOR UPDATE` по `organization_id`)
       — вебхук и поллинг приходят одновременно штатно.
    2. Идемпотентность по `applied_at` — перечитывается ПОСЛЕ захвата лока,
       чтобы конкурентный вызов, дождавшийся снятия лока, увидел уже
       закоммиченное состояние (READ COMMITTED).
    3/4. extend/upgrade — см. ветки ниже.
    5/6. Событие `paid_online` + простановка `applied_at`/`paid_at`/`status`/
       `subscription_event_id`.
    """
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise PaymentError("PAYMENT_NOT_FOUND", "Платёж не найден", 404)

    sub_result = await session.execute(
        select(Subscription)
        .where(Subscription.organization_id == payment.organization_id)
        .with_for_update()
    )
    sub = sub_result.scalar_one_or_none()

    # Перечитываем платёж уже под локом подписки — конкурентный вызов мог
    # применить его, пока мы ждали блокировку.
    await session.refresh(payment)
    if payment.applied_at is not None:
        return payment

    now = datetime.now(UTC)
    paid_at = _parse_paid_at(provider_data)

    if sub is None:
        # Инвариант «подписка создаётся вместе с организацией» нарушен — не
        # должно происходить в проде. Помечаем succeeded, но не применяем.
        payment.status = PaymentStatus.succeeded.value
        payment.paid_at = paid_at
        await session.flush()
        logger.error(
            "billing_apply_no_subscription",
            org_id=str(payment.organization_id),
            payment_id=str(payment.id),
        )
        return payment

    org = await session.get(Organization, payment.organization_id)
    if org is not None and org.is_deleted:
        # backend.md: «Если организация... была удалена — платёж помечаем
        # succeeded, но не применяем, applied_at остаётся NULL».
        payment.status = PaymentStatus.succeeded.value
        payment.paid_at = paid_at
        await session.flush()
        logger.warning(
            "billing_apply_org_deleted",
            org_id=str(payment.organization_id),
            payment_id=str(payment.id),
        )
        return payment

    from_plan_code = sub.plan_code
    from_status = sub.status
    period_end_before = sub.current_period_end

    if payment.kind == PaymentKind.extend.value:
        months = payment.months
        if months is None:
            # Инвариант нарушен: extend-платёж обязан фиксировать months при
            # создании (`create_checkout`). Данные повреждены — громко падаем,
            # а не тихо продлеваем на 0 месяцев.
            raise ValueError(f"payment {payment.id}: kind=extend без months")
        reference = (
            sub.current_period_end if sub.current_period_end is not None else sub.trial_ends_at
        )
        # `base = max(now, current_period_end либо trial_ends_at)` — тем же
        # правилом, что ручной extend (`subscription.py::extend_subscription`);
        # `now` — момент применения (обработки вебхука/поллинга), не момент
        # `paid_at` провайдера (тот идёт только в `payments.paid_at`).
        base = max(now, reference) if reference is not None else now
        sub.current_period_end = entitlements.add_months(base, months)
        sub.status = "active"
        sub.plan_code = payment.plan_code
        if sub.current_period_start is None:
            sub.current_period_start = now
        sub.last_expiry_notice_days = None
        note = f"{months} мес, -{payment.discount_percent}% (онлайн-оплата)"
    else:
        sub.plan_code = payment.plan_code
        note = f"апгрейд до Премиума, {payment.months} мес (онлайн-оплата)"

    await session.flush()

    event = SubscriptionEvent(
        organization_id=payment.organization_id,
        type=SubscriptionEventType.paid_online.value,
        from_plan_code=from_plan_code,
        to_plan_code=sub.plan_code,
        from_status=from_status,
        to_status=sub.status,
        period_end_before=period_end_before,
        period_end_after=sub.current_period_end,
        months=payment.months,
        amount_minor=payment.amount_minor,
        note=note,
        actor_user_id=payment.created_by_user_id,
        payment_id=payment.id,
    )
    session.add(event)
    await session.flush()

    payment.status = PaymentStatus.succeeded.value
    payment.paid_at = paid_at
    payment.applied_at = datetime.now(UTC)
    payment.subscription_event_id = event.id
    await session.flush()

    logger.info(
        "billing_payment_applied",
        org_id=str(payment.organization_id),
        payment_id=str(payment.id),
        kind=payment.kind,
        amount_minor=payment.amount_minor,
    )
    return payment


async def mark_payment_canceled(
    session: AsyncSession, payment_id: uuid.UUID, provider_data: dict[str, Any]
) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise PaymentError("PAYMENT_NOT_FOUND", "Платёж не найден", 404)
    if payment.status != PaymentStatus.pending.value:
        return payment
    cancellation = provider_data.get("cancellation_details") or {}
    payment.status = PaymentStatus.canceled.value
    payment.cancellation_reason = cancellation.get("reason")
    await session.flush()
    return payment


async def reconcile_payment(
    session: AsyncSession, settings: Settings, payment: Payment
) -> Payment:
    """Запрашивает свежее состояние платежа у провайдера и применяет его при
    необходимости. Идемпотентна — безопасно вызывать повторно (вебхук и
    поллинг статуса вызывают её независимо). Не коммитит — коммит на стороне
    вызывающего роутера."""
    if payment.provider_payment_id is None:
        return payment

    provider_data = await yookassa_client.get_payment(settings, payment.provider_payment_id)
    payment.provider_payload = provider_data
    await session.flush()

    if not _verify_provider_data(payment, provider_data):
        logger.error(
            "billing_payment_mismatch",
            payment_id=str(payment.id),
            org_id=str(payment.organization_id),
            provider_payment_id=payment.provider_payment_id,
        )
        return payment

    status = provider_data.get("status")
    if status == "succeeded":
        return await apply_payment(session, payment.id, provider_data)
    if status == "canceled":
        return await mark_payment_canceled(session, payment.id, provider_data)
    return payment


async def apply_refund(
    session: AsyncSession, provider_payment_id: str, refund_data: dict[str, Any]
) -> Payment | None:
    """`refund.succeeded` — возврат оформляется вручную в ЛК ЮKassa, но
    вебхук приходит и должен обрабатываться (backend.md, «Возвраты»).
    Автоматического сокращения оплаченного периода нет — решение принимает
    super_admin руками через обычный `PATCH .../subscription`."""
    result = await session.execute(
        select(Payment).where(Payment.provider_payment_id == provider_payment_id).with_for_update()
    )
    payment = result.scalar_one_or_none()
    if payment is None:
        logger.error("billing_refund_payment_not_found", provider_payment_id=provider_payment_id)
        return None
    if payment.status == PaymentStatus.refunded.value:
        return payment  # идемпотентность повторного вебхука refund.succeeded

    refund_amount_minor = _amount_from_provider(refund_data) or 0
    payment.status = PaymentStatus.refunded.value
    await session.flush()

    session.add(
        SubscriptionEvent(
            organization_id=payment.organization_id,
            type=SubscriptionEventType.payment_refunded.value,
            from_plan_code=payment.plan_code,
            to_plan_code=payment.plan_code,
            amount_minor=-refund_amount_minor,
            note=(
                f"Возврат по платежу {payment.id} ({refund_amount_minor / 100:.2f} ₽) — "
                "оформлен вручную в ЛК ЮKassa"
            ),
            payment_id=payment.id,
        )
    )
    await session.flush()
    logger.warning(
        "billing_payment_refunded",
        payment_id=str(payment.id),
        org_id=str(payment.organization_id),
        refund_amount_minor=refund_amount_minor,
    )
    return payment


# --- 4. Вебхук ЮKassa (диспетчер событий) -----------------------------------------
_HANDLED_PAYMENT_EVENTS = ("payment.succeeded", "payment.canceled")


async def process_webhook_event(
    session: AsyncSession, settings: Settings, body: dict[str, Any]
) -> None:
    """Диспетчер тела вебхука (IP уже проверен и тело уже распарсено роутером,
    см. `api/v1/billing_webhook.py`). Всегда либо коммитит успешную обработку,
    либо поднимает `PaymentError`/`YooKassaClientError`, которые роутер
    превращает в 4xx/5xx, достойные повтора провайдером."""
    event = body.get("event")
    obj = body.get("object")
    if not isinstance(obj, dict):
        raise PaymentError("VALIDATION_ERROR", "Тело вебхука не содержит object", 400)

    if event in _HANDLED_PAYMENT_EVENTS:
        provider_payment_id = obj.get("id")
        if not provider_payment_id:
            raise PaymentError("VALIDATION_ERROR", "object.id отсутствует в теле вебхука", 400)

        payment = await get_payment_by_provider_id(session, provider_payment_id)
        if payment is None:
            # Платёж создаётся синхронно в create_checkout ДО вызова ЮKassa,
            # поэтому к моменту вебхука запись обязана существовать. Если её
            # нет — залогировать и отдать 200 (тело было корректным,
            # повторная доставка ничего не изменит).
            logger.error(
                "billing_webhook_payment_not_found",
                provider_payment_id=provider_payment_id,
                webhook_event=event,
            )
            return

        try:
            await reconcile_payment(session, settings, payment)
        except yookassa_client.YooKassaClientError as exc:
            logger.error(
                "billing_webhook_reconcile_failed",
                provider_payment_id=provider_payment_id,
                error=repr(exc),
            )
            raise PaymentError(
                "PAYMENT_PROVIDER_ERROR", "Не удалось проверить платёж у провайдера", 502
            ) from exc
        await session.commit()
        return

    if event == "refund.succeeded":
        provider_payment_id = obj.get("payment_id")
        if not provider_payment_id:
            raise PaymentError(
                "VALIDATION_ERROR", "object.payment_id отсутствует в теле вебхука refund", 400
            )
        await apply_refund(session, provider_payment_id, obj)
        await session.commit()
        return

    logger.info("billing_webhook_unknown_event", webhook_event=event)


# --- 5/6. Статус и история платежей организации ----------------------------------
async def get_payment_for_org(
    session: AsyncSession, settings: Settings, org_id: uuid.UUID, payment_id: uuid.UUID
) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.organization_id != org_id:
        raise PaymentError("PAYMENT_NOT_FOUND", "Платёж не найден", 404)

    if payment.status == PaymentStatus.pending.value:
        age = (datetime.now(UTC) - ensure_utc(payment.created_at)).total_seconds()
        if age > _POLL_RECONCILE_AFTER_SECONDS:
            try:
                payment = await reconcile_payment(session, settings, payment)
                await session.commit()
            except yookassa_client.YooKassaClientError as exc:
                logger.warning(
                    "billing_poll_reconcile_failed",
                    payment_id=str(payment_id),
                    error=repr(exc),
                )
                await session.rollback()
    return payment


async def list_payments_for_org(
    session: AsyncSession, org_id: uuid.UUID, *, limit: int, offset: int
) -> tuple[list[Payment], int]:
    total = (
        await session.execute(
            select(func.count()).select_from(Payment).where(Payment.organization_id == org_id)
        )
    ).scalar_one()
    result = await session.execute(
        select(Payment)
        .where(Payment.organization_id == org_id)
        .order_by(Payment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total


async def get_plan_names(session: AsyncSession, plan_codes: set[str]) -> dict[str, str]:
    if not plan_codes:
        return {}
    result = await session.execute(select(Plan.code, Plan.name).where(Plan.code.in_(plan_codes)))
    return dict(result.tuples().all())


# --- 7. Реестр платежей платформы (super_admin) -----------------------------------
@dataclass(frozen=True, slots=True)
class AdminPaymentRow:
    payment: Payment
    organization_name: str
    plan_name: str
    created_by: User | None


async def list_admin_payments(
    session: AsyncSession,
    *,
    status: str | None = None,
    organization_id: uuid.UUID | None = None,
    is_test: bool | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[AdminPaymentRow], int, dict[str, int]]:
    conditions = []
    if status is not None:
        conditions.append(Payment.status == status)
    if organization_id is not None:
        conditions.append(Payment.organization_id == organization_id)
    if is_test is not None:
        conditions.append(Payment.is_test.is_(is_test))
    if date_from is not None:
        conditions.append(Payment.created_at >= date_from)
    if date_to is not None:
        conditions.append(Payment.created_at <= date_to)

    count_query = select(func.count()).select_from(Payment).where(*conditions)
    total = (await session.execute(count_query)).scalar_one()

    result = await session.execute(
        select(Payment)
        .where(*conditions)
        .order_by(Payment.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    payments = list(result.scalars().all())

    org_ids = {p.organization_id for p in payments}
    org_names: dict[uuid.UUID, str] = {}
    if org_ids:
        org_rows = await session.execute(
            select(Organization.id, Organization.name).where(Organization.id.in_(org_ids))
        )
        org_names = dict(org_rows.tuples().all())

    plan_names = await get_plan_names(session, {p.plan_code for p in payments})

    creator_ids = {p.created_by_user_id for p in payments if p.created_by_user_id is not None}
    creators: dict[uuid.UUID, User] = {}
    if creator_ids:
        users_result = await session.execute(select(User).where(User.id.in_(creator_ids)))
        creators = {u.id: u for u in users_result.scalars().all()}

    rows = [
        AdminPaymentRow(
            payment=p,
            organization_name=org_names.get(p.organization_id, ""),
            plan_name=plan_names.get(p.plan_code, p.plan_code),
            created_by=creators.get(p.created_by_user_id) if p.created_by_user_id else None,
        )
        for p in payments
    ]

    # Totals — с учётом фильтров периода/организации, но ВСЕГДА succeeded и
    # ВСЕГДА без тестовых платежей (backend.md, п.7): "status"/"is_test" из
    # запроса на агрегат не влияют — он отвечает на другой вопрос («сколько
    # реальных денег пришло»), а не «сколько строк подходит под текущий
    # фильтр списка».
    totals_conditions = [
        Payment.status == PaymentStatus.succeeded.value,
        Payment.is_test.is_(False),
    ]
    if organization_id is not None:
        totals_conditions.append(Payment.organization_id == organization_id)
    if date_from is not None:
        totals_conditions.append(Payment.created_at >= date_from)
    if date_to is not None:
        totals_conditions.append(Payment.created_at <= date_to)
    totals_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(Payment.amount_minor), 0),
                func.count(),
            ).where(*totals_conditions)
        )
    ).one()
    totals = {"succeeded_amount_minor": int(totals_row[0]), "count": int(totals_row[1])}

    return rows, total, totals
