"""
workers/reconciliation.py — Full reconciliation tasks
═══════════════════════════════════════════════════

Tasks:
  1. reconcile_unknown_deliveries — resolve timed-out sends
  2. poll_payment_updates — sync payments from SIS
  3. reconcile_sis_sync — full SIS data import
═══════════════════════════════════════════════════
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.connector_factory import get_connector
from adapters.csv_connector import CSVConnector, persist_guardians, persist_students
from adapters.sis_connector import SyncCheckpoint
from adapters.twilio_adapter import get_twilio_adapter
from domain.models import (
    Guardian,
    Invoice,
    MessageStatus,
    OutboundMessage,
    Payment,
    School,
    Student,
)
from domain.outbox import OutboxService
from domain.reconciliation_service import ReconciliationService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from infra.settings import get_settings
from workers.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def reconcile_unknown_deliveries(self) -> dict:
    import asyncio
    return asyncio.run(_async_reconcile_unknown())


async def _async_reconcile_unknown() -> dict:
    outbox = OutboxService()
    adapter = get_twilio_adapter()
    recon = ReconciliationService()
    result = {"resolved": 0, "not_found": 0, "failed": 0, "errors": 0}

    async with async_session_factory() as session:
        messages = await outbox.get_unknown_deliveries(session, older_than_minutes=10)
        for message in messages:
            try:
                if not message.client_message_id:
                    continue
                query_result = await adapter.query_delivery(message.client_message_id)
                await recon.reconcile_unknown_delivery(session, message, query_result.status)

                if query_result.status == "not_found":
                    result["not_found"] += 1
                elif query_result.status in ("sent", "delivered"):
                    result["resolved"] += 1
                else:
                    result["failed"] += 1
            except Exception:
                result["errors"] += 1
                continue
        await session.commit()
    return result


@celery_app.task(bind=True, max_retries=3, default_retry_delay=300)
def poll_payment_updates(self, school_id: int = 1) -> dict:
    import asyncio
    return asyncio.run(_async_poll_payments(school_id))


async def _async_poll_payments(school_id: int) -> dict:
    """
    Poll SIS for payment updates and reconcile with invoices.
    For CSV connector, reads payments.csv and creates Payment records.
    """
    from domain.invoice_service import InvoiceService
    from decimal import Decimal
    
    result = {"synced": 0, "invoices_updated": 0, "errors": []}
    
    async with async_session_factory() as session:
        school_result = await session.execute(select(School).where(School.id == school_id))
        school = school_result.scalar_one_or_none()
        if not school:
            return result
        
        # Load connector
        connector = get_connector(
            school_id=school_id,
            adapter_type=school.sis_adapter_type or "csv",
            config={"csv_directory": "/data/sis_exports"} if not school.sis_config else {},
        )
        if not connector:
            result["errors"].append(f"No connector for type {school.sis_adapter_type}")
            return result
        
        # Get checkpoint
        checkpoint = await connector.get_checkpoint()
        
        # Sync payments
        invoice_service = InvoiceService()
        async for payment_record in connector.sync_payments(checkpoint):
            try:
                # Find invoice by SIS ID
                inv_result = await session.execute(
                    select(Invoice).where(
                        Invoice.school_id == school_id,
                        Invoice.sis_invoice_id == payment_record.sis_invoice_id,
                    )
                )
                invoice = inv_result.scalar_one_or_none()
                if not invoice:
                    continue
                
                # Reconcile payment
                payment = await invoice_service.record_payment(
                    session=session,
                    invoice=invoice,
                    amount=Decimal(str(payment_record.amount)),
                    payment_method=payment_record.payment_method,
                    external_reference=payment_record.sis_payment_id,
                )
                result["synced"] += 1
                
                # If invoice now paid, suppress any pending reminders
                if invoice.status.value == "paid":
                    result["invoices_updated"] += 1
                    # Mark pending reminders as suppressed
                    pending = await session.execute(
                        select(OutboundMessage).where(
                            OutboundMessage.invoice_id == invoice.id,
                            OutboundMessage.status == MessageStatus.PENDING,
                        )
                    )
                    for msg in pending.scalars().all():
                        msg.status = MessageStatus.SUPPRESSED
                        msg.suppression_reason = "invoice_paid"
                    await session.flush()
                
            except Exception as exc:
                result["errors"].append(str(exc))
                continue
        
        await session.commit()
    
    return result
