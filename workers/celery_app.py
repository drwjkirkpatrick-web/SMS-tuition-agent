"""
workers/celery_app.py — Celery application configuration (Step 9 update)
═══════════════════════════════════════════════════

Now with the actual task paths registered for Beat scheduling.
═══════════════════════════════════════════════════
"""

import os
from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "sms_tuition_agent",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "workers.reminders",
        "workers.sends",
        "workers.reconciliation",
        "workers.inbound",
    ],
)

celery_app.conf.update(
    result_expires=3600,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_routes={
        "workers.reminders.*": {"queue": "reminders"},
        "workers.sends.*": {"queue": "sends"},
        "workers.reconciliation.*": {"queue": "reconciliation"},
        "workers.inbound.*": {"queue": "inbound"},
    },
    task_default_retry_delay=60,
    task_max_retries=3,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

# Beat schedule: run daily at 8:00 AM UTC (adjust for timezone in task)
celery_app.conf.beat_schedule = {
    "compute-reminders-daily": {
        "task": "workers.reminders.compute_reminder_candidates",
        "schedule": crontab(hour=8, minute=0),  # 8:00 AM UTC
        "kwargs": {"school_id": 1},
    },
    "reconcile-unknown": {
        "task": "workers.reconciliation.reconcile_unknown_deliveries",
        "schedule": 600.0,  # every 10 minutes
    },
    "poll-payments": {
        "task": "workers.reconciliation.poll_payment_updates",
        "schedule": 300.0,  # every 5 minutes
    },
}

celery_app.autodiscover_tasks()
