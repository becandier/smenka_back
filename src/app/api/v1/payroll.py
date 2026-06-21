# src/app/api/v1/payroll.py
import uuid
from datetime import datetime as dt_datetime
from typing import Any

from fastapi import APIRouter, Query, Response

from src.app.api.deps import CurrentUserDep, SessionDep
from src.app.models.member_rate import OrganizationMemberRate
from src.app.schemas.base import ApiResponse
from src.app.schemas.payroll import (
    CurrentRateResponse,
    ExportFormat,
    Granularity,
    MyEarningsResponse,
    PayrollDetailedResponse,
    PayrollResponse,
    RateCreate,
    RateDeleteResponse,
    RateListResponse,
    RateResponse,
    RateUpdate,
)
from src.app.services import payroll as payroll_service

_USER_IDS_QUERY = Query(
    None, description="Оставить только указанных сотрудников (повтор параметра или CSV uuid)"
)
_LOCATION_IDS_QUERY = Query(
    None,
    description=(
        "Учитывать только смены указанных точек (повтор/CSV uuid); спец-значение "
        "none — смены без точки"
    ),
)
_TZ_QUERY = Query("UTC", description="IANA-таймзона нарезки корзин (напр. Europe/Moscow)")
_ONLY_MISSING_RATE_QUERY = Query(
    False, description="Оставить только сотрудников со сменами без действующей ставки"
)
_INCLUDE_PENALTIES_QUERY = Query(
    True, description="Учитывать штрафы (penalty/net-поля); false — штрафы не вычитаются"
)

router = APIRouter(prefix="/organizations", tags=["payroll"])


def _rate_response(rate: OrganizationMemberRate) -> RateResponse:
    return RateResponse(
        id=str(rate.id),
        member_id=str(rate.member_id),
        rate_amount_minor=rate.rate_amount_minor,
        rate_type=rate.rate_type.value,
        currency=rate.currency,
        effective_from=rate.effective_from,
        note=rate.note,
        created_at=rate.created_at,
    )


def _rate_to_response(rate: OrganizationMemberRate) -> dict[str, Any]:
    return _rate_response(rate).model_dump(mode="json")


def current_rate_payload(
    rate: OrganizationMemberRate | None,
) -> dict[str, Any] | None:
    """Действующая ставка для вложения в ответы (MemberResponse, my-earnings)."""
    if rate is None:
        return None
    return CurrentRateResponse(
        rate_amount_minor=rate.rate_amount_minor,
        rate_type=rate.rate_type.value,
        currency=rate.currency,
        effective_from=rate.effective_from,
    ).model_dump(mode="json")


