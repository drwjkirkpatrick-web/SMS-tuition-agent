"""
workers/sends.py — Celery task: poll outbox and dispatch SMS
═══════════════════════════════════════════════════

This is the "worker" side of the transactional outbox pattern:
  1. Poll `outbound_messages` for pending rows
  2. Claim with row lock (status = sending)
  3. Render message body from template (Step 15)
  4. Call SMS adapter (Twilio) with idempotent client_message_id
  5. Update status based on provider response

Teaching notes:
  - The worker is idempotent: if it crashes after sending but before
    updating the database, the message stays as `sending`. Another
    worker will not pick it up (status != pending). A reconciliation
    loop (Step 14) resolves `sending` messages that never got updated.
  - We use `async_session_factory()` with explicit commit/rollback
    because each message is its own transaction.
  - If a send fails with a retryable error (network timeout), we mark
    `unknown_delivery` — NOT `failed`. Only permanent errors (invalid
    phone number) get `failed` immediately.
═══════════════════════════════════════════════════
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import MessageStatus, OutboundMessage
from domain.outbox import OutboxService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from infra.settings import get_settings
from workers.celery_app import celery_app


# Placeholder: SMS adapter interface (will be implemented in Step 11)
# from adapters.sms_adapter import get_sms_adapter


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def poll_and_send_messages(self, batch_size: int = 100) -> dict:
    """
    Celery task: poll outbox and send pending SMS messages.
    
    Returns:
        {"sent": N, "failed": M, "unknown": K, "claimed": L}
    """
    import asyncio
    return asyncio.run(_async_poll_and_send(batch_size))


async def _async_poll_and_send(batch_size: int) -> dict:
    outbox = OutboxService()
    result = {"sent": 0, "failed": 0, "unknown": 0, "claimed": 0, "skipped": 0}

    async with async_session_factory() as session:
        try:
            # 1. Poll for pending messages
            messages = await outbox.poll_pending(session, batch_size=batch_size)
            if not messages:
                return result

            for message in messages:
                try:
                    # 2. Claim for sending
                    claimed = await outbox.claim_for_sending(session, message)
                    if not claimed:
                        result["skipped"] += 1
                        continue
                    result["claimed"] += 1

                    # 3. Render body (Step 15 — placeholder for now)
                    # message.body = render_template(message.reminder_type, ...)
                    if not message.body:
                        message.body = "Reminder: tuition payment due. Reply HELP for options."

                    # 4. Send via SMS adapter (Step 11)
                    # For now, simulate send and mark as unknown_delivery
                    # to demonstrate the reconciliation path.
                    send_result = await _send_sms(session, message)

                    if send_result == "sent":
                        await outbox.transition_status(
                            session, message, MessageStatus.SENT,
                            provider_message_id=f"SIMULATED_{message.id}",
                        )
                        result["sent"] += 1
                    elif send_result == "unknown":
                        await outbox.transition_status(
                            session, message, MessageStatus.UNKNOWN_DELIVERY,
                        )
                        result["unknown"] += 1
                    else:
                        await outbox.transition_status(
                            session, message, MessageStatus.FAILED,
                        )
                        result["failed"] += 1

                    # 5. Log audit event
                    await log_audit_event(
                        event_type="message.send_attempt",
                        entity_type="message",
                        entity_id=message.message_key,
                        summary=f"SMS {message.status.value}: {message.reminder_type.value}",
                        context=AuditContext(
                            school_id=message.school_id,
                            actor_type="worker",
                            actor_id="poll_and_send",
                        ),
                    )

                except Exception as exc:
                    # Mark as failed for this specific message
                    await session.rollback()
                    # Start fresh session for next message
                    # (In production, you'd want per-message transactions)
                    result["failed"] += 1
                    continue

            # Commit all successful sends in this batch
            await session.commit()

        except Exception:
            await session.rollback()
            raise

    return result


async def _send_sms(
    session: AsyncSession,
    message: OutboundMessage,
) -> str:
    """
    Placeholder SMS send function.
    Step 11 will replace this with real Twilio adapter.
    
    Returns:
        "sent" — provider accepted
        "unknown" — network timeout, ambiguous
        "failed" — permanent error
    """
    # PLACEHOLDER: simulate successful send
    # In production:
    #   adapter = get_sms_adapter()
    #   response = await adapter.send(
    #       to=guardian.phone,
    #       body=message.body,
    #       client_message_id=message.client_message_id,
    #   )
    return "sent"
