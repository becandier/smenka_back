from celery import Celery
from celery.schedules import crontab

from src.app.core.config import get_settings

settings = get_settings()

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
    include=[
        "src.app.tasks.shifts",
        "src.app.tasks.cleanup",
    ],
    beat_schedule={
        "auto-finish-stale-shifts": {
            "task": "auto_finish_stale_shifts",
            "schedule": 300.0,
        },
        "auto-finish-stale-pauses": {
            "task": "auto_finish_stale_pauses",
            "schedule": 300.0,
        },
        "cleanup-expired-tokens": {
            "task": "cleanup_expired_tokens",
            "schedule": crontab(hour=3, minute=0),
        },
    },
)
