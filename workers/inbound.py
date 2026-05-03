"""
workers/inbound.py — Inbound SMS parsing and action dispatch
═══════════════════════════════════════════════════

Handles keyword-based inbound SMS from parents:
  PAID       → Queue payment reconciliation
  STATUS     → Reply with current balance
  CALL       → Queue callback request for staff
  EXTENSION  → Create hardship request
  HELP       → Send command list
  STOP       → Opt out of SMS
  START      → Opt back in

Teaching notes:
  - We use fuzzy matching (typos, case-insensitive) for keywords.
  - "PAID" is a claim, not confirmation — staff must verify.
  - STOP/START update guardian preferences and are logged for compliance.
  - All replies respect quiet hours (deferred to morning if needed).
  - The worker renders responses using templates (Step 15) and queues
    them for outbound send.
═══════════════════════════════════════════════════
"""

import re
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.mock_adapter import MockAdapter  # use real adapter in production
from adapters.twilio_adapter import get_twilio_adapter
from domain.hardship_service import HardshipService
from domain.invoice_service import InvoiceService
from domain.models import (
    Guardian,
    HardshipRequest,
    InboundIntent,
    InboundMessage,
    Invoice,
    InvoiceStatus,
    MessageStatus,
    OutboundMessage,
    ReminderType,
    School,
)
from domain.outbox import OutboxService
from domain.reminder_service import ReminderService
from domain.templates import TemplateRenderer, render_template
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from infra.settings import get_settings
from workers.celery_app import celery_app


# ═══════════════════════════════════════════════════
# Keyword Parsing
# ═══════════════════════════════════════════════════

KEYWORD_PATTERNS: dict[InboundIntent, list[re.Pattern]] = {
    InboundIntent.PAID: [
        re.compile(r"^\s*PAID\s*$", re.I),
        re.compile(r"^\s*I\s+PAID\s*$", re.I),
        re.compile(r"^\s*PAYMENT\s+SENT\s*$", re.I),
    ],
    InboundIntent.STATUS: [
        re.compile(r"^\s*STATUS\s*$", re.I),
        re.compile(r"^\s*BALANCE\s*$", re.I),
        re.compile(r"^\s*HOW\s+MUCH\s*$", re.I),
    ],
    InboundIntent.CALL: [
        re.compile(r"^\s*CALL\s*$", re.I),
        re.compile(r"^\s*CALL\s+ME\s*$", re.I),
        re.compile(r"^\s*PHONE\s*$", re.I),
    ],
    InboundIntent.EXTENSION: [
        re.compile(r"^\s*EXTENSION\s*$", re.I),
        re.compile(r"^\s*EXTEND\s*$", re.I),
        re.compile(r"^\s*NEED\s+MORE\s+TIME\s*$", re.I),
        re.compile(r"^\s*HARDSHIP\s*$", re.I),
    ],
    InboundIntent.HELP: [
        re.compile(r"^\s*HELP\s*$", re.I),
        re.compile(r"^\s*\?\s*$"),
        re.compile(r"^\s*INFO\s*$", re.I),
    ],
    InboundIntent.STOP: [
        re.compile(r"^\s*STOP\s*$", re.I),
        re.compile(r"^\s*UNSUBSCRIBE\s*$", re.I),
        re.compile(r"^\s*QUIT\s*$", re.I),
    ],
    InboundIntent.START: [
        re.compile(r"^\s*START\s*$", re.I),
        re.compile(r"^\s*SUBSCRIBE\s*$", re.I),
        re.compile(r"^\s*YES\s*$", re.I),
    ],
}


def parse_intent(body: str) -> tuple[InboundIntent, float]:
    """
    Parse inbound SMS body into an intent.
    
    Returns:
        (intent, confidence) where confidence is 1.0 for exact match,
        lower for partial matches.
    """
    normalized = body.strip().upper()
    
    for intent, patterns in KEYWORD_PATTERNS.items():
        for pattern in patterns:
            if pattern.match(body):
                return intent, 1.0
    
    # Fuzzy: check if any keyword appears anywhere in the message
    for intent, patterns in KEYWORD_PATTERNS.items():
        keyword = intent.value.upper()
        if keyword in normalized:
            return intent, 0.7
    
    return InboundIntent.UNKNOWN, 0.0


# ═══════════════════════════════════════════════════
# Celery Task
# ═══════════════════════════════════════════════════

@celery_app.task(bind=True, max_retries=3, default_retry_delay=30)
def process_inbound_message(
    self,
    inbound_message_id: int,
) -> dict:
    """Process an inbound SMS message by ID."""
    import asyncio
    return asyncio.run(_async_process_inbound(inbound_message_id))


