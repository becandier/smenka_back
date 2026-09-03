import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.app.core.config import get_settings
from src.app.core.logging import get_logger
from src.app.models.checklist import (
    ChecklistInstance,
    ChecklistInstanceItem,
    ChecklistInstanceStatus,
    ChecklistItemPhoto,
    ChecklistTemplateItem,
    ChecklistType,
    PhotoRequirement,
)
from src.app.models.file import File, FileCategory
from src.app.models.organization import OrganizationMember
from src.app.models.organization_settings import (
    DEFAULT_CHECKLIST_GRACE_MINUTES,
    OrganizationSettings,
)
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User
from src.app.models.work_location import WorkLocation
from src.app.services.checklist_assignment import _compute_effective
from src.app.services.checklist_location import get_location_ids_for_templates, matches_location
from src.app.services.checklist_template import ChecklistError
from src.app.services.common import ensure_admin_or_owner
from src.app.services.organization import get_organization
from src.app.services.shift import ensure_utc, validate_date_range

logger = get_logger(__name__)
settings = get_settings()


async def create_instances_for_shift(
    session: AsyncSession,
    shift: Shift,
    member: OrganizationMember,
) -> list[ChecklistInstance]:
    """Create checklist instances (snapshots) at shift start for org shifts."""
    if shift.organization_id is None:
        return []

    effective_pairs = await _compute_effective(
        session,
        shift.organization_id,
        member,
    )
    if not effective_pairs:
        return []

    # Фильтр по точке смены (checklist_work_location, backend.md
    # matches_location): применяется единообразно ко всем каналам, включая
    # personal_add. Шаблон без привязок проходит всегда; пустая таблица
    # привязок → фильтр всегда True → нулевое изменение поведения на проде.
    location_ids_by_template = await get_location_ids_for_templates(
        session, [t.id for t, _ in effective_pairs]
    )
    effective_pairs = [
        (template, source)
        for template, source in effective_pairs
        if matches_location(location_ids_by_template.get(template.id), shift.work_location_id)
    ]
    if not effective_pairs:
        return []

    template_ids = [t.id for t, _ in effective_pairs]
    items_result = await session.execute(
        select(ChecklistTemplateItem)
        .where(ChecklistTemplateItem.template_id.in_(template_ids))
        .order_by(ChecklistTemplateItem.position)
    )
    items_by_tpl: dict[uuid.UUID, list[ChecklistTemplateItem]] = {}
    for item in items_result.scalars().all():
        items_by_tpl.setdefault(item.template_id, []).append(item)

    created: list[ChecklistInstance] = []
    for template, _source in effective_pairs:
        instance = ChecklistInstance(
            shift_id=shift.id,
            template_id=template.id,
            name=template.name,
            type=template.type,
            is_required=template.is_required,
            status=ChecklistInstanceStatus.pending,
        )
        session.add(instance)
        await session.flush()

        tpl_items = items_by_tpl.get(template.id, [])
        for tpl_item in tpl_items:
            session.add(
                ChecklistInstanceItem(
                    instance_id=instance.id,
                    template_item_id=tpl_item.id,
                    text=tpl_item.text,
                    is_required=tpl_item.is_required,
                    position=tpl_item.position,
                    # Снимок настроек фото — последующая правка шаблона не влияет.
                    photo_requirement=tpl_item.photo_requirement,
                    photo_source=tpl_item.photo_source,
                )
            )

        # Shortcut: if no required items, instance is already completed.
        has_required_items = any(it.is_required for it in tpl_items)
        if not has_required_items:
            instance.status = ChecklistInstanceStatus.completed
            instance.completed_at = datetime.now(UTC)

        created.append(instance)

    await session.flush()
    logger.info(
        "checklist_instances_created",
        shift_id=str(shift.id),
        count=len(created),
    )
    return created


# --- checklist_grace_period: окно дозаполнения после закрытия смены ---------


@dataclass(frozen=True)
class ChecklistFillWindow:
    """Контекст окна дозаполнения для аддитивных полей ответа API
    (`fill_allowed`/`fill_deadline_at`) — сервер решает сам, клиент не
    вычисляет окно по часам устройства."""

    fill_allowed: bool
    fill_deadline_at: datetime | None


async def get_checklist_grace_minutes(
    session: AsyncSession,
    organization_id: uuid.UUID | None,
) -> int:
    """`checklist_grace_minutes` организации смены. Персональные смены
    (`organization_id is None`) чек-листов не имеют (`create_instances_for_shift`
    их не создаёт) — 0 без похода в БД. Запись настроек отсутствует → считаем
    DEFAULT_CHECKLIST_GRACE_MINUTES (server_default), как и для остальных
    настроек с дефолтом (см. `auto_finish_by_schedule` в `tasks/shifts.py`)."""
    if organization_id is None:
        return 0
    result = await session.execute(
        select(OrganizationSettings.checklist_grace_minutes).where(
            OrganizationSettings.organization_id == organization_id
        )
    )
    value = result.scalar_one_or_none()
    return value if value is not None else DEFAULT_CHECKLIST_GRACE_MINUTES


