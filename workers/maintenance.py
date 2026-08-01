"""
workers/maintenance.py — Scheduled maintenance tasks
═══════════════════════════════════════════════════

Celery tasks for:
  R6: Data retention purge (daily at 3 AM)
  R9: Failure threshold alerting (every 15 min)
  R10: Automated database backup (daily at 2 AM)
═══════════════════════════════════════════════════
"""

from workers.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=300)
def run_retention_purge(self, school_id: int = 0) -> dict:
    """R6: Run data retention purge for all record types."""
    import asyncio
    return asyncio.run(_async_run_retention_purge(school_id))


async def _async_run_retention_purge(school_id: int = 0) -> dict:
    from domain.retention import RetentionService
    from domain.models import AuditEventType
    from infra.audit_logger import AuditContext, log_audit_event

    svc = RetentionService()
    sid = school_id if school_id > 0 else None
    result = await svc.run_retention_purge(sid)

    await log_audit_event(
        event_type=AuditEventType.SIS_SYNC,  # closest existing audit type
        entity_type="system",
        entity_id="retention_purge",
        summary=f"Retention purge: {result}",
        context=AuditContext(school_id=sid, actor_type="system"),
    )
    return result


@celery_app.task(bind=True, max_retries=3, default_retry_delay=120)
def run_alert_check(self, school_id: int = 1) -> dict:
    """R9: Check failure rate and alert if threshold exceeded."""
    import asyncio
    return asyncio.run(_async_run_alert_check(school_id))


async def _async_run_alert_check(school_id: int) -> dict:
    from domain.alerting import AlertService

    svc = AlertService()
    result = await svc.run_alert_check(school_id)
    return result


@celery_app.task(bind=True, max_retries=1, default_retry_delay=600)
def run_backup(self) -> dict:
    """R10: Run automated database backup."""
    import asyncio
    return asyncio.run(_async_run_backup())


async def _async_run_backup() -> dict:
    from infra.backup import run_backup_job

    result = await run_backup_job()
    return result