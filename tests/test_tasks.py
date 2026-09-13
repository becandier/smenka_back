# tests/test_tasks.py
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from src.app.core.security import hash_password
from src.app.core.storage import ObjectSummary
from src.app.models.audit_log import AuditLog
from src.app.models.checklist import (
    ChecklistInstance,
    ChecklistInstanceStatus,
    ChecklistType,
)
from src.app.models.file import File, FileCategory
from src.app.models.organization import Organization
from src.app.models.organization_settings import OrganizationSettings
from src.app.models.shift import Pause, Shift, ShiftFinishReason, ShiftStatus
from src.app.models.user import RefreshToken, User, VerificationCode
from src.app.models.work_schedule import WorkSchedule
from src.app.services.file_storage import CATEGORY_POLICIES
from src.app.tasks import cleanup as cleanup_tasks
from src.app.tasks.cleanup import (
    cleanup_expired_tokens,
    cleanup_orphan_files,
    purge_expired_files,
    reconcile_storage_objects,
)
from src.app.tasks.shifts import (
    auto_finish_stale_pauses,
    auto_finish_stale_shifts,
    finalize_expired_checklist_grace_periods,
)
from tests.conftest import TEST_DATABASE_URL_SYNC

# Все тесты модуля гоняют Celery-таски через отдельное синхронное подключение
# (get_sync_test_session ниже) — db_session должен коммитить по-настоящему,
# иначе таска не увидит данных теста. См. tests/conftest.py::db_session.
pytestmark = pytest.mark.db_real_commit

# TEST_DATABASE_URL_SYNC — из conftest, а не пересчитан здесь: под pytest-xdist
# (make test-fast) у каждого воркера своя суффиксированная база (см.
# tests/conftest.py::TEST_DB_NAME) — sync-подключение обязано смотреть в ТУ ЖЕ
# базу, что и db_session этого воркера, иначе Celery-таска не увидит данных,
# которые тест закоммитил.
sync_test_engine = create_engine(TEST_DATABASE_URL_SYNC, echo=False)
sync_test_session_factory = sessionmaker(sync_test_engine, expire_on_commit=False)


@contextmanager
def get_sync_test_session() -> Generator[Session]:
    session = sync_test_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _make_user(
    *,
    user_id: uuid.UUID | None = None,
    email: str | None = None,
) -> User:
    return User(
        id=user_id or uuid.uuid4(),
        email=email or f"task-test-{uuid.uuid4().hex[:8]}@example.com",
        password_hash=hash_password("Test1234"),
        name="Task Test User",
        is_verified=True,
    )


def _make_org(
    *,
    owner_id: uuid.UUID,
    org_id: uuid.UUID | None = None,
    is_deleted: bool = False,
    deleted_at: datetime | None = None,
) -> Organization:
    return Organization(
        id=org_id or uuid.uuid4(),
        name="Test Org",
        owner_id=owner_id,
        is_deleted=is_deleted,
        deleted_at=deleted_at,
    )


def _make_schedule(org_id: uuid.UUID) -> WorkSchedule:
    from datetime import time as dt_time

    return WorkSchedule(
        id=uuid.uuid4(),
        organization_id=org_id,
        name="Дневная",
        start_time=dt_time(9, 0),
        end_time=dt_time(18, 0),
    )


def _make_pending_required_instance(shift_id: uuid.UUID) -> ChecklistInstance:
    """Обязательный экземпляр с одним незакрытым пунктом (checklist_grace_period:
    имитирует состояние «есть незаполненный обязательный чек-лист» без похода
    через полный API-флоу шаблонов/назначений)."""
    return ChecklistInstance(
        id=uuid.uuid4(),
        shift_id=shift_id,
        template_id=None,
        name="Открытие",
        type=ChecklistType.shift_start,
        is_required=True,
        status=ChecklistInstanceStatus.pending,
    )


