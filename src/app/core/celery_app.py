from celery import Celery  # type: ignore[import-untyped]
from celery.schedules import crontab  # type: ignore[import-untyped]
from celery.signals import task_failure  # type: ignore[import-untyped]

from src.app.core.config import get_settings
from src.app.core.logging import get_logger
from src.app.core.sentry import init_sentry

settings = get_settings()
logger = get_logger(__name__)

# Worker тоже инициализирует Sentry: при включённом DSN падения задач
# автоматически уходят в Sentry через CeleryIntegration. При пустом DSN — no-op.
init_sentry()

celery_app = Celery(
    "smenka",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    # Мониторинг (видимость во Flower/мониторинге, поднимается в проде — devops.md).
    task_track_started=True,
    task_send_sent_event=True,
    worker_send_task_events=True,
    # Транзиентная устойчивость: задача переотдаётся при падении воркера,
    # авто-завершение смен критично и не должно молча теряться.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    include=[
        "src.app.tasks.shifts",
        "src.app.tasks.cleanup",
    ],
    beat_schedule={
        "auto-finish-stale-shifts": {
            "task": "auto_finish_stale_shifts",
            # 60с (не 300) — авто-завершение по плановому концу графика (work_schedules,
            # R4): задержка закрытия смены до минуты вместо пяти; запрос лёгкий
            # (частичный индекс по scheduled_end_at, только active/paused).
            "schedule": 60.0,
        },
        "auto-finish-stale-pauses": {
            "task": "auto_finish_stale_pauses",
            "schedule": 300.0,
        },
        "cleanup-expired-tokens": {
            "task": "cleanup_expired_tokens",
            "schedule": crontab(hour=3, minute=0),
        },
        "cleanup-orphan-files": {
            "task": "cleanup_orphan_files",
            "schedule": crontab(minute=0),  # ежечасно
        },
    },
)


def _on_task_failure(
    sender: object = None,
    task_id: object = None,
    exception: BaseException | None = None,
    **kwargs: object,
) -> None:
    """Падение задачи → структурированный лог (отправку в Sentry делает
    CeleryIntegration, если DSN задан)."""
    logger.error(
        "celery_task_failed",
        task=getattr(sender, "name", None),
        task_id=task_id,
        error=repr(exception),
    )


task_failure.connect(_on_task_failure)
