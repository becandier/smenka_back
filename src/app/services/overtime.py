"""Заявки на переработку (backend.md, R6).

Сотрудник подаёт заявку на завершённой смене, у которой факт не превысил
план; owner/admin согласует/отклоняет. `approved` заявка добавляет
`minutes * 60` к оплачиваемому времени смены в payroll (`services/payroll.py`).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.logging import get_logger
from src.app.models.organization import OrganizationMember
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.shift_overtime_request import OvertimeRequestStatus, ShiftOvertimeRequest
from src.app.models.user import User
from src.app.models.work_location import WorkLocation
from src.app.services import entitlements
from src.app.services.common import ensure_admin_or_owner
from src.app.services.organization import get_organization
from src.app.services.organization_settings import get_settings_for_org
from src.app.services.shift import ensure_utc, validate_date_range

logger = get_logger(__name__)

DEFAULT_OVERTIME_REQUEST_DAYS = 7
VALID_REVIEW_STATUSES = {OvertimeRequestStatus.approved, OvertimeRequestStatus.rejected}


class OvertimeError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        self.code = code
        self.message = message
        self.status_code = status_code


async def _get_own_shift(session: AsyncSession, shift_id: uuid.UUID, user_id: uuid.UUID) -> Shift:
    result = await session.execute(
        select(Shift).where(
            Shift.id == shift_id,
            Shift.user_id == user_id,
            Shift.is_deleted.is_(False),
        )
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise OvertimeError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


async def _get_overtime_request_days(session: AsyncSession, org_id: uuid.UUID) -> int:
    settings = await get_settings_for_org(session, org_id)
    if settings is None:
        return DEFAULT_OVERTIME_REQUEST_DAYS
    return settings.overtime_request_days


async def create_overtime_request(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    minutes: int,
    comment: str,
) -> ShiftOvertimeRequest:
    """R6: заявка допустима на завершённой org-смене с графиком, где факт ≤ план."""
    shift = await _get_own_shift(session, shift_id, user_id)

    if (
        shift.organization_id is None
        or shift.status != ShiftStatus.finished
        or shift.scheduled_end_at is None
        or shift.finished_at is None
        or shift.finished_at > shift.scheduled_end_at
    ):
        raise OvertimeError(
            "OVERTIME_NOT_APPLICABLE",
            "По этой смене переработку добавить нельзя",
            409,
        )

    overtime_request_days = await _get_overtime_request_days(session, shift.organization_id)
    now = datetime.now(UTC)
    if now - shift.finished_at > timedelta(days=overtime_request_days):
        raise OvertimeError(
            "OVERTIME_PERIOD_EXPIRED",
            "Срок подачи заявки истёк",
            400,
        )

    existing_result = await session.execute(
        select(ShiftOvertimeRequest.id).where(
            ShiftOvertimeRequest.shift_id == shift_id,
            ShiftOvertimeRequest.status.in_(
                [OvertimeRequestStatus.pending, OvertimeRequestStatus.approved]
            ),
        )
    )
    if existing_result.scalar_one_or_none() is not None:
        raise OvertimeError(
            "OVERTIME_ALREADY_REQUESTED",
            "По этой смене уже есть заявка на переработку",
            409,
        )

    request = ShiftOvertimeRequest(shift_id=shift_id, minutes=minutes, comment=comment)
    session.add(request)
    try:
        await session.flush()
    except IntegrityError:
        # Гонка двух параллельных POST — частичный уникальный индекс закрывает.
        raise OvertimeError(
            "OVERTIME_ALREADY_REQUESTED",
            "По этой смене уже есть заявка на переработку",
            409,
        ) from None

    logger.info(
        "overtime_request_created",
        shift_id=str(shift_id),
        request_id=str(request.id),
        minutes=minutes,
    )
    return request


async def delete_own_overtime_request(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    """Отзыв своей `pending` заявки. Рассмотренную (approved/rejected) — нельзя."""
    shift = await _get_own_shift(session, shift_id, user_id)

    result = await session.execute(
        select(ShiftOvertimeRequest).where(
            ShiftOvertimeRequest.shift_id == shift.id,
            ShiftOvertimeRequest.status.in_(
                [OvertimeRequestStatus.pending, OvertimeRequestStatus.approved]
            ),
        )
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise OvertimeError("OVERTIME_REQUEST_NOT_FOUND", "Заявка не найдена", 404)
    if request.status != OvertimeRequestStatus.pending:
        raise OvertimeError(
            "OVERTIME_ALREADY_REVIEWED",
            "Заявка уже рассмотрена, отозвать нельзя",
            409,
        )

    await session.delete(request)
    await session.flush()
    logger.info("overtime_request_deleted", shift_id=str(shift_id), request_id=str(request.id))


async def review_overtime_request(
    session: AsyncSession,
    org_id: uuid.UUID,
    request_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    status: str,
    review_comment: str | None,
) -> ShiftOvertimeRequest:
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(
        session,
        org,
        requester_id,
        message="Нет прав для рассмотрения заявок на переработку",
    )
    await entitlements.require_active_subscription(session, org, requester_id)

    try:
        new_status = OvertimeRequestStatus(status)
    except ValueError:
        new_status = None
    if new_status not in VALID_REVIEW_STATUSES:
        raise OvertimeError(
            "VALIDATION_ERROR",
            "status должен быть approved или rejected",
            422,
        )

    result = await session.execute(
        select(ShiftOvertimeRequest)
        .join(Shift, ShiftOvertimeRequest.shift_id == Shift.id)
        .where(
            ShiftOvertimeRequest.id == request_id,
            Shift.organization_id == org_id,
            Shift.is_deleted.is_(False),
        )
    )
    request = result.scalar_one_or_none()
    if request is None:
        raise OvertimeError("OVERTIME_REQUEST_NOT_FOUND", "Заявка не найдена", 404)
    if request.status != OvertimeRequestStatus.pending:
        raise OvertimeError("OVERTIME_ALREADY_REVIEWED", "Заявка уже рассмотрена", 409)

    request.status = new_status
    request.review_comment = review_comment
    request.reviewed_by_user_id = requester_id
    request.reviewed_at = datetime.now(UTC)
    await session.flush()
    logger.info(
        "overtime_request_reviewed",
        org_id=str(org_id),
        request_id=str(request_id),
        status=new_status.value,
    )
    return request


@dataclass(frozen=True)
class OrgOvertimeRow:
    request: ShiftOvertimeRequest
    shift: Shift
    user: User | None
    display_name: str | None
    work_location_name: str | None


async def list_org_overtime_requests(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    status: str | None = None,
    user_ids: list[uuid.UUID] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[OrgOvertimeRow], int]:
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(
        session,
        org,
        requester_id,
        message="Нет прав для просмотра заявок на переработку",
    )

    status_enum = None
    if status is not None:
        try:
            status_enum = OvertimeRequestStatus(status)
        except ValueError:
            raise OvertimeError(
                "VALIDATION_ERROR",
                "status должен быть pending, approved или rejected",
                422,
            ) from None

    validate_date_range(date_from, date_to)

    conditions = [Shift.organization_id == org_id, Shift.is_deleted.is_(False)]
    if status_enum is not None:
        conditions.append(ShiftOvertimeRequest.status == status_enum)
    if user_ids:
        conditions.append(Shift.user_id.in_(user_ids))
    if date_from is not None:
        conditions.append(Shift.started_at >= ensure_utc(date_from))
    if date_to is not None:
        conditions.append(Shift.started_at <= ensure_utc(date_to))

    count_query = (
        select(func.count())
        .select_from(ShiftOvertimeRequest)
        .join(Shift, ShiftOvertimeRequest.shift_id == Shift.id)
        .where(*conditions)
    )
    total = (await session.execute(count_query)).scalar_one()

    page_query = (
        select(ShiftOvertimeRequest, Shift, User, OrganizationMember, WorkLocation)
        .join(Shift, ShiftOvertimeRequest.shift_id == Shift.id)
        .outerjoin(User, Shift.user_id == User.id)
        .outerjoin(
            OrganizationMember,
            and_(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == Shift.user_id,
            ),
        )
        .outerjoin(WorkLocation, Shift.work_location_id == WorkLocation.id)
        .where(*conditions)
        .order_by(ShiftOvertimeRequest.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(page_query)).all()

    out = [
        OrgOvertimeRow(
            request=req,
            shift=shift,
            user=user,
            display_name=member.display_name if member is not None else None,
            work_location_name=loc.name if loc is not None else None,
        )
        for req, shift, user, member, loc in rows
    ]
    return out, total


# --- Для ShiftResponse.overtime и payroll (services/payroll.py) ----------------


async def get_latest_overtime_for_shifts(
    session: AsyncSession,
    shift_ids: list[uuid.UUID],
) -> dict[uuid.UUID, ShiftOvertimeRequest]:
    """shift_id → последняя (по `created_at`) заявка смены — для `ShiftResponse.overtime`.

    Показываем самую свежую заявку независимо от статуса (в т.ч. `rejected` —
    мобилка должна показать причину отказа и разрешить подать заново)."""
    if not shift_ids:
        return {}
    result = await session.execute(
        select(ShiftOvertimeRequest)
        .distinct(ShiftOvertimeRequest.shift_id)
        .where(ShiftOvertimeRequest.shift_id.in_(shift_ids))
        .order_by(ShiftOvertimeRequest.shift_id, ShiftOvertimeRequest.created_at.desc())
    )
    return {r.shift_id: r for r in result.scalars().all()}


async def get_approved_overtime_minutes_by_shift(
    session: AsyncSession,
    shift_ids: list[uuid.UUID],
) -> dict[uuid.UUID, int]:
    """shift_id → минуты согласованной переработки (payroll: `overtime_seconds`)."""
    if not shift_ids:
        return {}
    result = await session.execute(
        select(ShiftOvertimeRequest.shift_id, ShiftOvertimeRequest.minutes).where(
            ShiftOvertimeRequest.shift_id.in_(shift_ids),
            ShiftOvertimeRequest.status == OvertimeRequestStatus.approved,
        )
    )
    return dict(result.tuples().all())
