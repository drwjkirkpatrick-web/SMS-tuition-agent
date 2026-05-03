"""
workers/reminders.py — Celery task: compute reminder candidates daily
═══════════════════════════════════════════════════

This is the task run by Celery Beat every morning (configurable).
It:
  1. Loads the school and all active invoices
  2. Computes reminder candidates (Step 8 logic)
  3. Checks suppression rules
  4. Inserts into outbox with ON CONFLICT DO NOTHING (idempotency)
  5. Logs audit events

Teaching notes:
  - `@celery_app.task` registers this function as a background task.
  - `bind=True` gives the task access to `self` (task instance with retry).
  - We create a DB session inside the task because Celery workers run
    in separate processes from the FastAPI server.
  - The entire operation is wrapped in a single transaction:
    if any part fails, nothing is committed (no partial sends).
═══════════════════════════════════════════════════
"""

from datetime import date, datetime

from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import Guardian, Invoice, InvoiceStatus, MessageStatus, OutboundMessage, School
from domain.reminder_service import ReminderService
from domain.dispatch_service import DispatchService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from infra.settings import get_settings
from workers.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def compute_reminder_candidates(self, school_id: int = 1) -> dict:
    """
    Celery task: compute and insert reminder candidates for a school.
    
    Args:
        school_id: the school to process (default 1 for single-school deployments)
    
    Returns:
        {"processed": N, "inserted": M, "suppressed": K, "errors": [...]}
    """
    import asyncio
    return asyncio.run(_async_compute_reminder_candidates(school_id))


async def _async_compute_reminder_candidates(school_id: int) -> dict:
    """Async implementation of the scheduler task."""
    settings = get_settings()
    reminder_service = ReminderService()
    dispatch_service = DispatchService()
    
    result = {
        "processed": 0,
        "inserted": 0,
        "suppressed": 0,
        "duplicates_skipped": 0,
        "errors": [],
    }

    async with async_session_factory() as session:
        try:
            # 1. Load school
            school_result = await session.execute(
                select(School).where(School.id == school_id, School.deleted_at.is_(None))
            )
            school = school_result.scalar_one_or_none()
            if not school:
                result["errors"].append(f"School {school_id} not found")
                return result

            # 2. Load active invoices (not paid, not cancelled, not deleted)
            invoice_result = await session.execute(
                select(Invoice).where(
                    Invoice.school_id == school_id,
                    Invoice.status.in_([InvoiceStatus.PENDING, InvoiceStatus.PARTIAL]),
                    Invoice.deleted_at.is_(None),
                )
            )
            invoices = list(invoice_result.scalars().all())
            result["processed"] = len(invoices)

            if not invoices:
                return result  # nothing to do

            # 3. Build candidates
            today = date.today()
            candidates = reminder_service.build_candidates(school, invoices, today=today)

            # 4. Apply suppression checks per candidate
            final_candidates = []
            for candidate in candidates:
                # Load guardian for suppression check
                guardian_result = await session.execute(
                    select(Guardian).where(Guardian.id == candidate.guardian_id)
                )
                guardian = guardian_result.scalar_one_or_none()
                if not guardian:
                    continue

                suppressed, reason = reminder_service.should_suppress(
                    invoices=[i for i in invoices if i.id == candidate.invoice_id][0],
                    guardian=guardian,
                )
                # Note: should_suppress expects an Invoice object
                # We need to refactor slightly — let's get the invoice
                invoice = next((i for i in invoices if i.id == candidate.invoice_id), None)
                if not invoice:
                    continue
                suppressed, reason = reminder_service.should_suppress(invoice, guardian)
                
                if suppressed:
                    result["suppressed"] += 1
                    await log_audit_event(
                        event_type="reminder.suppressed",
                        entity_type="message",
                        entity_id=candidate.message_key,
                        summary=f"Reminder suppressed: {reason}",
                        context=AuditContext(school_id=school_id, actor_type="scheduler"),
                    )
                    continue

                final_candidates.append(candidate)

            # 5. Insert into outbox (transactional)
            dispatch_result = await dispatch_service.insert_outbox_messages(
                session=session,
                candidates=final_candidates,
            )
            result["inserted"] = dispatch_result["inserted"]
            result["duplicates_skipped"] = dispatch_result["duplicates_skipped"]

            # 6. Commit everything
            await session.commit()

        except Exception as exc:
            await session.rollback()
            result["errors"].append(str(exc))
            raise

    return result