class TestAutoFinishStaleShifts:
    """R4 (work_schedules): авто-завершение org-смен ровно в scheduled_end_at.
    Персональные смены больше не авто-завершаются вообще."""

    async def test_personal_shift_never_auto_finished(self, db_session: AsyncSession):
        """Personal shift started 100h ago -> NEVER auto-finished (feature removed)."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=None,
            started_at=datetime.now(UTC) - timedelta(hours=100),
            status=ShiftStatus.active,
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated = result.scalar_one()

        assert updated.status == ShiftStatus.active
        assert updated.finished_at is None

    async def test_org_shift_finished_exactly_at_scheduled_end(self, db_session: AsyncSession):
        """scheduled_end_at in the past -> finished_at == scheduled_end_at (not "now")."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        schedule = _make_schedule(org.id)
        db_session.add(schedule)
        org_settings = OrganizationSettings(id=uuid.uuid4(), organization_id=org.id)
        db_session.add(org_settings)
        await db_session.flush()

        started = datetime.now(UTC) - timedelta(hours=2)
        scheduled_end = datetime.now(UTC) - timedelta(minutes=5)
        shift_id = uuid.uuid4()
        # Фиксируем значения ДО expire_all — иначе доступ к ORM-атрибутам после
        # него триггерит ленивую async-загрузку в синхронном контексте.
        schedule_id, schedule_name = schedule.id, schedule.name
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=started,
            status=ShiftStatus.active,
            work_schedule_id=schedule_id,
            schedule_name=schedule_name,
            scheduled_start_at=started,
            scheduled_end_at=scheduled_end,
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated = result.scalar_one()

        assert updated.status == ShiftStatus.finished
        assert updated.finished_at == scheduled_end
        assert updated.finish_reason == ShiftFinishReason.auto_schedule

        audit_result = await db_session.execute(
            select(AuditLog).where(AuditLog.resource_id == shift_id)
        )
        audit = audit_result.scalar_one()
        assert audit.action == "shift.auto_finish"
        assert audit.actor_user_id is None
        assert audit.summary["work_schedule_id"] == str(schedule_id)
        assert audit.summary["schedule_name"] == "Дневная"

    async def test_org_shift_skipped_when_auto_finish_by_schedule_disabled(
        self, db_session: AsyncSession
    ):
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(),
            organization_id=org.id,
            auto_finish_by_schedule=False,
        )
        db_session.add(org_settings)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.active,
            scheduled_start_at=datetime.now(UTC) - timedelta(hours=2),
            scheduled_end_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated = result.scalar_one()

        assert updated.status == ShiftStatus.active
        assert updated.finished_at is None

    async def test_org_shift_without_missing_settings_defaults_to_enabled(
        self, db_session: AsyncSession
    ):
        """No OrganizationSettings row at all -> still auto-finished (server_default true)."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()
        # Intentionally no OrganizationSettings row.

        scheduled_end = datetime.now(UTC) - timedelta(minutes=1)
        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.active,
            scheduled_start_at=datetime.now(UTC) - timedelta(hours=2),
            scheduled_end_at=scheduled_end,
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated = result.scalar_one()
        assert updated.status == ShiftStatus.finished
        assert updated.finished_at == scheduled_end

    async def test_org_shift_without_schedule_not_finished(self, db_session: AsyncSession):
        """scheduled_end_at is null -> never auto-finished, regardless of started_at age."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(id=uuid.uuid4(), organization_id=org.id)
        db_session.add(org_settings)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=100),
            status=ShiftStatus.active,
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated = result.scalar_one()
        assert updated.status == ShiftStatus.active
        assert updated.finished_at is None

    async def test_org_shift_future_scheduled_end_not_finished(self, db_session: AsyncSession):
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(id=uuid.uuid4(), organization_id=org.id)
        db_session.add(org_settings)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            status=ShiftStatus.active,
            scheduled_start_at=datetime.now(UTC) - timedelta(hours=1),
            scheduled_end_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session.add(shift)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated = result.scalar_one()
        assert updated.status == ShiftStatus.active

    async def test_stale_shift_pauses_closed_at_scheduled_end(self, db_session: AsyncSession):
        """Stale shift with an open pause -> pause.finished_at = shift.finished_at

        (= scheduled_end_at)."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(id=uuid.uuid4(), organization_id=org.id)
        db_session.add(org_settings)
        await db_session.flush()

        started = datetime.now(UTC) - timedelta(hours=2)
        scheduled_end = datetime.now(UTC) - timedelta(minutes=5)
        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=started,
            status=ShiftStatus.paused,
            scheduled_start_at=started,
            scheduled_end_at=scheduled_end,
        )
        db_session.add(shift)
        await db_session.flush()

        pause_id = uuid.uuid4()
        pause = Pause(
            id=pause_id,
            shift_id=shift_id,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            finished_at=None,
        )
        db_session.add(pause)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()

        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = result.scalar_one()
        assert updated_shift.status == ShiftStatus.finished
        assert updated_shift.finished_at == scheduled_end

        pause_result = await db_session.execute(select(Pause).where(Pause.id == pause_id))
        updated_pause = pause_result.scalar_one()
        assert updated_pause.finished_at == scheduled_end

    async def test_org_shift_with_grace_window_leaves_checklist_pending(
        self, db_session: AsyncSession
    ):
        """checklist_grace_period: авто-финиш по графику с `checklist_grace_minutes>0`
        не переводит незакрытый обязательный экземпляр в терминальный incomplete —
        окно дозаполнения открывается так же, как при ручном завершении."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(), organization_id=org.id, checklist_grace_minutes=30
        )
        db_session.add(org_settings)

        shift_id = uuid.uuid4()
        scheduled_end = datetime.now(UTC) - timedelta(minutes=5)
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.active,
            scheduled_start_at=datetime.now(UTC) - timedelta(hours=2),
            scheduled_end_at=scheduled_end,
        )
        db_session.add(shift)
        await db_session.flush()
        instance = _make_pending_required_instance(shift_id)
        instance_id = instance.id
        db_session.add(instance)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        shift_result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = shift_result.scalar_one()
        assert updated_shift.status == ShiftStatus.finished
        assert updated_shift.has_incomplete_required_checklists is True

        instance_result = await db_session.execute(
            select(ChecklistInstance).where(ChecklistInstance.id == instance_id)
        )
        assert instance_result.scalar_one().status == ChecklistInstanceStatus.pending

    async def test_org_shift_with_grace_disabled_finalizes_checklist_immediately(
        self, db_session: AsyncSession
    ):
        """checklist_grace_minutes=0 — прежнее поведение сохраняется и для
        авто-финиша по графику: терминальный incomplete сразу."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(), organization_id=org.id, checklist_grace_minutes=0
        )
        db_session.add(org_settings)

        shift_id = uuid.uuid4()
        scheduled_end = datetime.now(UTC) - timedelta(minutes=5)
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.active,
            scheduled_start_at=datetime.now(UTC) - timedelta(hours=2),
            scheduled_end_at=scheduled_end,
        )
        db_session.add(shift)
        await db_session.flush()
        instance = _make_pending_required_instance(shift_id)
        instance_id = instance.id
        db_session.add(instance)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_shifts()

        db_session.expire_all()
        shift_result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = shift_result.scalar_one()
        assert updated_shift.status == ShiftStatus.finished
        assert updated_shift.has_incomplete_required_checklists is True

        instance_result = await db_session.execute(
            select(ChecklistInstance).where(ChecklistInstance.id == instance_id)
        )
        assert instance_result.scalar_one().status == ChecklistInstanceStatus.incomplete


class TestFinalizeExpiredChecklistGracePeriods:
    """checklist_grace_period: терминальная фиксация чек-листов после того, как
    окно дозаполнения истекло (см. tasks/shifts.finalize_expired_checklist_grace_periods)."""

    async def test_window_elapsed_finalizes_to_incomplete(self, db_session: AsyncSession) -> None:
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(), organization_id=org.id, checklist_grace_minutes=30
        )
        db_session.add(org_settings)

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.finished,
            finished_at=datetime.now(UTC) - timedelta(minutes=31),
            has_incomplete_required_checklists=True,
        )
        db_session.add(shift)
        await db_session.flush()
        instance = _make_pending_required_instance(shift_id)
        instance_id = instance.id
        db_session.add(instance)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            finalize_expired_checklist_grace_periods()

        db_session.expire_all()
        instance_result = await db_session.execute(
            select(ChecklistInstance).where(ChecklistInstance.id == instance_id)
        )
        assert instance_result.scalar_one().status == ChecklistInstanceStatus.incomplete

        shift_result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        assert shift_result.scalar_one().has_incomplete_required_checklists is True

    async def test_running_twice_is_idempotent(self, db_session: AsyncSession) -> None:
        """checklist_grace_period, идемпотентность (финальное ревью, Находка 3):
        повторный прогон задачи на тех же данных не меняет уже зафиксированный
        результат и не падает — частичный индекс `ix_checklist_instances_pending_
        required` исключает уже финализированный экземпляр из кандидатов
        следующего тика (заявлено в докстроке задачи и в ADR-004, но напрямую не
        было проверено)."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(), organization_id=org.id, checklist_grace_minutes=30
        )
        db_session.add(org_settings)

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.finished,
            finished_at=datetime.now(UTC) - timedelta(minutes=31),
            has_incomplete_required_checklists=True,
        )
        db_session.add(shift)
        await db_session.flush()
        instance = _make_pending_required_instance(shift_id)
        instance_id = instance.id
        db_session.add(instance)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            finalize_expired_checklist_grace_periods()

        db_session.expire_all()
        after_first_run = (
            await db_session.execute(
                select(ChecklistInstance).where(ChecklistInstance.id == instance_id)
            )
        ).scalar_one()
        assert after_first_run.status == ChecklistInstanceStatus.incomplete
        completed_at_after_first_run = after_first_run.completed_at

        shift_after_first_run = (
            await db_session.execute(select(Shift).where(Shift.id == shift_id))
        ).scalar_one()
        assert shift_after_first_run.has_incomplete_required_checklists is True

        # Второй прогон на тех же данных, без каких-либо изменений между вызовами:
        # экземпляр уже не pending -> не попадает в кандидаты (частичный индекс),
        # задача должна быть no-op — ни статус, ни флаг не меняются, исключений нет.
        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            finalize_expired_checklist_grace_periods()

        db_session.expire_all()
        after_second_run = (
            await db_session.execute(
                select(ChecklistInstance).where(ChecklistInstance.id == instance_id)
            )
        ).scalar_one()
        assert after_second_run.status == ChecklistInstanceStatus.incomplete
        assert after_second_run.completed_at == completed_at_after_first_run

        shift_after_second_run = (
            await db_session.execute(select(Shift).where(Shift.id == shift_id))
        ).scalar_one()
        assert shift_after_second_run.has_incomplete_required_checklists is True

    async def test_window_still_open_not_finalized(self, db_session: AsyncSession) -> None:
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(), organization_id=org.id, checklist_grace_minutes=30
        )
        db_session.add(org_settings)

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.finished,
            finished_at=datetime.now(UTC) - timedelta(minutes=5),
            has_incomplete_required_checklists=True,
        )
        db_session.add(shift)
        await db_session.flush()
        instance = _make_pending_required_instance(shift_id)
        instance_id = instance.id
        db_session.add(instance)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            finalize_expired_checklist_grace_periods()

        db_session.expire_all()
        instance_result = await db_session.execute(
            select(ChecklistInstance).where(ChecklistInstance.id == instance_id)
        )
        # Окно ещё открыто (5 из 30 минут) — статус остаётся pending, дозаполнение
        # по-прежнему разрешено.
        assert instance_result.scalar_one().status == ChecklistInstanceStatus.pending

    async def test_missing_settings_row_defaults_to_30_minutes(
        self, db_session: AsyncSession
    ) -> None:
        """Нет строки OrganizationSettings -> считаем DEFAULT_CHECKLIST_GRACE_MINUTES
        (server_default), как и для остальных настроек с дефолтом."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()
        # Намеренно без строки OrganizationSettings.

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.finished,
            finished_at=datetime.now(UTC) - timedelta(minutes=31),
            has_incomplete_required_checklists=True,
        )
        db_session.add(shift)
        await db_session.flush()
        instance = _make_pending_required_instance(shift_id)
        instance_id = instance.id
        db_session.add(instance)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            finalize_expired_checklist_grace_periods()

        db_session.expire_all()
        instance_result = await db_session.execute(
            select(ChecklistInstance).where(ChecklistInstance.id == instance_id)
        )
        assert instance_result.scalar_one().status == ChecklistInstanceStatus.incomplete

    async def test_no_pending_required_instances_no_op(self, db_session: AsyncSession) -> None:
        """Идемпотентность: смена без pending-обязательных экземпляров не
        попадает в кандидаты (уже финализирована/выполнена ранее) — задача не
        трогает completed-экземпляры и не падает при пустой выборке."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.finished,
            finished_at=datetime.now(UTC) - timedelta(minutes=31),
            has_incomplete_required_checklists=False,
        )
        db_session.add(shift)
        await db_session.flush()
        instance = ChecklistInstance(
            id=uuid.uuid4(),
            shift_id=shift_id,
            template_id=None,
            name="Открытие",
            type=ChecklistType.shift_start,
            is_required=True,
            status=ChecklistInstanceStatus.completed,
        )
        instance_id = instance.id
        db_session.add(instance)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            finalize_expired_checklist_grace_periods()

        db_session.expire_all()
        instance_result = await db_session.execute(
            select(ChecklistInstance).where(ChecklistInstance.id == instance_id)
        )
        assert instance_result.scalar_one().status == ChecklistInstanceStatus.completed


