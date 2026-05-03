"""
domain/reconciliation_service.py — Payment and callback reconciliation
═══════════════════════════════════════════════════

Handles:
  - Payment reconciliation: matching parent-reported payments to invoices
  - Delivery callback processing: updating message status from Twilio webhooks
  - Unknown delivery reconciliation: querying provider for ambiguous sends

Teaching notes:
  - "Reconciliation" means "making two records agree." When a parent
    texts "PAID" or when Twilio sends a delivery receipt, we update
    our database to match reality.
  - All reconciliation operations are idempotent: running them twice
    produces the same final state.
  - Payment reconciliation uses `external_reference` to match against
    bank/Venmo/Stripe records (if integrated).
═══════════════════════════════════════════════════
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.invoice_service import InvoiceService
from domain.models import (
    DeliveryCallback,
    Guardian,
    Invoice,
    InvoiceStatus,
    MessageStatus,
    OutboundMessage,
    Payment,
    PaymentStatus,
)


class ReconciliationService:
    """
    Reconciles payments and delivery statuses.
    """

    def __init__(self):
        self.invoice_service = InvoiceService()

    async def reconcile_payment(
        self,
        session: AsyncSession,
        invoice: Invoice,
        amount: Decimal,
        payment_method: Optional[str] = None,
        external_reference: Optional[str] = None,
        confirmed_by: str = "system",
    ) -> Payment:
        """
        Record a confirmed payment and update invoice status.
        Idempotent: if payment with same external_reference already exists,
        return existing payment without creating a new one.
        """
        # Check for existing payment by external reference
        if external_reference:
            result = await session.execute(
                select(Payment).where(
                    Payment.invoice_id == invoice.id,
                    Payment.external_reference == external_reference,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                return existing

        # Record new payment
        payment = await self.invoice_service.record_payment(
            session=session,
            invoice=invoice,
            amount=amount,
            payment_method=payment_method,
            external_reference=external_reference,
        )
        payment.confirmed_by = confirmed_by
        payment.confirmed_at = datetime.utcnow()
        await session.flush()
        return payment

    async def process_delivery_callback(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        provider_event_id: str,
        provider_status: str,
        raw_payload: Optional[str] = None,
    ) -> DeliveryCallback:
        """
        Process a delivery callback from the SMS provider.
        Updates message.status based on provider_status.
        
        provider_status mapping:
          "sent" / "queued" / "accepted" → MessageStatus.SENT
          "delivered" → MessageStatus.DELIVERED
          "failed" / "undelivered" / "rejected" → MessageStatus.FAILED
        """
        # Create callback record (unique constraint on provider_event_id prevents duplicates)
        callback = DeliveryCallback(
            message_id=message.id,
            provider=message.provider,
            provider_event_id=provider_event_id,
            provider_status=provider_status,
            raw_payload=raw_payload,
        )
        session.add(callback)
        await session.flush()

        # Update message status
        status_map = {
            "sent": MessageStatus.SENT,
            "queued": MessageStatus.SENT,
            "accepted": MessageStatus.SENT,
            "delivered": MessageStatus.DELIVERED,
            "failed": MessageStatus.FAILED,
            "undelivered": MessageStatus.FAILED,
            "rejected": MessageStatus.FAILED,
        }
        new_status = status_map.get(provider_status.lower())
        if new_status:
            message.status = new_status
            if new_status == MessageStatus.SENT:
                message.sent_at = message.sent_at or datetime.utcnow()
            elif new_status == MessageStatus.DELIVERED:
                message.delivered_at = datetime.utcnow()
            elif new_status == MessageStatus.FAILED:
                message.failed_at = datetime.utcnow()
            message.updated_at = datetime.utcnow()
            await session.flush()

        return callback

    async def reconcile_unknown_delivery(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        provider_status: str,  # status queried from provider API
    ) -> None:
        """
        Resolve an UNKNOWN_DELIVERY message after querying the provider.
        
        Args:
            provider_status: "sent", "delivered", "failed", or "not_found"
        """
        if provider_status == "not_found":
            # Provider never received it — safe to retry
            message.status = MessageStatus.PENDING
            message.retry_count += 1
        elif provider_status in ("sent", "queued", "accepted"):
            message.status = MessageStatus.SENT
            message.sent_at = message.sent_at or datetime.utcnow()
        elif provider_status == "delivered":
            message.status = MessageStatus.DELIVERED
            message.delivered_at = datetime.utcnow()
        elif provider_status in ("failed", "undelivered", "rejected"):
            message.status = MessageStatus.FAILED
            message.failed_at = datetime.utcnow()

        message.updated_at = datetime.utcnow()
        await session.flush()