def compute_fill_window(
    shift: Shift,
    grace_minutes: int,
    *,
    now: datetime | None = None,
    already_finalized: bool = False,
) -> ChecklistFillWindow:
    """Активная/на паузе смена — всегда `fill_allowed=true`, `fill_deadline_at=null`.
    Завершённая — редактируемо, пока `now < finished_at + grace_minutes`;
    `grace_minutes = 0` — окно никогда не открывается (прежнее поведение).

    `already_finalized=True` — терминальная фиксация (`finalize_shift_checklists`,
    inline при `grace=0` либо Celery-задачей `finalize_expired_checklist_grace_periods`
    по истечении окна) уже произошла для этой смены: окно закрыто БЕЗУСЛОВНО, даже
    если пересчёт по чистому времени сказал бы иначе. Это инвариант «incomplete не
    воскресает» — он обязан выполняться и когда деплой задним числом раздвигает
    `checklist_grace_minutes` дефолтом на уже финализированные смены, и когда админ
    вручную увеличивает настройку после того, как часть смен уже прошла через
    финализацию по прежнему, меньшему окну."""
    if shift.status != ShiftStatus.finished:
        return ChecklistFillWindow(fill_allowed=True, fill_deadline_at=None)
    if already_finalized:
        return ChecklistFillWindow(fill_allowed=False, fill_deadline_at=None)
    if grace_minutes <= 0 or shift.finished_at is None:
        return ChecklistFillWindow(fill_allowed=False, fill_deadline_at=None)
    deadline = shift.finished_at + timedelta(minutes=grace_minutes)
    if (now or datetime.now(UTC)) < deadline:
        return ChecklistFillWindow(fill_allowed=True, fill_deadline_at=deadline)
    return ChecklistFillWindow(fill_allowed=False, fill_deadline_at=None)


async def _shift_checklists_finalized(session: AsyncSession, shift_id: uuid.UUID) -> bool:
    """Терминальная фиксация чек-листов смены уже произошла: хотя бы один
    обязательный экземпляр уже несёт статус `incomplete`. Ставит его только
    `finalize_shift_checklists` — и делает это атомарно для ВСЕХ обязательных
    `pending`-экземпляров смены одним UPDATE, поэтому существования одной такой
    строки достаточно, чтобы считать финализацию свершившейся для смены целиком.
    Once true — навсегда true (никакая мутация не переводит `incomplete` обратно)."""
    result = await session.execute(
        select(func.count()).where(
            ChecklistInstance.shift_id == shift_id,
            ChecklistInstance.is_required.is_(True),
            ChecklistInstance.status == ChecklistInstanceStatus.incomplete,
        )
    )
    return result.scalar_one() > 0


async def get_shift_fill_window(
    session: AsyncSession,
    shift_id: uuid.UUID,
) -> ChecklistFillWindow:
    """Окно дозаполнения смены для ответов `GET .../checklists` и
    `GET .../checklists/{instance_id}` (аддитивные `fill_allowed`/
    `fill_deadline_at`). `session.get` бьёт в identity map — если смена уже
    загружена в этой же транзакции (обычный случай), лишнего запроса нет."""
    shift = await session.get(Shift, shift_id)
    if shift is None:
        return ChecklistFillWindow(fill_allowed=False, fill_deadline_at=None)
    grace_minutes = await get_checklist_grace_minutes(session, shift.organization_id)
    already_finalized = await _shift_checklists_finalized(session, shift_id)
    return compute_fill_window(shift, grace_minutes, already_finalized=already_finalized)


async def _assert_fill_window_open(session: AsyncSession, shift: Shift) -> None:
    grace_minutes = await get_checklist_grace_minutes(session, shift.organization_id)
    already_finalized = await _shift_checklists_finalized(session, shift.id)
    window = compute_fill_window(shift, grace_minutes, already_finalized=already_finalized)
    if not window.fill_allowed:
        raise ChecklistError(
            "SHIFT_FINISHED",
            "Смена завершена, время на дозаполнение чек-листа истекло",
            400,
        )


async def _reassert_fill_window_open(session: AsyncSession, shift: Shift) -> None:
    """Повторная проверка окна дозаполнения непосредственно перед мутацией
    (аналог прежнего `_reassert_shift_active`, теперь учитывающий и границу
    окна дозаполнения, а не только терминальный `finished`).

    Защита от гонки на границе окна (ТЗ): между первой проверкой и коммитом
    либо смену мог завершить авто-финиш, либо окно могло истечь по часам, либо
    Celery-задача `finalize_expired_checklist_grace_periods` могла успеть
    терминально зафиксировать чек-листы смены (`_assert_fill_window_open` внутри
    заново читает `_shift_checklists_finalized` — не полагается на устаревший
    Python-снимок). Делаем свежее чтение `status` и `finished_at` (без FOR UPDATE —
    лок строки shifts создал бы цикл с авто-финишем, который сперва лочит строки
    экземпляров, затем строку смены) и пересчитываем окно заново с текущим
    `now()`. Остаточное окно (мутация коммитится на волосок позже) допустимо —
    тот же trade-off, что и раньше (см. `checklist_photos/backend.md`
    «Транзакции и гонки»); финальная защита от воскрешения `incomplete` в этом
    остаточном окне — терминальный guard в `_recompute_instance_status`."""
    await session.refresh(shift, attribute_names=["status", "finished_at"])
    await _assert_fill_window_open(session, shift)


