"""
api/webhooks/twilio.py — Twilio webhook handlers
═══════════════════════════════════════════════════

Handles two types of Twilio webhooks:
  1. Status callbacks — delivery receipts (sent/delivered/failed)
  2. Inbound SMS — messages from parents (PAID, STATUS, CALL, etc.)

Security:
  - Validates Twilio signature using HMAC-SHA1 + Auth Token
  - Rejects requests with invalid signatures (returns 403)
  - Deduplicates callbacks by provider_event_id

Teaching notes:
  - FastAPI routers let us group related endpoints.
    `prefix="/webhooks"` means all routes here start with /webhooks/...
  - Twilio sends webhooks as application/x-www-form-urlencoded POSTs.
    FastAPI's `Form(...)` extracts form fields.
  - The `request` parameter gives us access to the raw body (needed
    for signature validation — we must verify the exact bytes).
  - We queue inbound SMS parsing as a Celery task so the webhook
    responds immediately (Twilio times out after 15 seconds).
═══════════════════════════════════════════════════
"""

from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.twilio_adapter import TwilioAdapter
from domain.models import Guardian, InboundMessage, InboundIntent, OutboundMessage
from domain.reconciliation_service import ReconciliationService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import get_db
from infra.settings import get_settings
from workers.celery_app import celery_app

router = APIRouter()

# ── Status Callback (Delivery Receipt) ──

@router.post("/twilio/status")
async def twilio_status_callback(
    request: Request,
    MessageSid: str = Form(...),          # Twilio Message SID
    MessageStatus: str = Form(...),       # sent, delivered, failed, etc.
    ErrorCode: Optional[str] = Form(None),
    To: str = Form(...),
    From: str = Form(...),
    # Twilio echoes our custom params back
    client_id: Optional[str] = Form(None),  # our client_message_id
) -> dict:
    """
    Handle Twilio delivery status webhook.
    
    Twilio POSTs this when a message's status changes.
    We validate the signature, then update our outbox.
    """
    # 1. Validate signature
    adapter = TwilioAdapter()
    body = await request.body()
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    
    is_valid = await adapter.validate_webhook_signature(body, signature, url)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    # 2. Find our message by client_message_id or provider_message_id
    async with get_db() as session:
        recon = ReconciliationService()
        
        # Try to find by client_message_id first
        message = None
        if client_id:
            result = await session.execute(
                select(OutboundMessage).where(OutboundMessage.client_message_id == client_id)
            )
            message = result.scalar_one_or_none()
        
        # Fallback: find by provider_message_id
        if not message:
            result = await session.execute(
                select(OutboundMessage).where(OutboundMessage.provider_message_id == MessageSid)
            )
            message = result.scalar_one_or_none()

        if not message:
            # Message not found — could be a test webhook or stale data
            return {"status": "ignored", "reason": "message_not_found"}

        # 3. Process callback with dedupe
        # provider_event_id = MessageSid + MessageStatus (unique per event)
        provider_event_id = f"{MessageSid}:{MessageStatus}"
        
        try:
            await recon.process_delivery_callback(
                session=session,
                message=message,
                provider_event_id=provider_event_id,
                provider_status=MessageStatus,
                raw_payload=str(dict(await request.form())),
            )
            await session.commit()
        except Exception as exc:
            # Duplicate callback (unique constraint violation) — silently accept
            if "duplicate key" in str(exc).lower() or "unique constraint" in str(exc).lower():
                return {"status": "duplicate_ignored"}
            raise

    # 4. Audit log
    await log_audit_event(
        event_type="message.delivered",
        entity_type="message",
        entity_id=message.message_key,
        summary=f"Delivery callback: {MessageStatus}",
        context=AuditContext(school_id=message.school_id, actor_type="webhook"),
    )

    return {"status": "processed", "message_status": MessageStatus}


# ── Inbound SMS Webhook ──

@router.post("/twilio/inbound")
async def twilio_inbound_sms(
    request: Request,
    MessageSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(...),
    NumMedia: int = Form(0),
) -> dict:
    """
    Handle inbound SMS from parents.
    
    Queues the message for async processing so we respond to
    Twilio immediately (required: Twilio times out after 15s).
    """
    # 1. Validate signature
    adapter = TwilioAdapter()
    body_bytes = await request.body()
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    
    is_valid = await adapter.validate_webhook_signature(body_bytes, signature, url)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    # 2. Look up guardian by phone number
    async with get_db() as session:
        result = await session.execute(
            select(Guardian).where(Guardian.phone == From)
        )
        guardian = result.scalar_one_or_none()

    # 3. Queue async processing (Step 16)
    process_inbound_message.delay(
        provider_message_id=MessageSid,
        from_phone=From,
        to_phone=To,
        body=Body.strip().upper(),  # normalize for keyword matching
        guardian_id=guardian.id if guardian else None,
    )

    # 4. Return empty 200 (Twilio doesn't use the response body)
    return {"status": "queued"}


@celery_app.task
def process_inbound_message(
    provider_message_id: str,
    from_phone: str,
    to_phone: str,
    body: str,
    guardian_id: Optional[int] = None,
) -> dict:
    """
    Celery task: parse and act on inbound SMS.
    
    Delegated to workers.inbound module (Step 16).
    This stub exists so the webhook can queue it immediately.
    """
    import asyncio
    return asyncio.run(_async_process_inbound(
        provider_message_id, from_phone, to_phone, body, guardian_id
    ))


async def _async_process_inbound(
    provider_message_id: str,
    from_phone: str,
    to_phone: str,
    body: str,
    guardian_id: Optional[int] = None,
) -> dict:
    """Async implementation — will be expanded in Step 16."""
    from infra.database import async_session_factory
    from domain.models import InboundMessage, InboundIntent
    
    async with async_session_factory() as session:
        # Store the inbound message
        msg = InboundMessage(
            school_id=1,  # TODO: resolve from guardian
            guardian_id=guardian_id,
            provider="twilio",
            provider_message_id=provider_message_id,
            from_phone=from_phone,
            to_phone=to_phone,
            body=body,
            intent=InboundIntent.UNKNOWN,  # Step 16: parse body into intent
            processed_at=None,
        )
        session.add(msg)
        await session.commit()
    
    return {"status": "stored", "intent": "unknown"}
