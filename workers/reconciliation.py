"""
workers/reconciliation.py — Reconciliation tasks
═══════════════════════════════════════════════════

Two periodic tasks:
  1. reconcile_unknown_deliveries — queries provider for timed-out sends
  2. poll_payment_updates — checks for new payments from SIS

Teaching notes:
  - Reconciliation runs every 10 minutes (configurable).
  - It only processes messages that have been UNKNOWN_DELIVERY
    for > 10 minutes (gives provider time to register the send).
  - After querying the provider, it updates our DB and logs audit events.
═══════════════════════════════════════════════════
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.twilio_adapter import get_twilio_adapter
from domain.models import MessageStatus, OutboundMessage
from domain.outbox import OutboxService
from domain.reconciliation_service import ReconciliationService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from infra.settings import get_settings
from workers.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def reconcile_unknown_deliveries(self) -> dict:
    """Query provider for unknown delivery statuses and resolve them."""
    import asyncio
    return asyncio.run(_async_reconcile_unknown())


async def _async_reconcile_unknown() -> dict:
    outbox = OutboxService()
    adapter = get_twilio_adapter()
    recon = ReconciliationService()
    result = {"resolved": 0, "not_found": 0, "failed": 0, "errors": 0}

    async with async_session_factory() as session:
        # Get messages stuck in unknown_delivery for > 10 minutes
        messages = await outbox.get_unknown_deliveries(session, older_than_minutes=10)
        if not messages:
            return result

        for message in messages:
            try:
                if not message.client_message_id:
                    continue

                # Query Twilio for this message
                query_result = await adapter.query_delivery(message.client_message_id)

                # Reconcile
                await recon.reconcile_unknown_delivery(session, message, query_result.status)

                if query_result.status == "not_found":
                    result["not_found"] += 1
                elif query_result.status in ("sent", "delivered"):
                    result["resolved"] += 1
                else:
                    result["failed"] += 1

                await log_audit_event(
                    event_type="message.delivered" if query_result.status == "delivered" else "message.failed",
                    entity_type="message",
                    entity_id=message.message_key,
                    summary=f"Reconciled unknown_delivery: {query_result.status}",
                    context=AuditContext(school_id=message.school_id, actor_type="worker"),
                )

            except Exception:
                result["errors"] += 1
                continue

        await session.commit()
    return result


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def poll_payment_updates(self, school_id: int = 1) -> dict:
    """Poll SIS for payment updates and reconcile."""
    import asyncio
    return asyncio.run(_async_poll_payments(school_id))


async def _async_poll_payments(school_id: int) -> dict:
    from adapters.connector_factory import get_connector
    from adapters.csv_connector import persist_students, persist_guardians
    from infra.database import async_session_factory
    from sqlalchemy import select
    from domain.models import School
    
    result = {"synced": 0, "errors": []}
    
    async with async_session_factory() as session:
        school_result = await session.execute(select(School).where(School.id == school_id))
        school = school_result.scalar_one_or_none()
        if not school or not school.sis_config:
            return result
        
        # This is a simplified stub — full implementation in Step 17
        result["synced"] = 0
    
    return result