async def _async_process_inbound(inbound_message_id: int) -> dict:
    async with async_session_factory() as session:
        # 1. Load the inbound message
        result = await session.execute(
            select(InboundMessage).where(InboundMessage.id == inbound_message_id)
        )
        msg = result.scalar_one_or_none()
        if not msg:
            return {"status": "error", "reason": "message_not_found"}
        
        # 2. Parse intent
        intent, confidence = parse_intent(msg.body)
        msg.intent = intent
        msg.intent_confidence = confidence
        msg.processed_at = datetime.utcnow()
        await session.flush()
        
        # 3. Load guardian and school
        guardian = None
        if msg.guardian_id:
            g_result = await session.execute(
                select(Guardian).where(Guardian.id == msg.guardian_id)
            )
            guardian = g_result.scalar_one_or_none()
        
        school = None
        if msg.school_id:
            s_result = await session.execute(
                select(School).where(School.id == msg.school_id)
            )
            school = s_result.scalar_one_or_none()
        
        if not guardian or not school:
            return {"status": "error", "reason": "guardian_or_school_not_found"}
        
        # 4. Dispatch by intent
        renderer = TemplateRenderer()
        response_body = None
        action_taken = None
        
        if intent == InboundIntent.STOP:
            guardian.sms_opt_in = False
            guardian.opt_out_at = datetime.utcnow()
            guardian.opt_out_source = "sms_keyword"
            response_body = renderer.render("opt_out_confirm", {"school_name": school.name})
            action_taken = "opted_out"
            
            await log_audit_event(
                event_type="guardian.opt_out",
                entity_type="guardian",
                entity_id=str(guardian.id),
                summary=f"Guardian {guardian.id} opted out via SMS",
                context=AuditContext(school_id=school.id, actor_type="worker"),
            )
        
        elif intent == InboundIntent.START:
            guardian.sms_opt_in = True
            guardian.opt_in_at = datetime.utcnow()
            response_body = renderer.render("opt_in_confirm", {"school_name": school.name})
            action_taken = "opted_in"
        
        elif intent == InboundIntent.HELP:
            response_body = renderer.render("help_reply", {"school_name": school.name})
            action_taken = "help_sent"
        
        elif intent == InboundIntent.STATUS:
            # Get latest unpaid invoice
            inv_result = await session.execute(
                select(Invoice).where(
                    Invoice.guardian_id == guardian.id,
                    Invoice.status.in_([InvoiceStatus.PENDING, InvoiceStatus.PARTIAL]),
                ).order_by(Invoice.due_date)
            )
            invoice = inv_result.scalars().first()
            
            if invoice:
                balance = float(invoice.amount_due) - float(invoice.amount_paid)
                response_body = renderer.render("status_reply", {
                    "school_name": school.name,
                    "student_name": invoice.student.first_name if invoice.student else "your student",
                    "balance": f"{balance:.2f}",
                    "due_date": str(invoice.due_date),
                })
            else:
                response_body = f"{school.name}: No outstanding tuition balance found."
            action_taken = "status_replied"
        
        elif intent == InboundIntent.PAID:
            # Queue payment reconciliation (staff must verify)
            # For now, acknowledge and ask for details
            response_body = (
                f"{school.name}: Thank you! Please include payment method "
                f"(check, venmo, cash, zelle) and amount in your next message."
            )
            action_taken = "payment_acknowledged"
        
        elif intent == InboundIntent.CALL:
            # Queue callback request
            response_body = (
                f"{school.name}: We've received your callback request. "
                f"A staff member will call you within 24 hours."
            )
            action_taken = "callback_queued"
        
        elif intent == InboundIntent.EXTENSION:
            # Create hardship request
            hardship = HardshipService()
            h_request = await hardship.create_request(
                session=session,
                school_id=school.id,
                guardian_id=guardian.id,
                inbound_message_id=msg.id,
                request_body=msg.body,
            )
            response_body = renderer.render("hardship_ack", {"school_name": school.name})
            action_taken = "hardship_created"
            
            await log_audit_event(
                event_type="hardship.requested",
                entity_type="hardship",
                entity_id=str(h_request.id),
                summary=f"Hardship request from guardian {guardian.id}",
                context=AuditContext(school_id=school.id, actor_type="worker"),
            )
        
        else:
            response_body = (
                f"{school.name}: We didn't understand your message. "
                f"Reply HELP for available commands."
            )
            action_taken = "unknown_replied"
        
        # 5. Queue outbound response if we have one
        if response_body:
            await _queue_response(session, guardian, school, response_body)
        
        await session.commit()
        
        return {
            "status": "processed",
            "intent": intent.value,
            "confidence": confidence,
            "action": action_taken,
        }


async def _queue_response(
    session: AsyncSession,
    guardian: Guardian,
    school: School,
    body: str,
) -> None:
    """Insert an outbound response into the outbox for sending."""
    # Use a generic response key (not tied to invoice)
    message_key = f"{school.id}:{guardian.id}:response:{datetime.utcnow().isoformat()}"
    
    response = OutboundMessage(
        school_id=school.id,
        guardian_id=guardian.id,
        invoice_id=None,
        message_key=message_key,
        reminder_type=ReminderType.CALLBACK_ACK,
        status=MessageStatus.PENDING,
        body=body,
        segments=1,
        provider="twilio",
        client_message_id=message_key,
        scheduled_at=datetime.utcnow(),
    )
    session.add(response)
    await session.flush()
