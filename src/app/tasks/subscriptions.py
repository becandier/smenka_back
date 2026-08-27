"""Celery-beat задача уведомлений о подписке (`tariffs`).

Раз в сутки: за 7/3/1 день до `trial_ends_at`/`current_period_end` —
`subscription_expiring` (антидубль `last_expiry_notice_days`: письмо шлётся,
только если `last_expiry_notice_days IS NULL OR last_expiry_notice_days > N`);
в момент перехода в `suspended` (первый прогон после `grace_ends_at`) —
`subscription_suspended`, однократно, и запись `auto_suspended` в
`subscription_events` (актор `NULL` — системное событие; `status`/`plan_code`
подписки не меняются, переход в `suspended` производный от дат, не хранимый).
Получатели — owner организации и все участники с ролью `admin`.

Идемпотентность `subscription_suspended` (повторный прогон в тот же день и
во все последующие дни, пока статус остаётся `suspended`, не должен слать
уведомление снова) реализована через сентинел `0` в том же поле
`last_expiry_notice_days` — в ТЗ нет отдельной колонки под это, а `0` никогда
не выдаётся как реальный порог `7`/`3`/`1` (см. `models/subscription.py`).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.app.core.celery_app import celery_app
from src.app.core.database import get_sync_session
from src.app.core.logging import get_logger
from src.app.models.notification import Notification, NotificationType
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.subscription import Subscription, SubscriptionEvent, SubscriptionEventType
from src.app.services.entitlements import (
    EffectiveStatus,
    compute_effective_status,
    days_left,
    period_reference,
)

logger = get_logger(__name__)

_EXPIRY_THRESHOLDS = (7, 3, 1)
_SUSPENDED_NOTICE_SENTINEL = 0


def _recipient_user_ids(session: Session, org: Organization) -> set[uuid.UUID]:
    """Owner + все участники с ролью admin — получатели тарифных уведомлений
    (employee про тарифы не уведомляется, `docs/BILLING.md` §5)."""
    admin_ids = (
        session.execute(
            select(OrganizationMember.user_id).where(
                OrganizationMember.organization_id == org.id,
                OrganizationMember.role == MemberRole.admin,
            )
        )
        .scalars()
        .all()
    )
    return {org.owner_id, *admin_ids}


def _notify(
    session: Session,
    org: Organization,
    user_ids: set[uuid.UUID],
    *,
    type_: NotificationType,
    title: str,
    body: str,
    payload: dict[str, Any],
) -> None:
    for user_id in user_ids:
        session.add(
            Notification(
                user_id=user_id,
                organization_id=org.id,
                type=type_.value,
                title=title,
                body=body,
                payload=payload,
            )
        )


@celery_app.task(name="notify_subscription_status")
def notify_subscription_status() -> None:
    with get_sync_session() as session:
        now = datetime.now(UTC)

        rows = session.execute(
            select(Subscription, Organization)
            .join(Organization, Organization.id == Subscription.organization_id)
            .where(Organization.is_deleted.is_(False))
        ).all()

        expiring_count = 0
        suspended_count = 0

        for sub, org in rows:
            status = compute_effective_status(sub, now)

            if status in (EffectiveStatus.trialing, EffectiveStatus.active):
                dl = days_left(sub, now)
                applicable = [n for n in _EXPIRY_THRESHOLDS if dl is not None and dl <= n]
                if applicable:
                    threshold = min(applicable)
                    if (
                        sub.last_expiry_notice_days is None
                        or sub.last_expiry_notice_days > threshold
                    ):
                        _notify(
                            session,
                            org,
                            _recipient_user_ids(session, org),
                            type_=NotificationType.subscription_expiring,
                            title="Подписка скоро закончится",
                            body=(
                                f"Через {dl} дн. организация «{org.name}» перейдёт в "
                                "режим только для чтения, если не продлить подписку."
                            ),
                            payload={"organization_id": str(org.id), "days_left": dl},
                        )
                        sub.last_expiry_notice_days = threshold
                        expiring_count += 1
                elif sub.last_expiry_notice_days is not None:
                    # Самовосстановление: срок ещё не подошёл (> 7 дней) — сбрасываем
                    # устаревший антидубль (напр. после ручной правки PATCH, минуя
                    # extend, где сброс явный) — иначе будущие expiring-предупреждения
                    # будущего цикла оплаты были бы ошибочно подавлены.
                    sub.last_expiry_notice_days = None
            elif (
                status == EffectiveStatus.suspended
                and sub.last_expiry_notice_days != _SUSPENDED_NOTICE_SENTINEL
            ):
                _notify(
                    session,
                    org,
                    _recipient_user_ids(session, org),
                    type_=NotificationType.subscription_suspended,
                    title="Организация переведена в режим только для чтения",
                    body=(
                        f"Подписка организации «{org.name}» не продлена — доступна "
                        "только на чтение до продления."
                    ),
                    payload={"organization_id": str(org.id)},
                )
                # Каждое изменение подписки (включая авто-приостановку) должно
                # оставлять запись в журнале (backend.md, «Приёмка»). Ни `status`,
                # ни `plan_code` при этом не меняются (переход в suspended —
                # производный от дат, а не хранимое состояние) — from/to_status
                # здесь фиксируют ХРАНИМЫЙ статус (не изменился), период — дату
                # отсчёта (trial_ends_at/current_period_end), после которой
                # наступил suspended.
                session.add(
                    SubscriptionEvent(
                        organization_id=org.id,
                        type=SubscriptionEventType.auto_suspended.value,
                        from_status=sub.status,
                        to_status=sub.status,
                        period_end_before=period_reference(sub, now),
                        actor_user_id=None,
                    )
                )
                sub.last_expiry_notice_days = _SUSPENDED_NOTICE_SENTINEL
                suspended_count += 1

        if expiring_count or suspended_count:
            logger.info(
                "subscription_notifications_sent",
                expiring=expiring_count,
                suspended=suspended_count,
            )
