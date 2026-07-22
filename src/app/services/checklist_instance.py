import uuid
from datetime import UTC, datetime

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
    PhotoRequirement,
)
from src.app.models.file import File, FileCategory
from src.app.models.organization import OrganizationMember
from src.app.models.shift import Shift, ShiftStatus
from src.app.models.user import User
from src.app.services.checklist_assignment import _compute_effective
from src.app.services.checklist_location import get_location_ids_for_templates, matches_location
from src.app.services.checklist_template import ChecklistError

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
    при реальной смене статуса (без лишнего churn updated_at/онлайна)."""
    # Блокируем строку экземпляра до конца транзакции: конкурентные пересчёты одного
    # instance по РАЗНЫМ пунктам (PATCH vs привязка фото) иначе читают устаревший снимок
    # друг друга и статус может «застрять» в pending. Лок именно на строке instance;
    # авто-финиш тоже сперва трогает строки экземпляров, цикла блокировок с ним нет.
    await session.execute(
        select(ChecklistInstance.id)
        .where(ChecklistInstance.id == instance.id)
        .with_for_update()
    )
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
    # Per-item photo counts (LEFT JOIN), затем агрегаты per-instance — один запрос.
    per_item = (
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
    summary_result = await session.execute(
        select(
            per_item.c.instance_id,
            func.count().label("total"),
            func.coalesce(func.sum(completed_case), 0).label("completed"),
            func.coalesce(func.sum(satisfied_case), 0).label("satisfied_count"),
            func.coalesce(func.sum(missing_case), 0).label("photos_required_missing"),
        ).group_by(per_item.c.instance_id)
    )
    by_instance = {row.instance_id: row for row in summary_result.all()}

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

    from src.app.models.shift import ShiftStatus

    if shift.status == ShiftStatus.finished:
        raise ChecklistError(
            "SHIFT_FINISHED",
            "Нельзя редактировать чек-листы завершённой смены",
            400,
        )

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
        .options(
            selectinload(ChecklistInstanceItem.photos).selectinload(ChecklistItemPhoto.file)
        )
        .where(
            ChecklistInstanceItem.id == item_id,
            ChecklistInstanceItem.instance_id == instance_id,
        )
    )
    item = item_result.scalar_one_or_none()
    if item is None:
        raise ChecklistError("ITEM_NOT_FOUND", "Пункт не найден", 404)

    now = datetime.now(UTC)
    if item.is_completed != is_completed:
        item.is_completed = is_completed
        item.completed_at = now if is_completed else None
    item.comment = comment
    item.change_count = (item.change_count or 0) + 1

    await session.flush()
    await _recompute_instance_status(session, instance)
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


async def _ensure_owner_active_shift(
    session: AsyncSession,
    shift_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    forbidden_message: str,
) -> Shift:
    shift = await _get_shift(session, shift_id)
    if shift.user_id != user_id:
        raise ChecklistError("FORBIDDEN", forbidden_message, 403)
    _assert_shift_active(shift)
    return shift


def _assert_shift_active(shift: Shift) -> None:
    if shift.status == ShiftStatus.finished:
        raise ChecklistError(
            "SHIFT_FINISHED",
            "Нельзя редактировать чек-листы завершённой смены",
            400,
        )


async def _reassert_shift_active(session: AsyncSession, shift: Shift) -> None:
    """Повторная проверка статуса смены непосредственно перед мутацией.

    Защита от гонки с авто-завершением (ТЗ): между первой проверкой и коммитом
    смену мог завершить Celery/инлайн auto-finish. Делаем свежее чтение (без
    FOR UPDATE — лок строки shifts создал бы цикл с auto-finish, который сперва
    лочит строки экземпляров, затем строку смены) и под READ COMMITTED видим уже
    закоммиченный finished → SHIFT_FINISHED. Остаточное окно (финиш между этим
    чтением и нашим коммитом) допустимо: файл останется сиротой и его уберёт
    cleanup_orphan_files (см. backend.md «Транзакции и гонки»)."""
    await session.refresh(shift, attribute_names=["status"])
    _assert_shift_active(shift)


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
    shift = await _ensure_owner_active_shift(
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
    file = (
        await session.execute(select(File).where(File.id == file_id))
    ).scalar_one_or_none()
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

    # Повторная проверка статуса смены прямо перед мутацией (гонка с авто-финишем).
    await _reassert_shift_active(session, shift)

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
    shift = await _ensure_owner_active_shift(
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

    # Повторная проверка статуса смены прямо перед мутацией (гонка с авто-финишем).
    await _reassert_shift_active(session, shift)

    file_id = photo.file_id
    # Снять привязку до delete_file (тот бросает FILE_IN_USE для is_attached=true).
    await session.delete(photo)
    file = (
        await session.execute(select(File).where(File.id == file_id))
    ).scalar_one_or_none()
    if file is not None:
        file.is_attached = False
        await session.flush()
        await delete_file(session, file_id, user)
    else:
        await session.flush()

    await _recompute_instance_status(session, instance)
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
        file = (
            await session.execute(select(File).where(File.id == file_id))
        ).scalar_one_or_none()
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
    """Mark pending required instances as incomplete.

    Статус каждого экземпляра поддерживается свежим через _recompute_instance_status
    на каждом PATCH/привязке/отвязке фото, поэтому «pending обязательный» здесь уже
    означает «остались не-satisfied обязательные пункты» (включая отсутствие
    обязательного фото). Returns True если у смены есть incomplete-обязательные
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

    incomplete_count_result = await session.execute(
        select(func.count()).where(
            ChecklistInstance.shift_id == shift_id,
            ChecklistInstance.status == ChecklistInstanceStatus.incomplete,
            ChecklistInstance.is_required.is_(True),
        )
    )
    has_incomplete = incomplete_count_result.scalar_one() > 0
    await session.flush()
    return has_incomplete
