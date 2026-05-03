"""
workers/sends.py — Celery task: poll outbox and dispatch SMS (Step 11 update)
═══════════════════════════════════════════════════

Now using the real SMS adapter with idempotent sends.
═══════════════════════════════════════════════════
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.sms_adapter import ErrorCategory, SendStatus
from adapters.twilio_adapter import get_twilio_adapter
from domain.models import Guardian, MessageStatus, OutboundMessage
from domain.outbox import OutboxService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from infra.settings import get_settings
from workers.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def poll_and_send_messages(self, batch_size: int = 100) -> dict:
    import asyncio
    return asyncio.run(_async_poll_and_send(batch_size))


async def _async_poll_and_send(batch_size: int) -> dict:
    outbox = OutboxService()
    adapter = get_twilio_adapter()
    result = {"sent": 0, "failed": 0, "unknown": 0, "claimed": 0, "skipped": 0}

    async with async_session_factory() as session:
        try:
            messages = await outbox.poll_pending(session, batch_size=batch_size)
            if not messages:
                return result

            for message in messages:
                try:
                    # 1. Claim
                    claimed = await outbox.claim_for_sending(session, message)
                    if not claimed:
                        result["skipped"] += 1
                        continue
                    result["claimed"] += 1

                    # 2. Load guardian phone
                    guardian_result = await session.execute(
                        select(Guardian).where(Guardian.id == message.guardian_id)
                    )
                    guardian = guardian_result.scalar_one_or_none()
                    if not guardian:
                        await outbox.transition_status(session, message, MessageStatus.FAILED)
                        result["failed"] += 1
                        continue

                    # 3. Render body (Step 15)
                    if not message.body:
                        message.body = _render_body(message)

                    # 4. Send via adapter
                    send_result = await adapter.send(
                        to=guardian.phone,
                        body=message.body,
                        client_message_id=message.client_message_id,
                    )

                    # 5. Handle result
                    if send_result.status == SendStatus.ACCEPTED:
                        await outbox.transition_status(
                            session, message, MessageStatus.SENT,
                            provider_message_id=send_result.provider_message_id,
                        )
                        result["sent"] += 1
                    elif send_result.error_category == ErrorCategory.AMBIGUOUS:
                        await outbox.transition_status(
                            session, message, MessageStatus.UNKNOWN_DELIVERY,
                        )
                        result["unknown"] += 1
                    else:
                        await outbox.transition_status(
                            session, message, MessageStatus.FAILED,
                        )
                        result["failed"] += 1

                    # 6. Audit log
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
                    result["failed"] += 1
                    continue

            await session.commit()

        except Exception:
            await session.rollback()
            raise

    return result


def _render_body(message: OutboundMessage) -> str:
    """Placeholder body renderer. Step 15 will implement full templates."""
    templates = {
        "due_14": "Friendly reminder: tuition payment is due in 14 days. Reply HELP for options.",
        "due_3": "Reminder: tuition payment is due in 3 days. Reply HELP for options.",
        "due_today": "Reminder: tuition payment is due today. Reply HELP for options.",
        "late_notice": "Your tuition payment is now overdue. Please contact the office or reply CALL to speak with us.",
    }
    return templates.get(message.reminder_type.value, "Tuition reminder from your school.")