async def _has_live_incomplete_required(session: AsyncSession, shift_id: uuid.UUID) -> bool:
    """Есть ли у смены обязательный экземпляр, ещё не completed. До финализации
    (пока окно открыто) это значит «не все обязательные пункты закрыты пока
    что»; после финализации — то же самое, что «есть терминально incomplete»."""
    result = await session.execute(
        select(func.count()).where(
            ChecklistInstance.shift_id == shift_id,
            ChecklistInstance.is_required.is_(True),
            ChecklistInstance.status != ChecklistInstanceStatus.completed,
        )
    )
    return result.scalar_one() > 0


async def _refresh_live_incomplete_flag(session: AsyncSession, shift: Shift) -> None:
    """Держит `Shift.has_incomplete_required_checklists` актуальным во время
    окна дозаполнения (checklist_grace_period): пока терминальный `incomplete`
    ещё не проставлен, значение пересчитывается на лету при каждой правке
    пункта/фото завершённой смены — так отчёт не «застревает» на `true` после
    того, как сотрудник дозаполнил последний обязательный пункт в окне. Для
    активных/приостановленных смен не трогаем — там поле выставляется только
    на финише (`finish_shift`/авто-финиш)."""
    if shift.status != ShiftStatus.finished:
        return
    has_incomplete = await _has_live_incomplete_required(session, shift.id)
    if shift.has_incomplete_required_checklists != has_incomplete:
        shift.has_incomplete_required_checklists = has_incomplete
        await session.flush()


async def close_shift_checklists(
    session: AsyncSession,
    shift_id: uuid.UUID,
    organization_id: uuid.UUID | None,
) -> bool:
    """Точка входа для завершения смены (`finish_shift`, inline и Celery
    авто-финиш по графику): решает, финализировать ли обязательные чек-листы
    сразу (терминально) или оставить окно дозаполнения открытым.

    `checklist_grace_minutes = 0` — прежнее поведение: `finalize_shift_checklists`
    сразу переводит незакрытые обязательные экземпляры в терминальный
    `incomplete`. При `> 0` терминальная фиксация откладывается: экземпляры
    остаются `pending` (дозаполнение разрешено), а возвращаемое значение —
    живой снимок «остались ли незакрытые обязательные» на момент завершения
    (при отсутствии чек-листов равно `False`). Терминальная фиксация по
    истечении окна — задача `finalize_expired_checklist_grace_periods`.
    """
    grace_minutes = await get_checklist_grace_minutes(session, organization_id)
    if grace_minutes <= 0:
        return await finalize_shift_checklists(session, shift_id)
    return await _has_live_incomplete_required(session, shift_id)


async def _check_shift_access(
    session: AsyncSession,
    shift: Shift,
    requester_id: uuid.UUID,
) -> None:
    if shift.user_id == requester_id:
        return
    if shift.organization_id is None:
        raise ChecklistError("FORBIDDEN", "Нет доступа к чек-листам смены", 403)

    from src.app.models.organization import MemberRole, Organization

    org_result = await session.execute(
        select(Organization).where(Organization.id == shift.organization_id)
    )
    org = org_result.scalar_one_or_none()
    if org is None:
        raise ChecklistError("FORBIDDEN", "Нет доступа к чек-листам смены", 403)
    if org.owner_id == requester_id:
        return

    member_result = await session.execute(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == shift.organization_id,
            OrganizationMember.user_id == requester_id,
            OrganizationMember.role == MemberRole.admin,
        )
    )
    if member_result.scalar_one_or_none() is None:
        raise ChecklistError("FORBIDDEN", "Нет доступа к чек-листам смены", 403)


async def _get_shift(session: AsyncSession, shift_id: uuid.UUID) -> Shift:
    result = await session.execute(
        select(Shift).where(Shift.id == shift_id, Shift.is_deleted.is_(False))
    )
    shift = result.scalar_one_or_none()
    if shift is None:
        raise ChecklistError("SHIFT_NOT_FOUND", "Смена не найдена", 404)
    return shift