class TestAutoFinishStalePauses:
    async def test_pause_exceeding_limit_finished(self, db_session: AsyncSession):
        """Org max_pause_minutes=30, pause started 35 min ago -> pause closed, shift active."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(),
            organization_id=org.id,
            max_pause_minutes=30,
        )
        db_session.add(org_settings)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.paused,
        )
        db_session.add(shift)
        await db_session.flush()

        pause_id = uuid.uuid4()
        pause = Pause(
            id=pause_id,
            shift_id=shift_id,
            started_at=datetime.now(UTC) - timedelta(minutes=35),
            finished_at=None,
        )
        db_session.add(pause)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_pauses()

        db_session.expire_all()

        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = result.scalar_one()
        assert updated_shift.status == ShiftStatus.active

        pause_result = await db_session.execute(select(Pause).where(Pause.id == pause_id))
        updated_pause = pause_result.scalar_one()
        assert updated_pause.finished_at is not None

    async def test_pause_within_limit_not_finished(self, db_session: AsyncSession):
        """Org max_pause_minutes=60, pause started 30 min ago -> stays open."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        org = _make_org(owner_id=user.id)
        db_session.add(org)
        await db_session.flush()

        org_settings = OrganizationSettings(
            id=uuid.uuid4(),
            organization_id=org.id,
            max_pause_minutes=60,
        )
        db_session.add(org_settings)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=org.id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.paused,
        )
        db_session.add(shift)
        await db_session.flush()

        pause_id = uuid.uuid4()
        pause = Pause(
            id=pause_id,
            shift_id=shift_id,
            started_at=datetime.now(UTC) - timedelta(minutes=30),
            finished_at=None,
        )
        db_session.add(pause)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_pauses()

        db_session.expire_all()

        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = result.scalar_one()
        assert updated_shift.status == ShiftStatus.paused

        pause_result = await db_session.execute(select(Pause).where(Pause.id == pause_id))
        updated_pause = pause_result.scalar_one()
        assert updated_pause.finished_at is None

    async def test_personal_pauses_not_affected(self, db_session: AsyncSession):
        """Personal shift (no org) with open pause -> not affected by auto-finish."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        shift_id = uuid.uuid4()
        shift = Shift(
            id=shift_id,
            user_id=user.id,
            organization_id=None,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            status=ShiftStatus.paused,
        )
        db_session.add(shift)
        await db_session.flush()

        pause_id = uuid.uuid4()
        pause = Pause(
            id=pause_id,
            shift_id=shift_id,
            started_at=datetime.now(UTC) - timedelta(minutes=120),
            finished_at=None,
        )
        db_session.add(pause)
        await db_session.commit()

        with patch("src.app.tasks.shifts.get_sync_session", get_sync_test_session):
            auto_finish_stale_pauses()

        db_session.expire_all()

        result = await db_session.execute(select(Shift).where(Shift.id == shift_id))
        updated_shift = result.scalar_one()
        assert updated_shift.status == ShiftStatus.paused

        pause_result = await db_session.execute(select(Pause).where(Pause.id == pause_id))
        updated_pause = pause_result.scalar_one()
        assert updated_pause.finished_at is None


class TestCleanupExpiredTokens:
    async def test_expired_tokens_deleted(self, db_session: AsyncSession):
        """Expired refresh token + expired verification code -> both deleted."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        token_id = uuid.uuid4()
        token = RefreshToken(
            id=token_id,
            user_id=user.id,
            token=f"expired-token-{uuid.uuid4().hex}",
            expires_at=datetime.now(UTC) - timedelta(days=1),
            revoked=False,
        )
        db_session.add(token)

        code_id = uuid.uuid4()
        code = VerificationCode(
            id=code_id,
            user_id=user.id,
            code="1234",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        db_session.add(code)
        await db_session.commit()

        with patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session):
            cleanup_expired_tokens()

        db_session.expire_all()

        token_result = await db_session.execute(
            select(RefreshToken).where(RefreshToken.id == token_id)
        )
        assert token_result.scalar_one_or_none() is None

        code_result = await db_session.execute(
            select(VerificationCode).where(VerificationCode.id == code_id)
        )
        assert code_result.scalar_one_or_none() is None

    async def test_revoked_tokens_deleted(self, db_session: AsyncSession):
        """Revoked (but not expired) token -> deleted."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        token_id = uuid.uuid4()
        token = RefreshToken(
            id=token_id,
            user_id=user.id,
            token=f"revoked-token-{uuid.uuid4().hex}",
            expires_at=datetime.now(UTC) + timedelta(days=30),
            revoked=True,
        )
        db_session.add(token)
        await db_session.commit()

        with patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session):
            cleanup_expired_tokens()

        db_session.expire_all()

        result = await db_session.execute(select(RefreshToken).where(RefreshToken.id == token_id))
        assert result.scalar_one_or_none() is None

    async def test_valid_tokens_kept(self, db_session: AsyncSession):
        """Valid token (not expired, not revoked) -> kept."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        token_id = uuid.uuid4()
        token = RefreshToken(
            id=token_id,
            user_id=user.id,
            token=f"valid-token-{uuid.uuid4().hex}",
            expires_at=datetime.now(UTC) + timedelta(days=30),
            revoked=False,
        )
        db_session.add(token)
        await db_session.commit()

        with patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session):
            cleanup_expired_tokens()

        db_session.expire_all()

        result = await db_session.execute(select(RefreshToken).where(RefreshToken.id == token_id))
        assert result.scalar_one_or_none() is not None


