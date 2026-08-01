"""
domain/dead_letter.py — Dead letter queue for permanently failed messages
═══════════════════════════════════════════════════

When a message exhausts its retry budget (retry_count >= max_retries) or
suffers a non-retryable failure, it is moved to the dead letter queue
instead of being silently discarded. This preserves the message content
and failure context for manual investigation or replay.

Flow:
  OutboundMessage (FAILED, retries exhausted)
    → DeadLetterService.move_to_dead_letter()
    → DeadLetterMessage record created
    → Original message can be archived or deleted

Replay:
  DeadLetterService.replay_from_dead_letter()
    → New OutboundMessage created with PENDING status
    → Dead letter record removed (or marked replayed)

Teaching notes:
  - The dead letter table is separate from outbound_messages so that
    retention purges on the outbox don't lose dead-lettered messages.
  - We store the full message body and metadata so operators can
    inspect what failed without joining back to the original table
    (which may have been purged).
  - The ORM model is defined here (not in models.py) to keep the
    dead-letter concern self-contained. It registers with Base.metadata
    on import, so init_db() will create the table as long as this
    module is imported at startup.
═══════════════════════════════════════════════════
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, String, Text, func, select, delete
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import (
    AuditEventType,
    MessageStatus,
    OutboundMessage,
    ReminderType,
)
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import Base, async_session_factory


# ═══════════════════════════════════════════════════
# ORM Model — dead_letter_messages table
# ═══════════════════════════════════════════════════

class DeadLetterMessageORM(Base):
    """
    Persistence model for dead-lettered messages.

    This table holds messages that have exhausted retries or suffered
    permanent failures. It is separate from outbound_messages so that
    outbox retention purges do not lose dead-lettered content.
    """
    __tablename__ = "dead_letter_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    original_message_id: Mapped[int] = mapped_column(
        ForeignKey("outbound_messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    school_id: Mapped[int] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    guardian_id: Mapped[int] = mapped_column(
        ForeignKey("guardians.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_key: Mapped[str] = mapped_column(String(255), nullable=False)
    reminder_type: Mapped[ReminderType] = mapped_column(
        Enum(ReminderType, name="reminder_type"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    failure_reason: Mapped[str] = mapped_column(Text, nullable=False)

    original_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    dead_lettered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_dead_letter_school_created", "school_id", "dead_lettered_at"),
    )


# ═══════════════════════════════════════════════════
# Dataclass — public return type
# ═══════════════════════════════════════════════════

@dataclass(frozen=True)
class DeadLetterMessage:
    """
    Immutable snapshot of a dead-lettered message.

    Returned by DeadLetterService methods. This is a plain dataclass
    (not an ORM model) so callers receive a detached, serialisable
    object with no session lifecycle attached.
    """
    id: int
    original_message_id: int
    school_id: int
    guardian_id: int
    message_key: str
    reminder_type: ReminderType
    body: str
    failure_reason: str
    original_created_at: datetime
    dead_lettered_at: datetime


def _orm_to_dataclass(row: DeadLetterMessageORM) -> DeadLetterMessage:
    """Convert an ORM row to the frozen dataclass return type."""
    return DeadLetterMessage(
        id=row.id,
        original_message_id=row.original_message_id,
        school_id=row.school_id,
        guardian_id=row.guardian_id,
        message_key=row.message_key,
        reminder_type=row.reminder_type,
        body=row.body,
        failure_reason=row.failure_reason,
        original_created_at=row.original_created_at,
        dead_lettered_at=row.dead_lettered_at,
    )


# ═══════════════════════════════════════════════════
# Service
# ═══════════════════════════════════════════════════

class DeadLetterService:
    """
    Manages the dead letter queue for failed outbound messages.

    All methods accept an AsyncSession so they can participate in the
    caller's transaction. Use async_session_factory() for standalone calls.
    """

    async def move_to_dead_letter(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        reason: str,
    ) -> DeadLetterMessage:
        """
        Create a dead letter record from a failed outbound message.

        Copies all relevant fields from the original message so the
        dead letter is self-contained (survives even if the original
        outbound_messages row is later purged).

        Args:
            session: active async DB session
            message: the failed OutboundMessage to dead-letter
            reason: human-readable failure reason (provider error, exhausted retries, etc.)

        Returns:
            DeadLetterMessage dataclass snapshot of the created record
        """
        row = DeadLetterMessageORM(
            original_message_id=message.id,
            school_id=message.school_id,
            guardian_id=message.guardian_id,
            message_key=message.message_key,
            reminder_type=message.reminder_type,
            body=message.body,
            failure_reason=reason,
            original_created_at=message.created_at,
            dead_lettered_at=datetime.utcnow(),
        )
        session.add(row)
        await session.flush()  # populate row.id

        # Audit the dead-lettering
        await log_audit_event(
            event_type=AuditEventType.MESSAGE_FAILED,
            entity_type="dead_letter",
            entity_id=str(row.id),
            summary=f"Message {message.message_key} moved to dead letter: {reason}",
            context=AuditContext(
                school_id=message.school_id,
                actor_type="system",
                actor_id="dead_letter_service",
            ),
        )

        return _orm_to_dataclass(row)

    async def list_dead_letters(
        self,
        session: AsyncSession,
        school_id: int,
        limit: int = 50,
    ) -> list[DeadLetterMessage]:
        """
        List dead-lettered messages for a school, most recent first.

        Args:
            session: active async DB session
            school_id: school to filter by
            limit: maximum number of records to return (default 50)

        Returns:
            List of DeadLetterMessage dataclass snapshots
        """
        stmt = (
            select(DeadLetterMessageORM)
            .where(DeadLetterMessageORM.school_id == school_id)
            .order_by(DeadLetterMessageORM.dead_lettered_at.desc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [_orm_to_dataclass(r) for r in rows]

    async def replay_from_dead_letter(
        self,
        session: AsyncSession,
        dead_letter_id: int,
    ) -> OutboundMessage:
        """
        Move a dead-lettered message back to the outbox for retry.

        Creates a new OutboundMessage with PENDING status and a fresh
        retry budget, then removes the dead letter record. The new
        message gets a unique message_key suffix to avoid collision
        with the original (which may still exist in outbound_messages).

        Args:
            session: active async DB session
            dead_letter_id: id of the DeadLetterMessageORM to replay

        Returns:
            The newly created OutboundMessage (PENDING status, ready for the send worker)

        Raises:
            ValueError: if the dead letter record is not found
        """
        # 1. Load the dead letter record
        result = await session.execute(
            select(DeadLetterMessageORM).where(
                DeadLetterMessageORM.id == dead_letter_id
            )
        )
        dl = result.scalar_one_or_none()
        if dl is None:
            raise ValueError(f"Dead letter {dead_letter_id} not found")

        # 2. Create a new outbound message for retry
        #    Append replay suffix to message_key to avoid unique constraint violation
        replay_key = f"{dl.message_key}:replay:{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

        new_message = OutboundMessage(
            school_id=dl.school_id,
            guardian_id=dl.guardian_id,
            message_key=replay_key,
            reminder_type=dl.reminder_type,
            status=MessageStatus.PENDING,
            body=dl.body,
            retry_count=0,
            max_retries=3,
            scheduled_at=datetime.utcnow(),
        )
        session.add(new_message)
        await session.flush()

        # 3. Remove the dead letter record (replayed successfully)
        await session.execute(
            delete(DeadLetterMessageORM).where(
                DeadLetterMessageORM.id == dead_letter_id
            )
        )

        # 4. Audit the replay
        await log_audit_event(
            event_type=AuditEventType.MESSAGE_SEND_ATTEMPT,
            entity_type="dead_letter",
            entity_id=str(dead_letter_id),
            summary=f"Dead letter {dead_letter_id} replayed as new outbound message {new_message.id}",
            context=AuditContext(
                school_id=dl.school_id,
                actor_type="system",
                actor_id="dead_letter_service",
            ),
        )

        return new_message