async def _recompute_instance_status(
    session: AsyncSession,
    instance: ChecklistInstance,
) -> None:
    """Единый пересчёт статуса экземпляра по критерию «satisfied».

    satisfied = is_completed AND (photo_requirement != required OR photos_count >= 1).
    Экземпляр completed, когда нет не-satisfied ОБЯЗАТЕЛЬНЫХ пунктов; иначе pending.
    Применяется из PATCH пункта, привязки/отвязки фото. completed_at трогаем только
    при реальной смене статуса (без лишнего churn updated_at/онлайна).

    Терминальный инвариант («incomplete не воскресает»): если к моменту получения
    блокировки строка экземпляра уже несёт `incomplete` — no-op, статус не трогаем,
    каким бы ни оказался `blocking`. Это единственная точка, через которую проходят
    ВСЕ мутации пункта/фото, поэтому одной проверки здесь достаточно, чтобы закрыть
    гонку с Celery-задачей `finalize_expired_checklist_grace_periods`: она фиксирует
    `incomplete` через `UPDATE ... WHERE status='pending'`, который блокирует те же
    строки, что и наш `SELECT ... FOR UPDATE` ниже. Если задача успела закоммититься
    первой (окно закрылось между `_reassert_fill_window_open` и этим вызовом), наш
    `SELECT` дождётся её коммита и увидит уже `incomplete`; если нет — мы отработаем
    первыми, и задаче на следующем тике будет просто нечего финализировать."""
    # Блокируем строку экземпляра до конца транзакции: конкурентные пересчёты одного
    # instance по РАЗНЫМ пунктам (PATCH vs привязка фото) иначе читают устаревший снимок
    # друг друга и статус может «застрять» в pending. Лок именно на строке instance;
    # авто-финиш (`auto_finish_stale_shifts`) тоже сперва трогает строки экземпляров,
    # цикла блокировок с ним нет. Читаем `status` в том же запросе — после получения
    # блокировки это гарантированно самое свежее закоммиченное значение.
    locked_status = (
        await session.execute(
            select(ChecklistInstance.status)
            .where(ChecklistInstance.id == instance.id)
            .with_for_update()
        )
    ).scalar_one()
    if locked_status == ChecklistInstanceStatus.incomplete:
        instance.status = locked_status
        return

    photos_count_subq = (
        select(func.count(ChecklistItemPhoto.id))
        .where(ChecklistItemPhoto.instance_item_id == ChecklistInstanceItem.id)
        .correlate(ChecklistInstanceItem)
        .scalar_subquery()
    )
    blocking_result = await session.execute(
        select(func.count()).where(
            ChecklistInstanceItem.instance_id == instance.id,
            ChecklistInstanceItem.is_required.is_(True),
            or_(
                ChecklistInstanceItem.is_completed.is_(False),
                and_(
                    ChecklistInstanceItem.photo_requirement == PhotoRequirement.required,
                    photos_count_subq == 0,
                ),
            ),
        )
    )
    blocking = blocking_result.scalar_one()

    now = datetime.now(UTC)
    if blocking == 0:
        if instance.status != ChecklistInstanceStatus.completed:
            instance.status = ChecklistInstanceStatus.completed
            instance.completed_at = now
    else:
        if instance.status != ChecklistInstanceStatus.pending:
            instance.status = ChecklistInstanceStatus.pending
            instance.completed_at = None

    await session.flush()


def _per_item_photo_subquery(instance_ids: list[uuid.UUID]) -> Any:
    """Per-item photo counts (LEFT JOIN) для набора экземпляров — переиспользуется
    `get_shift_checklists` и реестром организации (`_items_summary_by_instance`)."""
    return (
        select(
            ChecklistInstanceItem.instance_id.label("instance_id"),
            ChecklistInstanceItem.is_completed.label("is_completed"),
            ChecklistInstanceItem.photo_requirement.label("photo_requirement"),
            func.count(ChecklistItemPhoto.id).label("photos_count"),
        )
        .select_from(ChecklistInstanceItem)
        .outerjoin(
            ChecklistItemPhoto,
            ChecklistItemPhoto.instance_item_id == ChecklistInstanceItem.id,
        )
        .where(ChecklistInstanceItem.instance_id.in_(instance_ids))
        .group_by(ChecklistInstanceItem.id)
        .subquery()
    )


async def _items_summary_by_instance(
    session: AsyncSession,
    instance_ids: list[uuid.UUID],
    *,
    with_photos_total: bool = False,
) -> dict[uuid.UUID, Any]:
    """instance_id -> row(total, completed, satisfied_count, photos_required_missing,
    [photos_count]).

    Один агрегирующий запрос (GROUP BY) поверх `_per_item_photo_subquery` — без N+1
    на каждый экземпляр. `with_photos_total` добавляет суммарное число фото
    экземпляра (нужно реестру организации, не нужно детали смены).
    """
    if not instance_ids:
        return {}

    per_item = _per_item_photo_subquery(instance_ids)
    completed_case = case((per_item.c.is_completed.is_(True), 1), else_=0)
    satisfied_case = case(
        (
            and_(
                per_item.c.is_completed.is_(True),
                or_(
                    per_item.c.photo_requirement != PhotoRequirement.required,
                    per_item.c.photos_count >= 1,
                ),
            ),
            1,
        ),
        else_=0,
    )
    missing_case = case(
        (
            and_(
                per_item.c.photo_requirement == PhotoRequirement.required,
                per_item.c.photos_count == 0,
            ),
            1,
        ),
        else_=0,
    )
    columns = [
        per_item.c.instance_id,
        func.count().label("total"),
        func.coalesce(func.sum(completed_case), 0).label("completed"),
        func.coalesce(func.sum(satisfied_case), 0).label("satisfied_count"),
        func.coalesce(func.sum(missing_case), 0).label("photos_required_missing"),
    ]
    if with_photos_total:
        columns.append(func.coalesce(func.sum(per_item.c.photos_count), 0).label("photos_count"))

    summary_result = await session.execute(select(*columns).group_by(per_item.c.instance_id))
    return {row.instance_id: row for row in summary_result.all()}