def _make_file(
    owner_id: uuid.UUID,
    *,
    is_attached: bool,
    age_hours: int,
) -> File:
    return File(
        id=uuid.uuid4(),
        storage_key=f"other/{uuid.uuid4().hex}.bin",
        bucket="smenka-files",
        category=FileCategory.other,
        original_filename="x.bin",
        content_type="application/octet-stream",
        size_bytes=10,
        is_attached=is_attached,
        owner_user_id=owner_id,
        created_at=datetime.now(UTC) - timedelta(hours=age_hours),
    )


def _make_checklist_photo_file(
    owner_id: uuid.UUID,
    *,
    is_attached: bool = True,
    age_days: int = 40,
    purged_at: datetime | None = None,
    organization_id: uuid.UUID | None = None,
) -> File:
    """checklist_photo_retention: файл категории checklist_photo с заданным
    возрастом (по умолчанию старше дефолтного CHECKLIST_PHOTO_RETENTION_DAYS=30)."""
    return File(
        id=uuid.uuid4(),
        storage_key=f"checklist-photos/{uuid.uuid4().hex}.jpg",
        bucket="smenka-files",
        category=FileCategory.checklist_photo,
        original_filename="proof.jpg",
        content_type="image/jpeg",
        size_bytes=10,
        is_attached=is_attached,
        owner_user_id=owner_id,
        organization_id=organization_id,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
        purged_at=purged_at,
    )


def _make_shift_geo_photo_file(
    owner_id: uuid.UUID,
    *,
    is_attached: bool = True,
    age_days: int = 91,
    purged_at: datetime | None = None,
    organization_id: uuid.UUID | None = None,
) -> File:
    """storage_housekeeping: файл категории shift_geo_photo с заданным возрастом
    (по умолчанию старше дефолтного SHIFT_GEO_PHOTO_RETENTION_DAYS=90)."""
    return File(
        id=uuid.uuid4(),
        storage_key=f"shift-geo-photos/{uuid.uuid4().hex}.jpg",
        bucket="smenka-files",
        category=FileCategory.shift_geo_photo,
        original_filename="geo.jpg",
        content_type="image/jpeg",
        size_bytes=10,
        is_attached=is_attached,
        owner_user_id=owner_id,
        organization_id=organization_id,
        created_at=datetime.now(UTC) - timedelta(days=age_days),
        purged_at=purged_at,
    )


class TestCleanupOrphanFiles:
    async def test_old_unattached_deleted_others_kept(self, db_session: AsyncSession):
        """Сирота (unattached, >24h) удаляется; свежий и привязанный — остаются."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        orphan = _make_file(user.id, is_attached=False, age_hours=25)
        fresh = _make_file(user.id, is_attached=False, age_hours=1)
        attached_old = _make_file(user.id, is_attached=True, age_hours=25)
        db_session.add_all([orphan, fresh, attached_old])
        await db_session.commit()

        # Фиксируем значения до expire_all — иначе доступ к ORM-атрибутам триггерит
        # ленивую async-загрузку в синхронном контексте.
        orphan_id, orphan_key = orphan.id, orphan.storage_key
        fresh_id, attached_id = fresh.id, attached_old.id

        deleted_keys: list[str] = []

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            deleted_keys.extend(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            cleanup_orphan_files()

        db_session.expire_all()
        assert deleted_keys == [orphan_key]

        remaining = (await db_session.execute(select(File.id))).scalars().all()
        assert orphan_id not in remaining
        assert fresh_id in remaining
        assert attached_id in remaining

    async def test_storage_error_keeps_row_for_retry(self, db_session: AsyncSession):
        """исправление cleanup_orphan_files: StorageError на удалении объекта ->
        строка files НЕ удаляется; следующий (успешный) запуск подчищает её."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        orphan = _make_file(user.id, is_attached=False, age_hours=25)
        db_session.add(orphan)
        await db_session.commit()
        orphan_id = orphan.id

        def failing_report(keys: list[str]) -> tuple[list[str], list[str]]:
            return [], list(keys)

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", failing_report),
        ):
            cleanup_orphan_files()

        db_session.expire_all()
        remaining = (await db_session.execute(select(File.id))).scalars().all()
        assert orphan_id in remaining  # строка осталась — объект не потерян молча

        def succeeding_report(keys: list[str]) -> tuple[list[str], list[str]]:
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", succeeding_report),
        ):
            cleanup_orphan_files()

        db_session.expire_all()
        remaining_after_retry = (await db_session.execute(select(File.id))).scalars().all()
        assert orphan_id not in remaining_after_retry

    async def test_full_batch_partial_failure_stops_loop(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ):
        """Ревью-фикс: полный батч с частичным провалом останавливает цикл после
        коммита — _delete_objects_report вызывается ровно один раз, а не
        долбит те же ключи повторно до _ORPHAN_MAX_BATCHES. Успешные в этом
        батче удаляются, провалившийся остаётся кандидатом, до следующего
        батча цикл не доходит вовсе."""
        monkeypatch.setattr(cleanup_tasks, "_ORPHAN_BATCH_SIZE", 2)

        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        # order_by(created_at): failing раньше succeeding раньше untouched.
        failing = _make_file(user.id, is_attached=False, age_hours=32)
        succeeding = _make_file(user.id, is_attached=False, age_hours=31)
        untouched = _make_file(user.id, is_attached=False, age_hours=26)
        db_session.add_all([failing, succeeding, untouched])
        await db_session.commit()
        failing_id, failing_key = failing.id, failing.storage_key
        succeeding_id = succeeding.id
        untouched_id = untouched.id

        call_count = 0

        def partial_failure_report(keys: list[str]) -> tuple[list[str], list[str]]:
            nonlocal call_count
            call_count += 1
            succeeded_keys = [k for k in keys if k != failing_key]
            failed_keys = [k for k in keys if k == failing_key]
            return succeeded_keys, failed_keys

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", partial_failure_report),
        ):
            cleanup_orphan_files()

        assert call_count == 1  # цикл не повторил батч со StorageError

        db_session.expire_all()
        remaining = (await db_session.execute(select(File.id))).scalars().all()
        assert failing_id in remaining  # провалившийся — остался кандидатом
        assert succeeding_id not in remaining  # успешный в том же батче — удалён
        assert untouched_id in remaining  # до второго батча цикл не дошёл


