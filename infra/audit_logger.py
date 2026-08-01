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
from typing import Optional, TYPE_CHECKING

from sqlalchemy import insert

from domain.models import AuditEvent, AuditEventType
from infra.database import async_session_factory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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
    session: Optional["AsyncSession"] = None,
) -> None:
    """
    Insert an audit event into the database.

    S7: Transactional audit logging — if a session is provided, the event
    is inserted into that session's transaction (same commit/rollback).
    If no session is provided, a standalone session is created.
    
    Example (transactional — rolls back with caller):
        await log_audit_event(
            event_type=AuditEventType.REMINDER_SUPPRESSED,
            entity_type="message",
            entity_id="msg_123",
            summary="Reminder suppressed: invoice already paid",
            context=AuditContext(school_id=1, actor_type="scheduler"),
            session=my_session,  # ← same transaction
        )

    Example (standalone — commits independently):
        await log_audit_event(...)  # no session → standalone commit
    """
    ctx = context or AuditContext()

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

    if session is not None:
        # S7: Insert into caller's transaction (no separate commit)
        await session.execute(stmt)
    else:
        # Fallback: standalone session with its own commit
        async with async_session_factory() as standalone:
            await standalone.execute(stmt)
            await standalone.commit()