async def get_shift_checklists(
    session: AsyncSession,
    shift_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> list[tuple[ChecklistInstance, int, int, int, int]]:
    """Возвращает (instance, total, completed, satisfied_count, photos_required_missing)."""
    shift = await _get_shift(session, shift_id)
    await _check_shift_access(session, shift, requester_id)

    result = await session.execute(
        select(ChecklistInstance)
        .where(ChecklistInstance.shift_id == shift_id)
        .order_by(ChecklistInstance.created_at)
    )
    instances = list(result.scalars().all())
    if not instances:
        return []

    instance_ids = [i.id for i in instances]
    by_instance = await _items_summary_by_instance(session, instance_ids)

    out: list[tuple[ChecklistInstance, int, int, int, int]] = []
    for inst in instances:
        row = by_instance.get(inst.id)
        if row is None:
            out.append((inst, 0, 0, 0, 0))
        else:
            out.append(
                (
                    inst,
                    int(row.total),
                    int(row.completed),
                    int(row.satisfied_count),
                    int(row.photos_required_missing),
                )
            )
    return out


async def get_instance_detail(
    session: AsyncSession,
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    requester_id: uuid.UUID,
) -> ChecklistInstance:
    shift = await _get_shift(session, shift_id)
    await _check_shift_access(session, shift, requester_id)

    result = await session.execute(
        select(ChecklistInstance)
        .options(
            selectinload(ChecklistInstance.items)
            .selectinload(ChecklistInstanceItem.photos)
            .selectinload(ChecklistItemPhoto.file)
        )
        .where(
            ChecklistInstance.id == instance_id,
            ChecklistInstance.shift_id == shift_id,
        )
    )
    instance = result.scalar_one_or_none()
    if instance is None:
        raise ChecklistError("INSTANCE_NOT_FOUND", "Экземпляр не найден", 404)
    return instance


async def update_instance_item(
    session: AsyncSession,
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    item_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    is_completed: bool,
    comment: str | None,
) -> ChecklistInstanceItem:
    shift = await _get_shift(session, shift_id)

    if shift.user_id != requester_id:
        raise ChecklistError("FORBIDDEN", "Заполнять может только владелец смены", 403)

    await _assert_fill_window_open(session, shift)

    instance_result = await session.execute(
        select(ChecklistInstance).where(
            ChecklistInstance.id == instance_id,
            ChecklistInstance.shift_id == shift_id,
        )
    )
    instance = instance_result.scalar_one_or_none()
    if instance is None:
        raise ChecklistError("INSTANCE_NOT_FOUND", "Экземпляр не найден", 404)

    item_result = await session.execute(
        select(ChecklistInstanceItem)
        .options(selectinload(ChecklistInstanceItem.photos).selectinload(ChecklistItemPhoto.file))
        .where(
            ChecklistInstanceItem.id == item_id,
            ChecklistInstanceItem.instance_id == instance_id,
        )
    )
    item = item_result.scalar_one_or_none()
    if item is None:
        raise ChecklistError("ITEM_NOT_FOUND", "Пункт не найден", 404)

    # Повторная проверка окна прямо перед мутацией (гонка с истечением окна/авто-финишем).
    await _reassert_fill_window_open(session, shift)

    now = datetime.now(UTC)
    if item.is_completed != is_completed:
        item.is_completed = is_completed
        item.completed_at = now if is_completed else None
    item.comment = comment
    item.change_count = (item.change_count or 0) + 1

    await session.flush()
    await _recompute_instance_status(session, instance)
    await _refresh_live_incomplete_flag(session, shift)
    return item


async def _load_instance_for_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
) -> ChecklistInstance:
    instance = (
        await session.execute(
            select(ChecklistInstance).where(
                ChecklistInstance.id == instance_id,
                ChecklistInstance.shift_id == shift_id,
            )
        )
    ).scalar_one_or_none()
    if instance is None:
        raise ChecklistError("INSTANCE_NOT_FOUND", "Экземпляр не найден", 404)
    return instance


async def _ensure_owner_fillable_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    forbidden_message: str,
) -> Shift:
    shift = await _get_shift(session, shift_id)
    if shift.user_id != user_id:
        raise ChecklistError("FORBIDDEN", forbidden_message, 403)
    await _assert_fill_window_open(session, shift)
    return shift