class TestPurgeExpiredFilesChecklistPhotoRule:
    """storage_housekeeping: правило `checklist_photo` (унаследовано от
    checklist_photo_retention без изменения поведения) — удаление ОБЪЕКТА S3
    (не строки) привязанных фото чек-листов старше CHECKLIST_PHOTO_RETENTION_DAYS."""

    async def test_expired_attached_photo_purged(self, db_session: AsyncSession):
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        photo = _make_checklist_photo_file(user.id, is_attached=True, age_days=31)
        db_session.add(photo)
        await db_session.commit()
        photo_id, photo_key = photo.id, photo.storage_key

        deleted_keys: list[str] = []

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            deleted_keys.extend(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        assert deleted_keys == [photo_key]

        db_session.expire_all()
        result = await db_session.execute(select(File).where(File.id == photo_id))
        row = result.scalar_one()
        assert row.purged_at is not None

    async def test_fresh_unattached_other_category_and_already_purged_untouched(
        self, db_session: AsyncSession
    ):
        """Не трогаются: моложе N дней; непривязанный; другая категория; уже удалённый."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        fresh = _make_checklist_photo_file(user.id, is_attached=True, age_days=5)
        unattached = _make_checklist_photo_file(user.id, is_attached=False, age_days=40)
        other_category = _make_file(user.id, is_attached=True, age_hours=40 * 24)
        already_purged = _make_checklist_photo_file(
            user.id, is_attached=True, age_days=40, purged_at=datetime.now(UTC)
        )
        db_session.add_all([fresh, unattached, other_category, already_purged])
        await db_session.commit()
        ids = {
            "fresh": fresh.id,
            "unattached": unattached.id,
            "other_category": other_category.id,
            "already_purged": already_purged.id,
        }
        already_purged_at = already_purged.purged_at

        deleted_keys: list[str] = []

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            deleted_keys.extend(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        assert deleted_keys == []

        db_session.expire_all()
        for label, file_id in ids.items():
            row = (await db_session.execute(select(File).where(File.id == file_id))).scalar_one()
            if label == "already_purged":
                assert row.purged_at == already_purged_at
            else:
                assert row.purged_at is None

    async def test_storage_error_keeps_purged_at_null_retry_succeeds(
        self, db_session: AsyncSession
    ):
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        photo = _make_checklist_photo_file(user.id, is_attached=True, age_days=31)
        db_session.add(photo)
        await db_session.commit()
        photo_id = photo.id

        def failing_report(keys: list[str]) -> tuple[list[str], list[str]]:
            return [], list(keys)

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", failing_report),
        ):
            purge_expired_files()

        db_session.expire_all()
        row = (await db_session.execute(select(File).where(File.id == photo_id))).scalar_one()
        assert row.purged_at is None

        def succeeding_report(keys: list[str]) -> tuple[list[str], list[str]]:
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", succeeding_report),
        ):
            purge_expired_files()

        db_session.expire_all()
        row_after_retry = (
            await db_session.execute(select(File).where(File.id == photo_id))
        ).scalar_one()
        assert row_after_retry.purged_at is not None

    async def test_full_batch_partial_failure_stops_loop(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ревью-фикс (унаследован от checklist_photo_retention): полный батч с
        частичным провалом останавливает цикл после коммита —
        _delete_objects_report вызывается ровно один раз, а не долбит те же
        ключи повторно до _PURGE_MAX_BATCHES. Успешный в этом батче помечается
        purged_at, провалившийся остаётся кандидатом, до следующего батча цикл
        не доходит вовсе."""
        monkeypatch.setattr(cleanup_tasks, "_PURGE_BATCH_SIZE", 2)

        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        # order_by(created_at): failing раньше succeeding раньше untouched.
        failing = _make_checklist_photo_file(user.id, is_attached=True, age_days=33)
        succeeding = _make_checklist_photo_file(user.id, is_attached=True, age_days=32)
        untouched = _make_checklist_photo_file(user.id, is_attached=True, age_days=31)
        db_session.add_all([failing, succeeding, untouched])
        await db_session.commit()
        failing_id, failing_key = failing.id, failing.storage_key
        succeeding_id = succeeding.id
        untouched_id = untouched.id

        call_count = 0

        def partial_failure_report(keys: list[str]) -> tuple[list[str], list[str]]:
            nonlocal call_count
            call_count += 1
            succeeded_keys = [k for k in keys if k != failing_key]
            failed_keys = [k for k in keys if k == failing_key]
            return succeeded_keys, failed_keys

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", partial_failure_report),
        ):
            purge_expired_files()

        assert call_count == 1  # цикл не повторил батч со StorageError

        db_session.expire_all()
        rows = {
            row.id: row
            for row in (
                await db_session.execute(
                    select(File).where(File.id.in_([failing_id, succeeding_id, untouched_id]))
                )
            )
            .scalars()
            .all()
        }
        assert rows[failing_id].purged_at is None  # провалившийся — остался кандидатом
        assert rows[succeeding_id].purged_at is not None  # успешный в том же батче — помечен
        assert rows[untouched_id].purged_at is None  # до второго батча цикл не дошёл

    async def test_idempotent_second_run_no_op(self, db_session: AsyncSession) -> None:
        """Повторный запуск после успешной очистки не трогает уже удалённые фото."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        photo = _make_checklist_photo_file(user.id, is_attached=True, age_days=31)
        db_session.add(photo)
        await db_session.commit()
        photo_id = photo.id

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        db_session.expire_all()
        first_purged_at = (
            await db_session.execute(select(File.purged_at).where(File.id == photo_id))
        ).scalar_one()
        assert first_purged_at is not None

        deleted_keys_second_run: list[str] = []

        def fake_report_second(keys: list[str]) -> tuple[list[str], list[str]]:
            deleted_keys_second_run.extend(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report_second),
        ):
            purge_expired_files()

        assert deleted_keys_second_run == []  # уже purged — не кандидат повторно


class TestPurgeExpiredFilesShiftGeoPhotoRule:
    """storage_housekeeping: правило `shift_geo_photo` — фото старта смены без
    геопроверки (лицо сотрудника), старше SHIFT_GEO_PHOTO_RETENTION_DAYS."""

    async def test_expired_attached_photo_purged(self, db_session: AsyncSession):
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        photo = _make_shift_geo_photo_file(user.id, is_attached=True, age_days=91)
        db_session.add(photo)
        await db_session.commit()
        photo_id, photo_key = photo.id, photo.storage_key

        deleted_keys: list[str] = []

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            deleted_keys.extend(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        assert deleted_keys == [photo_key]

        db_session.expire_all()
        row = (await db_session.execute(select(File).where(File.id == photo_id))).scalar_one()
        assert row.purged_at is not None

    async def test_fresh_unattached_other_category_and_already_purged_untouched(
        self, db_session: AsyncSession
    ) -> None:
        """Не трогаются: моложе N дней; непривязанный; другая категория; уже удалённый."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        fresh = _make_shift_geo_photo_file(user.id, is_attached=True, age_days=5)
        unattached = _make_shift_geo_photo_file(user.id, is_attached=False, age_days=91)
        other_category = _make_checklist_photo_file(user.id, is_attached=True, age_days=91)
        already_purged = _make_shift_geo_photo_file(
            user.id, is_attached=True, age_days=91, purged_at=datetime.now(UTC)
        )
        db_session.add_all([fresh, unattached, other_category, already_purged])
        await db_session.commit()
        ids = {
            "fresh": fresh.id,
            "unattached": unattached.id,
            "other_category": other_category.id,
        }
        already_purged_id, already_purged_at = already_purged.id, already_purged.purged_at

        deleted_keys: list[str] = []

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            deleted_keys.extend(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        # other_category (checklist_photo, age_days=91) старше CHECKLIST_PHOTO_RETENTION_DAYS=30,
        # поэтому его заберёт СВОЁ правило — deleted_keys содержит его ключ, но не ключи
        # shift_geo_photo кандидатов (fresh/unattached/already_purged).
        assert photo_key_prefixes(deleted_keys) == {"checklist-photos/"}

        db_session.expire_all()
        for label, file_id in ids.items():
            row = (await db_session.execute(select(File).where(File.id == file_id))).scalar_one()
            assert row.purged_at is None, label

        already_purged_row = (
            await db_session.execute(select(File).where(File.id == already_purged_id))
        ).scalar_one()
        assert already_purged_row.purged_at == already_purged_at

    async def test_storage_error_keeps_purged_at_null_retry_succeeds(
        self, db_session: AsyncSession
    ) -> None:
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        photo = _make_shift_geo_photo_file(user.id, is_attached=True, age_days=91)
        db_session.add(photo)
        await db_session.commit()
        photo_id = photo.id

        def failing_report(keys: list[str]) -> tuple[list[str], list[str]]:
            return [], list(keys)

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", failing_report),
        ):
            purge_expired_files()

        db_session.expire_all()
        row = (await db_session.execute(select(File).where(File.id == photo_id))).scalar_one()
        assert row.purged_at is None

        def succeeding_report(keys: list[str]) -> tuple[list[str], list[str]]:
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", succeeding_report),
        ):
            purge_expired_files()

        db_session.expire_all()
        row_after_retry = (
            await db_session.execute(select(File).where(File.id == photo_id))
        ).scalar_one()
        assert row_after_retry.purged_at is not None


