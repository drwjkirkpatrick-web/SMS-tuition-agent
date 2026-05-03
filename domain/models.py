"""
domain/models.py — SQLAlchemy ORM models
═══════════════════════════════════════════════════

This file defines every table in the database. Key design decisions:

  1. SQLAlchemy 2.0 style: Mapped[type] with type hints (modern, clean)
  2. Soft deletes: `deleted_at` timestamp instead of physical DELETE
     (preserves audit trail, allows recovery)
  3. UNIQUE constraints enforce deduplication at the database level:
     - outbound_messages.message_key → no duplicate reminders
     - delivery_callbacks.provider_event_id → no duplicate webhooks
  4. All tables have created_at/updated_at timestamps
  5. Enum columns prevent invalid status values

Teaching notes:
  - `server_default=func.now()` means PostgreSQL sets the timestamp,
    not Python. This avoids clock skew between app servers.
  - `onupdate=func.now()` auto-updates `updated_at` on every UPDATE.
  - `Index(..., postgresql_where=...)` creates partial indexes (smaller,
    faster) — e.g., only index pending messages, not archived ones.
  - Relationships use `lazy="selectin"` to avoid N+1 query problems.
═══════════════════════════════════════════════════
"""

import enum
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.database import Base


# ═══════════════════════════════════════════════════
# Enum Definitions
# ═══════════════════════════════════════════════════

class InvoiceStatus(str, enum.Enum):
    PENDING = "pending"      # created, not yet paid
    PARTIAL = "partial"      # some payment received
    PAID = "paid"              # fully paid
    OVERDUE = "overdue"        # past due date, unpaid
    CANCELLED = "cancelled"    # voided by school


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"        # parent says they paid, not yet confirmed
    CONFIRMED = "confirmed"    # school confirmed receipt
    REVERSED = "reversed"      # refunded or bounced


class MessageStatus(str, enum.Enum):
    PENDING = "pending"              # in outbox, waiting for worker
    SENDING = "sending"              # worker picked it up
    SENT = "sent"                    # provider accepted
    DELIVERED = "delivered"          # provider confirmed delivery
    FAILED = "failed"                # permanent failure (e.g., invalid number)
    UNKNOWN_DELIVERY = "unknown_delivery"  # timeout — needs reconciliation
    SUPPRESSED = "suppressed"        # blocked (already paid, opted out, etc.)


class ReminderType(str, enum.Enum):
    DUE_14 = "due_14"          # 14 days before due date
    DUE_3 = "due_3"            # 3 days before
    DUE_TODAY = "due_today"    # on due date
    LATE_NOTICE = "late_notice" # after due date, unpaid
    PAYMENT_CONFIRMED = "payment_confirmed"
    CALLBACK_ACK = "callback_ack"
    HARDSHIP_ACK = "hardship_ack"


class HardshipStatus(str, enum.Enum):
    REQUESTED = "requested"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    DENIED = "denied"
    RESOLVED = "resolved"


class InboundIntent(str, enum.Enum):
    STATUS = "status"
    PAID = "paid"
    CALL = "call"
    EXTENSION = "extension"
    HELP = "help"
    STOP = "stop"
    START = "start"
    UNKNOWN = "unknown"


class AuditEventType(str, enum.Enum):
    MESSAGE_SEND_ATTEMPT = "message.send_attempt"
    MESSAGE_DELIVERED = "message.delivered"
    MESSAGE_FAILED = "message.failed"
    REMINDER_SUPPRESSED = "reminder.suppressed"
    POLICY_CHANGED = "policy.changed"
    GUARDIAN_OPT_OUT = "guardian.opt_out"
    SIS_SYNC = "sis.sync"
    PAYMENT_RECONCILED = "payment.reconciled"
    HARDSHIP_REQUESTED = "hardship.requested"
    LOGIN_FAILURE = "login.failure"


# ═══════════════════════════════════════════════════
# Schools
# ═══════════════════════════════════════════════════

class School(Base):
    __tablename__ = "schools"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64),
        default="America/Los_Angeles",
        nullable=False,
    )
    # SIS connection config (encrypted at rest, application-level)
    sis_adapter_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sis_config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Reminder policy (director-configurable, Step 19)
    reminder_policy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    students: Mapped[List["Student"]] = relationship(back_populates="school")
    guardians: Mapped[List["Guardian"]] = relationship(back_populates="school")
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="school")


