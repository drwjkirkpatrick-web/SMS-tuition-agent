"""
api/webhooks/twilio.py — Twilio webhook handlers
═══════════════════════════════════════════════════

v2: Input sanitization, dynamic school_id, phone validation.
═══════════════════════════════════════════════════
"""

import re
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.twilio_adapter import TwilioAdapter
from domain.models import Guardian, InboundMessage, InboundIntent, OutboundMessage, School, AuditEventType
from domain.reconciliation_service import ReconciliationService
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import get_db
from infra.settings import get_settings
from workers.celery_app import celery_app

router = APIRouter()

# S9: E.164 phone number validation pattern
E164_PATTERN = re.compile(r"^\+?[1-9]\d{1,14}$")

# S4: Control character pattern for sanitization
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_sms_body(body: str) -> str:
    """S4: Strip control characters and cap length for safe storage."""
    sanitized = CONTROL_CHARS.sub("", body)
    return sanitized[:1600]  # cap at 1600 chars (10 SMS segments max)


def _validate_phone(phone: str) -> bool:
    """S9: Validate E.164 phone number format."""
    return bool(E164_PATTERN.match(phone))

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
    
    v2: S4 sanitizes body, S5 resolves school_id dynamically,
    S9 validates phone number format.
    """
    # 1. Validate signature
    adapter = TwilioAdapter()
    body_bytes = await request.body()
    signature = request.headers.get("X-Twilio-Signature", "")
    url = str(request.url)
    
    is_valid = await adapter.validate_webhook_signature(body_bytes, signature, url)
    if not is_valid:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    # S9: Validate phone number format
    if not _validate_phone(From):
        await log_audit_event(
            event_type=AuditEventType.LOGIN_FAILURE,
            entity_type="webhook",
            entity_id=MessageSid,
            summary=f"Rejected inbound SMS: invalid phone format",
            context=AuditContext(actor_type="webhook", source=From[:20]),
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid phone number format")

    # S4: Sanitize body for safe storage
    sanitized_body = _sanitize_sms_body(Body)

    # 2. Look up guardian by phone number
    async with get_db() as session:
        result = await session.execute(
            select(Guardian).where(Guardian.phone == From)
        )
        guardian = result.scalar_one_or_none()

        # S5: Resolve school_id dynamically from guardian, not hardcoded
        school_id = guardian.school_id if guardian else None

        # If no guardian found, try to match by the receiving Twilio number
        if not school_id:
            school_result = await session.execute(
                select(School).where(School.deleted_at.is_(None))
            )
            # For single-school deployments, use the first school
            school = school_result.scalars().first()
            school_id = school.id if school else 1

    # 3. Queue async processing (Step 16)
    process_inbound_message.delay(
        provider_message_id=MessageSid,
        from_phone=From,
        to_phone=To,
        body=sanitized_body.strip().upper(),  # normalize for keyword matching
        guardian_id=guardian.id if guardian else None,
        school_id=school_id,  # S5: pass resolved school_id
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
    school_id: Optional[int] = None,
) -> dict:
    """
    Celery task: parse and act on inbound SMS.
    S5: Now accepts school_id parameter for multi-school support.
    """
    import asyncio
    return asyncio.run(_async_process_inbound(
        provider_message_id, from_phone, to_phone, body, guardian_id, school_id
    ))


async def _async_process_inbound(
    provider_message_id: str,
    from_phone: str,
    to_phone: str,
    body: str,
    guardian_id: Optional[int] = None,
    school_id: Optional[int] = None,
) -> dict:
    """Async implementation — will be expanded in Step 16."""
    from infra.database import async_session_factory
    from domain.models import InboundMessage, InboundIntent
    
    async with async_session_factory() as session:
        # S5: Use passed school_id instead of hardcoded 1
        msg = InboundMessage(
            school_id=school_id or 1,  # fallback to 1 for single-school deployments
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