def photo_key_prefixes(keys: list[str]) -> set[str]:
    """Тестовый хелпер: множество префиксов категории по storage_key (первый
    сегмент до `/`), удобно сравнивать «какие категории затронуты»."""
    return {key.split("/", 1)[0] + "/" for key in keys}


class TestPurgeExpiredFilesDeletedOrganizationRule:
    """storage_housekeeping: правило `deleted_organization` — все файлы (любая
    категория) организации, удалённой больше DELETED_ORG_FILE_RETENTION_DAYS
    назад (`organizations.deleted_at`)."""

    async def test_files_of_old_deleted_org_purged_any_category(
        self, db_session: AsyncSession
    ) -> None:
        """Файлы свежие по возрасту (created_at) своей категории всё равно
        удаляются — правило зависит от organizations.deleted_at, не от
        files.created_at."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        old_deleted_org = _make_org(
            owner_id=user.id,
            is_deleted=True,
            deleted_at=datetime.now(UTC) - timedelta(days=40),
        )
        db_session.add(old_deleted_org)
        await db_session.flush()

        checklist_photo = _make_checklist_photo_file(
            user.id, is_attached=True, age_days=1, organization_id=old_deleted_org.id
        )
        geo_photo = _make_shift_geo_photo_file(
            user.id, is_attached=True, age_days=1, organization_id=old_deleted_org.id
        )
        db_session.add_all([checklist_photo, geo_photo])
        await db_session.commit()
        checklist_id, checklist_key = checklist_photo.id, checklist_photo.storage_key
        geo_id, geo_key = geo_photo.id, geo_photo.storage_key

        deleted_keys: list[str] = []

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            deleted_keys.extend(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        assert set(deleted_keys) == {checklist_key, geo_key}

        db_session.expire_all()
        rows = {
            row.id: row
            for row in (
                await db_session.execute(select(File).where(File.id.in_([checklist_id, geo_id])))
            )
            .scalars()
            .all()
        }
        assert rows[checklist_id].purged_at is not None
        assert rows[geo_id].purged_at is not None

    async def test_neighbors_untouched(self, db_session: AsyncSession) -> None:
        """Не трогаются: живая организация; организация удалена недавно (в
        пределах срока); непривязанный файл удалённой организации. Категория
        knowledge_base намеренно — у неё нет своего срока хранения, поэтому
        `purged_at` может проставить ТОЛЬКО правило deleted_organization,
        никакое другое правило её не заберёт по ошибке."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        live_org = _make_org(owner_id=user.id)
        recently_deleted_org = _make_org(
            owner_id=user.id,
            is_deleted=True,
            deleted_at=datetime.now(UTC) - timedelta(days=5),
        )
        old_deleted_org = _make_org(
            owner_id=user.id,
            is_deleted=True,
            deleted_at=datetime.now(UTC) - timedelta(days=40),
        )
        db_session.add_all([live_org, recently_deleted_org, old_deleted_org])
        await db_session.flush()

        def _kb_file(org_id: uuid.UUID, *, is_attached: bool) -> File:
            return File(
                id=uuid.uuid4(),
                storage_key=f"knowledge-base/{uuid.uuid4().hex}.pdf",
                bucket="smenka-files",
                category=FileCategory.knowledge_base,
                original_filename="doc.pdf",
                content_type="application/pdf",
                size_bytes=10,
                is_attached=is_attached,
                owner_user_id=user.id,
                organization_id=org_id,
                created_at=datetime.now(UTC) - timedelta(days=1),
            )

        live_org_file = _kb_file(live_org.id, is_attached=True)
        recently_deleted_file = _kb_file(recently_deleted_org.id, is_attached=True)
        unattached_old_deleted_file = _kb_file(old_deleted_org.id, is_attached=False)
        db_session.add_all([live_org_file, recently_deleted_file, unattached_old_deleted_file])
        await db_session.commit()
        ids = {
            "live_org": live_org_file.id,
            "recently_deleted": recently_deleted_file.id,
            "unattached_old_deleted": unattached_old_deleted_file.id,
        }

        called = False

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            nonlocal called
            called = True
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        assert called is False  # ни один из трёх соседей не стал кандидатом

        db_session.expire_all()
        for label, file_id in ids.items():
            row = (await db_session.execute(select(File).where(File.id == file_id))).scalar_one()
            assert row.purged_at is None, label

    async def test_storage_error_in_org_rule_does_not_block_other_rules(
        self, db_session: AsyncSession
    ) -> None:
        """StorageError в правиле deleted_organization останавливает ТОЛЬКО
        его — правило shift_geo_photo (идущее следом) всё равно отрабатывает
        в этом же запуске задачи."""
        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        old_deleted_org = _make_org(
            owner_id=user.id,
            is_deleted=True,
            deleted_at=datetime.now(UTC) - timedelta(days=40),
        )
        db_session.add(old_deleted_org)
        await db_session.flush()

        # knowledge_base — категория без своего срока хранения, попадёт
        # ТОЛЬКО под deleted_organization (не под checklist_photo/shift_geo_photo).
        org_file = File(
            id=uuid.uuid4(),
            storage_key=f"knowledge-base/{uuid.uuid4().hex}.pdf",
            bucket="smenka-files",
            category=FileCategory.knowledge_base,
            original_filename="doc.pdf",
            content_type="application/pdf",
            size_bytes=10,
            is_attached=True,
            owner_user_id=user.id,
            organization_id=old_deleted_org.id,
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        geo_photo = _make_shift_geo_photo_file(user.id, is_attached=True, age_days=91)
        db_session.add_all([org_file, geo_photo])
        await db_session.commit()
        org_file_id, org_file_key = org_file.id, org_file.storage_key
        geo_id = geo_photo.id

        def selective_failure_report(keys: list[str]) -> tuple[list[str], list[str]]:
            if org_file_key in keys:
                return [], list(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", selective_failure_report),
        ):
            purge_expired_files()

        db_session.expire_all()
        org_row = (
            await db_session.execute(select(File).where(File.id == org_file_id))
        ).scalar_one()
        geo_row = (await db_session.execute(select(File).where(File.id == geo_id))).scalar_one()
        assert org_row.purged_at is None  # deleted_organization — провалилось
        assert geo_row.purged_at is not None  # shift_geo_photo — отработало как обычно


class TestPurgeExpiredFilesZeroRetentionDisablesRule:
    """storage_housekeeping: `retention_days<=0` на любом из трёх правил
    выключает именно его, не трогая остальные."""

    async def test_checklist_photo_zero_retention_skips_rule(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cleanup_tasks.settings, "checklist_photo_retention_days", 0)

        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        photo = _make_checklist_photo_file(user.id, is_attached=True, age_days=365)
        db_session.add(photo)
        await db_session.commit()
        photo_id = photo.id

        called = False

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            nonlocal called
            called = True
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        assert called is False

        db_session.expire_all()
        row = (await db_session.execute(select(File).where(File.id == photo_id))).scalar_one()
        assert row.purged_at is None

    async def test_shift_geo_photo_zero_retention_skips_rule(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cleanup_tasks.settings, "shift_geo_photo_retention_days", 0)

        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        photo = _make_shift_geo_photo_file(user.id, is_attached=True, age_days=365)
        db_session.add(photo)
        await db_session.commit()
        photo_id = photo.id

        called = False

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            nonlocal called
            called = True
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        assert called is False

        db_session.expire_all()
        row = (await db_session.execute(select(File).where(File.id == photo_id))).scalar_one()
        assert row.purged_at is None

    async def test_deleted_org_zero_retention_skips_rule(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cleanup_tasks.settings, "deleted_org_file_retention_days", 0)

        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        old_deleted_org = _make_org(
            owner_id=user.id,
            is_deleted=True,
            deleted_at=datetime.now(UTC) - timedelta(days=365),
        )
        db_session.add(old_deleted_org)
        await db_session.flush()

        # knowledge_base — не попадает ни под checklist_photo, ни под
        # shift_geo_photo, только под (выключенное здесь) deleted_organization.
        org_file = File(
            id=uuid.uuid4(),
            storage_key=f"knowledge-base/{uuid.uuid4().hex}.pdf",
            bucket="smenka-files",
            category=FileCategory.knowledge_base,
            original_filename="doc.pdf",
            content_type="application/pdf",
            size_bytes=10,
            is_attached=True,
            owner_user_id=user.id,
            organization_id=old_deleted_org.id,
            created_at=datetime.now(UTC) - timedelta(days=1),
        )
        db_session.add(org_file)
        await db_session.commit()
        org_file_id = org_file.id

        called = False

        def fake_report(keys: list[str]) -> tuple[list[str], list[str]]:
            nonlocal called
            called = True
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_report),
        ):
            purge_expired_files()

        assert called is False

        db_session.expire_all()
        row = (await db_session.execute(select(File).where(File.id == org_file_id))).scalar_one()
        assert row.purged_at is None