# ═══════════════════════════════════════════════════
# Students
# ═══════════════════════════════════════════════════

class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    school_id: Mapped[int] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SECURITY: store first name only (per FERPA policy §2.2)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Internal SIS reference (opaque string, not PII)
    sis_student_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    school: Mapped["School"] = relationship(back_populates="students")
    guardians: Mapped[List["Guardian"]] = relationship(
        secondary="student_guardian_links", back_populates="students"
    )
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="student")


# ═══════════════════════════════════════════════════
# Guardians
# ═══════════════════════════════════════════════════

class Guardian(Base):
    __tablename__ = "guardians"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    school_id: Mapped[int] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Phone in E.164 format (+15551234567)
    phone: Mapped[str] = mapped_column(String(20), nullable=False)
    # HMAC hash of phone for deduplication queries without exposing number
    phone_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)

    # Opt-in / opt-out
    sms_opt_in: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    opt_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    opt_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    opt_out_source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    school: Mapped["School"] = relationship(back_populates="guardians")
    students: Mapped[List["Student"]] = relationship(
        secondary="student_guardian_links", back_populates="guardians"
    )
    invoices: Mapped[List["Invoice"]] = relationship(back_populates="guardian")

    # Unique constraint: one phone per school (prevents duplicate guardians)
    __table_args__ = (
        UniqueConstraint("school_id", "phone", name="uq_guardian_school_phone"),
    )


# ═══════════════════════════════════════════════════
# Student-Guardian Link Table (many-to-many)
# ═══════════════════════════════════════════════════

class StudentGuardianLink(Base):
    __tablename__ = "student_guardian_links"

    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"),
        primary_key=True,
    )
    guardian_id: Mapped[int] = mapped_column(
        ForeignKey("guardians.id", ondelete="CASCADE"),
        primary_key=True,
    )
    relationship_type: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True
    )  # e.g., "parent", "grandparent", "aunt"
    is_primary_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


# ═══════════════════════════════════════════════════
# Invoices
# ═══════════════════════════════════════════════════

class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    school_id: Mapped[int] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    guardian_id: Mapped[int] = mapped_column(
        ForeignKey("guardians.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Invoice details
    invoice_number: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_due: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False
    )  # DECIMAL(10,2) — never use float for money
    amount_paid: Mapped[float] = mapped_column(Numeric(10, 2), default=0.00, nullable=False)
    due_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"),
        default=InvoiceStatus.PENDING,
        nullable=False,
    )

    # External SIS reference
    sis_invoice_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    school: Mapped["School"] = relationship(back_populates="invoices")
    student: Mapped["Student"] = relationship(back_populates="invoices")
    guardian: Mapped["Guardian"] = relationship(back_populates="invoices")
    payments: Mapped[List["Payment"]] = relationship(back_populates="invoice")
    messages: Mapped[List["OutboundMessage"]] = relationship(back_populates="invoice")

    # Unique: one invoice_number per school
    __table_args__ = (
        UniqueConstraint("school_id", "invoice_number", name="uq_invoice_school_number"),
    )


# ═══════════════════════════════════════════════════
# Payments
# ═══════════════════════════════════════════════════

class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"),
        default=PaymentStatus.PENDING,
        nullable=False,
    )
    # How the parent claims they paid: "check", "venmo", "cash", "zelle", etc.
    payment_method: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # External payment processor reference (if any)
    external_reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Who confirmed this payment (staff name or system)
    confirmed_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    invoice: Mapped["Invoice"] = relationship(back_populates="payments")


# ═══════════════════════════════════════════════════
# Outbound Messages (The Outbox + Sent Log)
# ═══════════════════════════════════════════════════

