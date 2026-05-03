"""
infra/audit_logger.py — Audit event logging
═══════════════════════════════════════════════════

All security-relevant events are written to the `audit_events` table.
This module provides:
  - `log_audit_event()` — async function to insert events
  - `AuditContext` — dataclass for common event metadata

Teaching note: We use SQLAlchemy async insert directly (not Celery)
for audit logging because audit events must be written in the same
database transaction as the operation they describe. If the operation
fails and rolls back, the audit event rolls back too — preserving
causal consistency.
═══════════════════════════════════════════════════
"""

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import insert

from domain.models import AuditEvent, AuditEventType
from infra.database import async_session_factory


@dataclass(frozen=True)
class AuditContext:
    """
    Common metadata attached to every audit event.
    Frozen = immutable after creation (thread-safe, hashable).
    """
    school_id: Optional[int] = None
    actor_type: str = "system"          # "system", "worker", "user", "api"
    actor_id: Optional[str] = None      # worker hostname, user ID, API key name
    source: Optional[str] = None        # IP address or container hostname


async def log_audit_event(
    event_type: AuditEventType,
    entity_type: str,
    entity_id: Optional[str],
    summary: str,
    details: Optional[str] = None,
    context: Optional[AuditContext] = None,
) -> None:
    """
    Insert an audit event into the database.
    
    Call this within an existing session for transactional consistency,
    or pass a standalone session.
    
    Example:
        await log_audit_event(
            event_type=AuditEventType.REMINDER_SUPPRESSED,
            entity_type="message",
            entity_id="msg_123",
            summary="Reminder suppressed: invoice already paid",
            context=AuditContext(school_id=1, actor_type="scheduler"),
        )
    """
    ctx = context or AuditContext()
    
    async with async_session_factory() as session:
        stmt = insert(AuditEvent).values(
            school_id=ctx.school_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            details=details,
            actor_type=ctx.actor_type,
            actor_id=ctx.actor_id,
            source=ctx.source,
        )
        await session.execute(stmt)
        await session.commit()
