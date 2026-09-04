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
from src.app.tasks.cleanup import cleanup_expired_tokens, cleanup_orphan_files
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


def _make_org(*, owner_id: uuid.UUID, org_id: uuid.UUID | None = None) -> Organization:
    return Organization(
        id=org_id or uuid.uuid4(),
        name="Test Org",
        owner_id=owner_id,
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

        with (
            patch("src.app.tasks.cleanup.get_sync_session", get_sync_test_session),
            patch(
                "src.app.tasks.cleanup._delete_orphan_objects",
                lambda keys: deleted_keys.extend(keys),
            ),
        ):
            cleanup_orphan_files()

        db_session.expire_all()
        assert deleted_keys == [orphan_key]

        remaining = (await db_session.execute(select(File.id))).scalars().all()
        assert orphan_id not in remaining
        assert fresh_id in remaining
        assert attached_id in remaining
