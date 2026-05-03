"""
workers/celery_app.py — Celery application configuration
═══════════════════════════════════════════════════

Celery is a distributed task queue. Think of it as a "to-do list"
that multiple workers can pull from simultaneously.

Key concepts:
  - Broker (Redis): where tasks wait to be picked up
  - Worker: a process that executes tasks
  - Beat: a scheduler that adds tasks to the broker at set times
  - Queue: a named lane for tasks (reminders, sends, inbound, etc.)
    We use separate queues so a backed-up send queue doesn't delay
    payment reconciliation.
═══════════════════════════════════════════════════
"""

import os
from celery import Celery

# Read Redis URL from environment (or default for local dev)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Create the Celery app
# The first argument is the "main module name" — used to find tasks.
celery_app = Celery(
    "sms_tuition_agent",
    broker=REDIS_URL,
    backend=REDIS_URL,
    # Tell Celery where to look for task definitions
    include=[
        "workers.reminders",      # Step 8–9
        "workers.sends",          # Step 11–12
        "workers.reconciliation", # Step 14, 17
        "workers.inbound",        # Step 16
    ],
)

# ── Configuration ──
# These settings control worker behavior, serialization, and routing.
celery_app.conf.update(
    # Task results expire after 1 hour (keep Redis small)
    result_expires=3600,
    
    # Serialize tasks as JSON (human-readable, widely supported)
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    
    # Timezone awareness — critical for "quiet hours" and due dates
    timezone="UTC",  # overridden by SCHOOL_TIMEZONE in task logic
    enable_utc=True,
    
    # Route tasks to named queues (defined in docker-compose)
    task_routes={
        "workers.reminders.*": {"queue": "reminders"},
        "workers.sends.*": {"queue": "sends"},
        "workers.reconciliation.*": {"queue": "reconciliation"},
        "workers.inbound.*": {"queue": "inbound"},
    },
    
    # Retry failed tasks with exponential backoff
    task_default_retry_delay=60,   # 1 minute
    task_max_retries=3,
    
    # Acknowledge tasks only after they complete (not at start).
    # Prevents lost tasks if a worker crashes mid-execution.
    task_acks_late=True,
    
    # Prefetch only 1 task at a time per worker.
    # Prevents a slow task from blocking the worker's queue slot.
    worker_prefetch_multiplier=1,
)


# ── Beat Schedule (periodic tasks) ──
# These entries tell Celery Beat what tasks to run and when.
# We'll define the actual task functions in later steps.
celery_app.conf.beat_schedule = {
    # Step 9: compute reminder candidates daily at 8:00 AM
    "compute-reminders-daily": {
        "task": "workers.reminders.compute_reminder_candidates",
        "schedule": 86400.0,  # seconds = 24 hours; or crontab(hour=8, minute=0)
    },
    # Step 14: reconcile unknown deliveries every 10 minutes
    "reconcile-unknown": {
        "task": "workers.reconciliation.reconcile_unknown_deliveries",
        "schedule": 600.0,  # 10 minutes
    },
    # Step 17: poll for payment updates every 5 minutes
    "poll-payments": {
        "task": "workers.reconciliation.poll_payment_updates",
        "schedule": 300.0,  # 5 minutes
    },
}


# ── Autodiscover tasks ──
# This imports all modules listed in `include=` so tasks are registered.
celery_app.autodiscover_tasks()