def test_purge_expired_files_registered_old_task_removed() -> None:
    """storage_housekeeping: purge_expired_files в beat вместо
    purge_expired_checklist_photos; старой задачи нет ни в коде, ни в beat."""
    from src.app.core.celery_app import celery_app

    entry = celery_app.conf.beat_schedule.get("purge-expired-files")
    assert entry is not None
    assert entry["task"] == "purge_expired_files"

    assert celery_app.conf.beat_schedule.get("purge-expired-checklist-photos") is None
    assert not hasattr(cleanup_tasks, "purge_expired_checklist_photos")


def test_reconcile_storage_objects_registered_in_beat_schedule() -> None:
    """storage_housekeeping: reconcile_storage_objects зарегистрирована в beat."""
    from src.app.core.celery_app import celery_app

    entry = celery_app.conf.beat_schedule.get("reconcile-storage-objects")
    assert entry is not None
    assert entry["task"] == "reconcile_storage_objects"


class TestReconcileStorageObjects:
    """storage_housekeeping: сверка «объекты S3 без строки в files» —
    слой хранилища (`_list_objects_page`/`_delete_objects_report`) мокается,
    реальный S3 не трогается."""

    async def test_disabled_skips_s3_entirely(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cleanup_tasks.settings, "storage_reconcile_enabled", False)

        called = False

        def fake_list(
            prefix: str, continuation_token: str | None
        ) -> tuple[list[ObjectSummary], str | None]:
            nonlocal called
            called = True
            return [], None

        with patch("src.app.tasks.cleanup._list_objects_page", fake_list):
            reconcile_storage_objects()

        assert called is False

    async def test_orphan_and_purged_survivor_deleted_live_and_young_kept(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Удаляются: объект без строки files; объект со строкой, но
        purged_at уже проставлен (пережил удаление) — оба старше порога.
        НЕ трогаются: живая строка (purged_at=NULL), даже старше порога;
        молодой (моложе ORPHAN_FILE_TTL_HOURS) объект без строки; всё, что
        лежит вне префиксов CATEGORY_POLICIES (включая db-backups/ — сюда
        вообще не заглядываем)."""
        monkeypatch.setattr(cleanup_tasks.settings, "storage_reconcile_enabled", True)
        monkeypatch.setattr(cleanup_tasks.settings, "orphan_file_ttl_hours", 24)

        user = _make_user()
        db_session.add(user)
        await db_session.flush()

        live_file = _make_checklist_photo_file(user.id, is_attached=True, age_days=1)
        survivor_file = _make_checklist_photo_file(
            user.id, is_attached=True, age_days=40, purged_at=datetime.now(UTC)
        )
        db_session.add_all([live_file, survivor_file])
        await db_session.commit()

        old = datetime.now(UTC) - timedelta(hours=48)
        young = datetime.now(UTC) - timedelta(hours=1)
        orphan_key = "checklist-photos/no-row/2026/09/orphan.jpg"
        young_orphan_key = "checklist-photos/no-row/2026/09/young.jpg"

        checklist_prefix = CATEGORY_POLICIES[FileCategory.checklist_photo].prefix
        objects_by_prefix = {
            checklist_prefix: [
                ObjectSummary(key=live_file.storage_key, size=10, last_modified=old),
                ObjectSummary(key=survivor_file.storage_key, size=20, last_modified=old),
                ObjectSummary(key=orphan_key, size=30, last_modified=old),
                ObjectSummary(key=young_orphan_key, size=40, last_modified=young),
            ]
        }

        prefixes_called: list[str] = []

        def fake_list(
            prefix: str, continuation_token: str | None
        ) -> tuple[list[ObjectSummary], str | None]:
            prefixes_called.append(prefix)
            return objects_by_prefix.get(prefix, []), None

        deleted_keys: list[str] = []

        def fake_delete_report(keys: list[str]) -> tuple[list[str], list[str]]:
            deleted_keys.extend(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._list_objects_page", fake_list),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_delete_report),
        ):
            reconcile_storage_objects()

        assert set(deleted_keys) == {survivor_file.storage_key, orphan_key}

        expected_prefixes = {policy.prefix for policy in CATEGORY_POLICIES.values()}
        assert set(prefixes_called) == expected_prefixes
        assert "db-backups/" not in prefixes_called

        db_session.expire_all()
        live_row = (
            await db_session.execute(select(File).where(File.id == live_file.id))
        ).scalar_one()
        assert live_row.purged_at is None  # живая строка не тронута сверкой

    async def test_paginated_listing_processes_all_pages(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cleanup_tasks.settings, "storage_reconcile_enabled", True)

        old = datetime.now(UTC) - timedelta(hours=48)
        checklist_prefix = CATEGORY_POLICIES[FileCategory.checklist_photo].prefix
        page1_key = "checklist-photos/no-row/2026/09/page1.jpg"
        page2_key = "checklist-photos/no-row/2026/09/page2.jpg"

        calls: list[tuple[str, str | None]] = []

        def fake_list(
            prefix: str, continuation_token: str | None
        ) -> tuple[list[ObjectSummary], str | None]:
            calls.append((prefix, continuation_token))
            if prefix != checklist_prefix:
                return [], None
            if continuation_token is None:
                return [ObjectSummary(key=page1_key, size=1, last_modified=old)], "token-1"
            if continuation_token == "token-1":
                return [ObjectSummary(key=page2_key, size=2, last_modified=old)], None
            raise AssertionError("неожиданный третий вызов страницы")

        deleted_keys: list[str] = []

        def fake_delete_report(keys: list[str]) -> tuple[list[str], list[str]]:
            deleted_keys.extend(keys)
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._list_objects_page", fake_list),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_delete_report),
        ):
            reconcile_storage_objects()

        checklist_calls = [c for c in calls if c[0] == checklist_prefix]
        assert checklist_calls == [(checklist_prefix, None), (checklist_prefix, "token-1")]
        assert set(deleted_keys) == {page1_key, page2_key}

    async def test_candidates_over_limit_deletes_nothing(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Предохранитель: кандидатов больше STORAGE_RECONCILE_MAX_DELETES —
        не удаляется НИЧЕГО за весь запуск."""
        monkeypatch.setattr(cleanup_tasks.settings, "storage_reconcile_enabled", True)
        monkeypatch.setattr(cleanup_tasks.settings, "storage_reconcile_max_deletes", 1)

        old = datetime.now(UTC) - timedelta(hours=48)
        checklist_prefix = CATEGORY_POLICIES[FileCategory.checklist_photo].prefix
        objects = [
            ObjectSummary(key="checklist-photos/no-row/2026/09/o1.jpg", size=1, last_modified=old),
            ObjectSummary(key="checklist-photos/no-row/2026/09/o2.jpg", size=1, last_modified=old),
        ]

        def fake_list(
            prefix: str, continuation_token: str | None
        ) -> tuple[list[ObjectSummary], str | None]:
            return (objects, None) if prefix == checklist_prefix else ([], None)

        delete_called = False

        def fake_delete_report(keys: list[str]) -> tuple[list[str], list[str]]:
            nonlocal delete_called
            delete_called = True
            return list(keys), []

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch("src.app.tasks.cleanup._list_objects_page", fake_list),
            patch("src.app.tasks.cleanup._delete_objects_report", fake_delete_report),
        ):
            reconcile_storage_objects()

        assert delete_called is False