@router.post(
    "/{org_id}/members/{member_id}/rates",
    status_code=201,
    summary="Назначить ставку (новая запись истории)",
    description=(
        "Добавляет новую строку истории ставок участника, действующую с "
        "`effective_from`. Прошлые ставки не перезаписываются — для смены "
        "ставки «с даты» используется именно этот эндпоинт. `member_id` — "
        "UUID записи участника (organization_members.id). Доступно владельцу "
        "(Owner) и админам."
    ),
)
async def create_member_rate(
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    body: RateCreate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    rate = await payroll_service.create_rate(
        session,
        org_id,
        member_id,
        user.id,
        rate_amount_minor=body.rate_amount_minor,
        rate_type=body.rate_type,
        currency=body.currency,
        effective_from=body.effective_from,
        note=body.note,
    )
    await session.commit()
    return ApiResponse.success(_rate_to_response(rate))


@router.get(
    "/{org_id}/members/{member_id}/rates",
    summary="История ставок участника",
    description=(
        "Вся история ставок участника, сортировка по `effective_from` по "
        "убыванию, без пагинации. Доступно владельцу (Owner) и админам."
    ),
)
async def list_member_rates(
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    rates = await payroll_service.list_rates(session, org_id, member_id, user.id)
    return ApiResponse.success(
        RateListResponse(
            items=[_rate_response(r) for r in rates],
        ).model_dump(mode="json")
    )


@router.patch(
    "/{org_id}/members/{member_id}/rates/{rate_id}",
    summary="Исправить запись истории ставок",
    description=(
        "Исправление существующей записи (например, опечатки). Все поля "
        "опциональны. Для назначения новой ставки «с даты» используйте POST. "
        "Доступно владельцу (Owner) и админам."
    ),
)
async def update_member_rate(
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    rate_id: uuid.UUID,
    body: RateUpdate,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    rate = await payroll_service.update_rate(
        session,
        org_id,
        member_id,
        rate_id,
        user.id,
        body.model_dump(exclude_unset=True),
    )
    await session.commit()
    return ApiResponse.success(_rate_to_response(rate))


@router.delete(
    "/{org_id}/members/{member_id}/rates/{rate_id}",
    summary="Удалить запись истории ставок",
    description=(
        "Удаляет ошибочную запись истории. Действующая ставка для затронутых "
        "периодов пересчитывается «на лету» при следующем запросе payroll. "
        "Доступно владельцу (Owner) и админам."
    ),
)
async def delete_member_rate(
    org_id: uuid.UUID,
    member_id: uuid.UUID,
    rate_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ApiResponse:
    await payroll_service.delete_rate(session, org_id, member_id, rate_id, user.id)
    await session.commit()
    return ApiResponse.success(RateDeleteResponse(deleted=True).model_dump())


@router.get(
    "/{org_id}/payroll",
    summary="Отчёт по зарплате за период",
    description=(
        "Отчёт «сколько кому заплатить»: по каждому сотруднику с завершёнными "
        "сменами в периоде — отработанное время, число смен и начисление в "
        "копейках по ставке, действовавшей на момент каждой смены. Смены без "
        "действующей ставки попадают в `unpaid_*` и не входят в `gross`. "
        "`date_to` включительно (как в date_filters). При `granularity != none` "
        "к каждому сотруднику добавляется `breakdown` — разбивка по корзинам "
        "(день/неделя/месяц) в таймзоне `tz` с посуточным округлением денег. "
        "Фильтры `user_ids`, `location_ids` (вкл. none — «без точки»), "
        "`only_missing_rate`. Доступно владельцу (Owner) и админам."
    ),
)
async def org_payroll(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница периода по started_at, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница периода по started_at, включительно (UTC)"
    ),
    granularity: Granularity = Query(
        Granularity.none, description="Уровень разбивки: none (агрегат) | day | week | month"
    ),
    user_ids: list[str] | None = _USER_IDS_QUERY,
    location_ids: list[str] | None = _LOCATION_IDS_QUERY,
    tz: str = _TZ_QUERY,
    only_missing_rate: bool = _ONLY_MISSING_RATE_QUERY,
    include_penalties: bool = _INCLUDE_PENALTIES_QUERY,
) -> ApiResponse:
    report = await payroll_service.get_org_payroll(
        session,
        org_id,
        user.id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity.value,
        user_ids=user_ids,
        location_ids=location_ids,
        tz=tz,
        only_missing_rate=only_missing_rate,
        include_penalties=include_penalties,
    )
    model = PayrollDetailedResponse if "granularity" in report else PayrollResponse
    return ApiResponse.success(model(**report).model_dump(mode="json"))


@router.get(
    "/{org_id}/payroll/export",
    summary="Экспорт отчёта по зарплате в Excel",
    description=(
        "Бинарная выгрузка `.xlsx` с листами «Сводка» (агрегат по сотрудникам) и "
        "«Детализация» (сотрудник × корзина). Те же фильтры, что у `payroll`; "
        "если `granularity` не передан — берётся `day` (детализация — смысл "
        "выгрузки). Часы и деньги — числами, чтобы Excel суммировал. Доступно "
        "владельцу (Owner) и админам."
    ),
    response_class=Response,
)
async def export_payroll(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница периода по started_at, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница периода по started_at, включительно (UTC)"
    ),
    granularity: Granularity | None = Query(
        None, description="Уровень разбивки; по умолчанию day (детализация)"
    ),
    user_ids: list[str] | None = _USER_IDS_QUERY,
    location_ids: list[str] | None = _LOCATION_IDS_QUERY,
    tz: str = _TZ_QUERY,
    only_missing_rate: bool = _ONLY_MISSING_RATE_QUERY,
    include_penalties: bool = _INCLUDE_PENALTIES_QUERY,
    export_format: ExportFormat = Query(
        ExportFormat.xlsx, alias="format", description="Формат выгрузки (на старте только xlsx)"
    ),
) -> Response:
    content, filename = await payroll_service.export_org_payroll(
        session,
        org_id,
        user.id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity.value if granularity is not None else None,
        user_ids=user_ids,
        location_ids=location_ids,
        tz=tz,
        only_missing_rate=only_missing_rate,
        include_penalties=include_penalties,
    )
    return Response(
        content=content,
        media_type=payroll_service.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/{org_id}/my-earnings",
    summary="Мой заработок за период",
    description=(
        "Личный заработок текущего участника за период (та же логика расчёта, "
        "что в payroll) и его действующая ставка. Доступно только участникам "
        "организации (employee/admin); owner участником не является."
    ),
)
async def my_earnings(
    org_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    date_from: dt_datetime | None = Query(
        None, description="Нижняя граница периода по started_at, включительно (UTC)"
    ),
    date_to: dt_datetime | None = Query(
        None, description="Верхняя граница периода по started_at, включительно (UTC)"
    ),
) -> ApiResponse:
    earnings = await payroll_service.get_my_earnings(
        session,
        org_id,
        user.id,
        date_from=date_from,
        date_to=date_to,
    )
    current_rate = earnings.pop("current_rate")
    return ApiResponse.success(
        MyEarningsResponse(
            **earnings,
            current_rate=current_rate_payload(current_rate),
        ).model_dump(mode="json")
    )