async def attach_photo(
    session: AsyncSession,
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    item_id: uuid.UUID,
    user: User,
    *,
    file_id: uuid.UUID,
    captured_at: datetime | None,
    latitude: float | None,
    longitude: float | None,
) -> tuple[ChecklistItemPhoto, File]:
    """Привязать уже загруженный файл к пункту-экземпляру (одна транзакция)."""
    shift = await _ensure_owner_fillable_shift(
        session,
        shift_id,
        user.id,
        forbidden_message="Добавлять фото может только владелец смены",
    )
    await _load_instance_for_shift(session, shift_id, instance_id)

    # Блокируем строку пункта (FOR UPDATE) — защита от гонки по лимиту фото.
    item = (
        await session.execute(
            select(ChecklistInstanceItem)
            .where(
                ChecklistInstanceItem.id == item_id,
                ChecklistInstanceItem.instance_id == instance_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if item is None:
        raise ChecklistError("ITEM_NOT_FOUND", "Пункт не найден", 404)

    if item.photo_requirement == PhotoRequirement.none:
        raise ChecklistError("PHOTO_NOT_ALLOWED", "К этому пункту нельзя прикреплять фото", 400)

    # Любая проблема с файлом-кандидатом → единый PHOTO_FILE_INVALID (мобилка перезаливает).
    file = (await session.execute(select(File).where(File.id == file_id))).scalar_one_or_none()
    if (
        file is None
        or file.category != FileCategory.checklist_photo
        or file.owner_user_id != user.id
        or file.organization_id != shift.organization_id
    ):
        raise ChecklistError("PHOTO_FILE_INVALID", "Файл недоступен для привязки", 400)

    already = (
        await session.execute(
            select(ChecklistItemPhoto.id).where(ChecklistItemPhoto.file_id == file_id)
        )
    ).scalar_one_or_none()
    if already is not None:
        raise ChecklistError("PHOTO_FILE_INVALID", "Файл недоступен для привязки", 400)

    current_count = (
        await session.execute(
            select(func.count()).where(ChecklistItemPhoto.instance_item_id == item_id)
        )
    ).scalar_one()
    if current_count >= settings.checklist_max_photos_per_item:
        raise ChecklistError("PHOTO_LIMIT_EXCEEDED", "Достигнут лимит фото на пункт", 409)

    next_position = (
        await session.execute(
            select(func.coalesce(func.max(ChecklistItemPhoto.position), -1)).where(
                ChecklistItemPhoto.instance_item_id == item_id
            )
        )
    ).scalar_one() + 1

    # Повторная проверка окна прямо перед мутацией (гонка с истечением окна/авто-финишем).
    await _reassert_fill_window_open(session, shift)

    file.is_attached = True
    photo = ChecklistItemPhoto(
        instance_item_id=item_id,
        file_id=file_id,
        captured_at=captured_at,
        latitude=latitude,
        longitude=longitude,
        position=next_position,
    )
    session.add(photo)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Параллельный второй POST того же файла упал на UNIQUE(file_id).
        await session.rollback()
        raise ChecklistError("PHOTO_FILE_INVALID", "Файл недоступен для привязки", 400) from exc

    instance = await _load_instance_for_shift(session, shift_id, instance_id)
    await _recompute_instance_status(session, instance)
    await _refresh_live_incomplete_flag(session, shift)
    logger.info(
        "checklist_photo_attached",
        shift_id=str(shift_id),
        item_id=str(item_id),
        file_id=str(file_id),
    )
    return photo, file


async def detach_photo(
    session: AsyncSession,
    shift_id: uuid.UUID,
    instance_id: uuid.UUID,
    item_id: uuid.UUID,
    photo_id: uuid.UUID,
    user: User,
) -> None:
    """Отвязать и физически удалить фото (объект S3 + строки files и связи)."""
    shift = await _ensure_owner_fillable_shift(
        session,
        shift_id,
        user.id,
        forbidden_message="Удалять фото может только владелец смены",
    )
    instance = await _load_instance_for_shift(session, shift_id, instance_id)

    item = (
        await session.execute(
            select(ChecklistInstanceItem).where(
                ChecklistInstanceItem.id == item_id,
                ChecklistInstanceItem.instance_id == instance_id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise ChecklistError("ITEM_NOT_FOUND", "Пункт не найден", 404)

    photo = (
        await session.execute(
            select(ChecklistItemPhoto).where(
                ChecklistItemPhoto.id == photo_id,
                ChecklistItemPhoto.instance_item_id == item_id,
            )
        )
    ).scalar_one_or_none()
    if photo is None:
        raise ChecklistError("PHOTO_NOT_FOUND", "Фото не найдено", 404)

    from src.app.services.file_storage import delete_file

    # Повторная проверка окна прямо перед мутацией (гонка с истечением окна/авто-финишем).
    await _reassert_fill_window_open(session, shift)

    file_id = photo.file_id
    # Снять привязку до delete_file (тот бросает FILE_IN_USE для is_attached=true).
    await session.delete(photo)
    file = (await session.execute(select(File).where(File.id == file_id))).scalar_one_or_none()
    if file is not None:
        file.is_attached = False
        await session.flush()
        await delete_file(session, file_id, user)
    else:
        await session.flush()

    await _recompute_instance_status(session, instance)
    await _refresh_live_incomplete_flag(session, shift)
    logger.info(
        "checklist_photo_detached",
        shift_id=str(shift_id),
        item_id=str(item_id),
        photo_id=str(photo_id),
    )


async def cleanup_shift_photo_files(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user: User,
) -> int:
    """Удалить файлы всех привязанных фото смены (объект S3 + строку files) ДО
    каскадного удаления строк связи. Возвращает число удалённых файлов.

    Privacy-хук для будущего пути удаления смены: каскад ON DELETE снёс бы лишь
    строки checklist_item_photos, оставив files (is_attached=true) и объекты S3
    сиротами, которых cleanup_orphan_files НЕ подбирает. В текущей архитектуре
    смены не hard-удаляются (орг — soft-delete), прямого триггера нет —
    подключается из пути удаления смены, когда он появится."""
    from src.app.services.file_storage import delete_file

    file_ids = list(
        (
            await session.execute(
                select(ChecklistItemPhoto.file_id)
                .join(
                    ChecklistInstanceItem,
                    ChecklistItemPhoto.instance_item_id == ChecklistInstanceItem.id,
                )
                .join(
                    ChecklistInstance,
                    ChecklistInstanceItem.instance_id == ChecklistInstance.id,
                )
                .where(ChecklistInstance.shift_id == shift_id)
            )
        )
        .scalars()
        .all()
    )
    deleted = 0
    for file_id in file_ids:
        file = (await session.execute(select(File).where(File.id == file_id))).scalar_one_or_none()
        if file is None:
            continue
        file.is_attached = False
        await session.flush()
        await delete_file(session, file_id, user)
        deleted += 1
    return deleted


async def finalize_shift_checklists(
    session: AsyncSession,
    shift_id: uuid.UUID,
) -> bool:
    """Терминально зафиксировать статус обязательных чек-листов: незакрытые
    pending → incomplete. Возврата в pending для этих экземпляров больше нет —
    вызывать только когда окно дозаполнения точно закрыто (`checklist_grace_minutes
    = 0` при завершении смены — см. `close_shift_checklists`, — либо по истечении
    окна — см. Celery-задачу `finalize_expired_checklist_grace_periods`).

    Статус каждого экземпляра поддерживается свежим через _recompute_instance_status
    на каждом PATCH/привязке/отвязке фото, поэтому «pending обязательный» здесь уже
    означает «остались не-satisfied обязательные пункты» (включая отсутствие
    обязательного фото). Returns True если у смены остались незакрытые обязательные
    экземпляры. Caller выставляет shift.has_incomplete_required_checklists.
    """
    await session.execute(
        update(ChecklistInstance)
        .where(
            ChecklistInstance.shift_id == shift_id,
            ChecklistInstance.status == ChecklistInstanceStatus.pending,
            ChecklistInstance.is_required.is_(True),
        )
        .values(status=ChecklistInstanceStatus.incomplete)
    )
    # После UPDATE ни один обязательный экземпляр не остаётся pending — «не completed»
    # здесь равносильно «incomplete», проверка та же, что и во время открытого окна.
    has_incomplete = await _has_live_incomplete_required(session, shift_id)
    await session.flush()
    return has_incomplete


# --- checklist_reports: сводка в ShiftResponse и реестр организации ---------


@dataclass(frozen=True)
class ShiftChecklistsSummary:
    """Сводка по чек-листам одной смены для `ShiftResponse.checklists_summary`."""

    total: int
    completed: int
    required_total: int
    required_incomplete: int


ZERO_SHIFT_CHECKLISTS_SUMMARY = ShiftChecklistsSummary(0, 0, 0, 0)


async def get_checklists_summary_for_shifts(
    session: AsyncSession,
    shift_ids: list[uuid.UUID],
) -> dict[uuid.UUID, ShiftChecklistsSummary]:
    """shift_id -> сводка по чек-листам, один агрегирующий запрос (без N+1).

    Используется ТОЛЬКО в орг-эндпоинтах смен (список/деталь); персональные
    эндпоинты не вызывают эту функцию — там `checklists_summary` остаётся `null`.
    """
    if not shift_ids:
        return {}

    completed_case = case(
        (ChecklistInstance.status == ChecklistInstanceStatus.completed, 1), else_=0
    )
    required_case = case((ChecklistInstance.is_required.is_(True), 1), else_=0)
    required_incomplete_case = case(
        (
            and_(
                ChecklistInstance.is_required.is_(True),
                ChecklistInstance.status != ChecklistInstanceStatus.completed,
            ),
            1,
        ),
        else_=0,
    )
    result = await session.execute(
        select(
            ChecklistInstance.shift_id,
            func.count().label("total"),
            func.coalesce(func.sum(completed_case), 0).label("completed"),
            func.coalesce(func.sum(required_case), 0).label("required_total"),
            func.coalesce(func.sum(required_incomplete_case), 0).label("required_incomplete"),
        )
        .where(ChecklistInstance.shift_id.in_(shift_ids))
        .group_by(ChecklistInstance.shift_id)
    )
    return {
        row.shift_id: ShiftChecklistsSummary(
            total=int(row.total),
            completed=int(row.completed),
            required_total=int(row.required_total),
            required_incomplete=int(row.required_incomplete),
        )
        for row in result.all()
    }


VALID_ORG_INSTANCE_SORTS = {"shift_started_at", "completed_at", "created_at"}

_ORG_INSTANCE_SORT_COLUMNS = {
    "shift_started_at": Shift.started_at,
    "completed_at": ChecklistInstance.completed_at,
    "created_at": ChecklistInstance.created_at,
}


def _org_instance_order_by(sort: str, order: str) -> Any:
    column = _ORG_INSTANCE_SORT_COLUMNS.get(sort, Shift.started_at)
    return column.asc() if order.lower() == "asc" else column.desc()


@dataclass(frozen=True)
class OrgChecklistInstanceRow:
    """Одна строка реестра `GET /organizations/{org_id}/checklist-instances`."""

    instance: ChecklistInstance
    shift: Shift
    user: User | None
    display_name: str | None
    work_location: WorkLocation | None
    items_total: int
    items_completed: int
    satisfied_count: int
    photos_required_missing: int
    photos_count: int


async def list_org_checklist_instances(
    session: AsyncSession,
    org_id: uuid.UUID,
    requester_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None = None,
    template_id: uuid.UUID | None = None,
    type_: str | None = None,
    status: str | None = None,
    state: str | None = None,
    is_required: bool | None = None,
    work_location_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
    sort: str = "shift_started_at",
    order: str = "desc",
) -> tuple[list[OrgChecklistInstanceRow], int]:
    """Реестр экземпляров чек-листов организации: фильтры + сортировка + пагинация.

    Только owner/admin организации. Персональные смены (`organization_id is null`)
    и `is_deleted=true` не попадают в выдачу никогда (`checklist_reports`).
    """
    org = await get_organization(session, org_id)
    await ensure_admin_or_owner(
        session,
        org,
        requester_id,
        message="Нет прав на просмотр чек-листов организации",
    )

    validate_date_range(date_from, date_to)

    if sort not in VALID_ORG_INSTANCE_SORTS:
        raise ChecklistError(
            "INVALID_SORT",
            f"Сортировка должна быть: {', '.join(sorted(VALID_ORG_INSTANCE_SORTS))}",
            400,
        )

    type_enum = None
    if type_ is not None:
        try:
            type_enum = ChecklistType(type_)
        except ValueError:
            raise ChecklistError(
                "INVALID_TYPE",
                f"Тип должен быть: {', '.join(t.value for t in ChecklistType)}",
                400,
            ) from None

    status_enum = None
    if status is not None:
        try:
            status_enum = ChecklistInstanceStatus(status)
        except ValueError:
            raise ChecklistError(
                "INVALID_STATUS",
                f"Статус должен быть: {', '.join(s.value for s in ChecklistInstanceStatus)}",
                400,
            ) from None

    if state is not None and state not in {"completed", "not_completed"}:
        raise ChecklistError(
            "INVALID_STATE",
            "Состояние должно быть: completed, not_completed",
            400,
        )

    conditions = [Shift.organization_id == org_id, Shift.is_deleted.is_(False)]
    if user_id is not None:
        conditions.append(Shift.user_id == user_id)
    if template_id is not None:
        conditions.append(ChecklistInstance.template_id == template_id)
    if type_enum is not None:
        conditions.append(ChecklistInstance.type == type_enum)
    if status_enum is not None:
        # status приоритетнее state — при обоих переданных state игнорируется.
        conditions.append(ChecklistInstance.status == status_enum)
    elif state == "completed":
        conditions.append(ChecklistInstance.status == ChecklistInstanceStatus.completed)
    elif state == "not_completed":
        conditions.append(ChecklistInstance.status != ChecklistInstanceStatus.completed)
    if is_required is not None:
        conditions.append(ChecklistInstance.is_required.is_(is_required))
    if work_location_id is not None:
        conditions.append(Shift.work_location_id == work_location_id)
    if date_from is not None:
        conditions.append(Shift.started_at >= ensure_utc(date_from))
    if date_to is not None:
        conditions.append(Shift.started_at <= ensure_utc(date_to))

    count_query = (
        select(func.count())
        .select_from(ChecklistInstance)
        .join(Shift, ChecklistInstance.shift_id == Shift.id)
        .where(*conditions)
    )
    total = (await session.execute(count_query)).scalar_one()

    page_query = (
        select(ChecklistInstance, Shift, User, WorkLocation, OrganizationMember)
        .join(Shift, ChecklistInstance.shift_id == Shift.id)
        .outerjoin(User, Shift.user_id == User.id)
        .outerjoin(WorkLocation, Shift.work_location_id == WorkLocation.id)
        .outerjoin(
            OrganizationMember,
            and_(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == Shift.user_id,
            ),
        )
        .where(*conditions)
        .order_by(_org_instance_order_by(sort, order))
        .limit(limit)
        .offset(offset)
    )
    page_rows = (await session.execute(page_query)).all()

    instance_ids = [instance.id for instance, _shift, _user, _location, _member in page_rows]
    summary_by_instance = await _items_summary_by_instance(
        session, instance_ids, with_photos_total=True
    )

    out: list[OrgChecklistInstanceRow] = []
    for instance, shift, user, work_location, member in page_rows:
        summary = summary_by_instance.get(instance.id)
        out.append(
            OrgChecklistInstanceRow(
                instance=instance,
                shift=shift,
                user=user,
                display_name=member.display_name if member is not None else None,
                work_location=work_location,
                items_total=int(summary.total) if summary is not None else 0,
                items_completed=int(summary.completed) if summary is not None else 0,
                satisfied_count=int(summary.satisfied_count) if summary is not None else 0,
                photos_required_missing=(
                    int(summary.photos_required_missing) if summary is not None else 0
                ),
                photos_count=int(summary.photos_count) if summary is not None else 0,
            )
        )
    return out, total