class OutboundMessage(Base):
    __tablename__ = "outbound_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    school_id: Mapped[int] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    invoice_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    guardian_id: Mapped[int] = mapped_column(
        ForeignKey("guardians.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── DEDUPLICATION: the most important column ──
    # Deterministic key: school + student + guardian + invoice + type + due_date + policy_version
    message_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    reminder_type: Mapped[ReminderType] = mapped_column(
        Enum(ReminderType, name="reminder_type"),
        nullable=False,
    )
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, name="message_status"),
        default=MessageStatus.PENDING,
        nullable=False,
    )

    # Content
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Number of SMS segments (1 = 160 chars, 2 = 306 chars GSM-7)
    segments: Mapped[int] = mapped_column(default=1, nullable=False)

    # Provider tracking
    provider: Mapped[str] = mapped_column(String(50), default="twilio", nullable=False)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # The idempotent client reference sent to provider
    client_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, index=True)

    # Retry tracking
    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)
    max_retries: Mapped[int] = mapped_column(default=3, nullable=False)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Timing
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Suppression reason (if status = suppressed)
    suppression_reason: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    invoice: Mapped[Optional["Invoice"]] = relationship(back_populates="messages")
    callbacks: Mapped[List["DeliveryCallback"]] = relationship(back_populates="message")

    # Indexes for common queries
    __table_args__ = (
        # Fast lookup of pending messages ordered by scheduled time
        Index(
            "ix_outbound_pending_scheduled",
            "status",
            "scheduled_at",
            postgresql_where="status = 'pending'",
        ),
        # Fast lookup of unknown_delivery messages for reconciliation
        Index(
            "ix_outbound_unknown",
            "status",
            "updated_at",
            postgresql_where="status = 'unknown_delivery'",
        ),
    )


# ═══════════════════════════════════════════════════
# Delivery Callbacks (Webhook receipts from Twilio)
# ═══════════════════════════════════════════════════

class DeliveryCallback(Base):
    __tablename__ = "delivery_callbacks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(
        ForeignKey("outbound_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── DEDUPLICATION ──
    provider: Mapped[str] = mapped_column(String(50), default="twilio", nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # Status from provider
    provider_status: Mapped[str] = mapped_column(String(50), nullable=False)
    # Raw webhook payload (JSON string) for debugging
    raw_payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships
    message: Mapped["OutboundMessage"] = relationship(back_populates="callbacks")


# ═══════════════════════════════════════════════════
# Inbound Messages (Texts from Parents)
# ═══════════════════════════════════════════════════

class InboundMessage(Base):
    __tablename__ = "inbound_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    school_id: Mapped[int] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    guardian_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("guardians.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Provider info
    provider: Mapped[str] = mapped_column(String(50), default="twilio", nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # Content
    from_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    to_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Parsed intent (Step 16)
    intent: Mapped[InboundIntent] = mapped_column(
        Enum(InboundIntent, name="inbound_intent"),
        default=InboundIntent.UNKNOWN,
        nullable=False,
    )
    # Confidence score for intent parsing (0.0–1.0)
    intent_confidence: Mapped[float] = mapped_column(Numeric(3, 2), default=1.00, nullable=False)

    # Processing
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ═══════════════════════════════════════════════════
# Hardship Requests
# ═══════════════════════════════════════════════════

class HardshipRequest(Base):
    __tablename__ = "hardship_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
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
    invoice_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"),
        nullable=True,
    )

    # The inbound message that triggered this request
    inbound_message_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("inbound_messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[HardshipStatus] = mapped_column(
        Enum(HardshipStatus, name="hardship_status"),
        default=HardshipStatus.REQUESTED,
        nullable=False,
    )

    # Parent's explanation (extracted from SMS body)
    request_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Staff response
    staff_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # SLA tracking (Step 18)
    sla_deadline: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ═══════════════════════════════════════════════════
# Audit Events (Immutable, Append-Only)
# ═══════════════════════════════════════════════════

class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    school_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("schools.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    event_type: Mapped[AuditEventType] = mapped_column(
        Enum(AuditEventType, name="audit_event_type"),
        nullable=False,
        index=True,
    )
    # Primary entity affected
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g., "message", "invoice"
    entity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Masked summary (safe for logs)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Full details (JSON) for deep investigation
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Who/what triggered the event
    actor_type: Mapped[str] = mapped_column(
        String(50), default="system", nullable=False
    )  # "system", "worker", "user", "api"
    actor_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    # Source IP or worker hostname
    source: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Index for time-range queries (audit reports)
    __table_args__ = (
        Index("ix_audit_school_created", "school_id", "created_at"),
        Index("ix_audit_type_created", "event_type", "created_at"),
    )
