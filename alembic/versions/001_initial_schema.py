"""Initial schema: schools, students, guardians, invoices, payments, messages, callbacks, audit

Revision ID: 001
Revises: 
Create Date: 2026-05-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Enums ──
    op.execute("CREATE TYPE invoice_status AS ENUM ('pending', 'partial', 'paid', 'overdue', 'cancelled')")
    op.execute("CREATE TYPE payment_status AS ENUM ('pending', 'confirmed', 'reversed')")
    op.execute("CREATE TYPE message_status AS ENUM ('pending', 'sending', 'sent', 'delivered', 'failed', 'unknown_delivery', 'suppressed')")
    op.execute("CREATE TYPE reminder_type AS ENUM ('due_14', 'due_3', 'due_today', 'late_notice', 'payment_confirmed', 'callback_ack', 'hardship_ack')")
    op.execute("CREATE TYPE hardship_status AS ENUM ('requested', 'under_review', 'approved', 'denied', 'resolved')")
    op.execute("CREATE TYPE inbound_intent AS ENUM ('status', 'paid', 'call', 'extension', 'help', 'stop', 'start', 'unknown')")
    op.execute("CREATE TYPE audit_event_type AS ENUM ('message.send_attempt', 'message.delivered', 'message.failed', 'reminder.suppressed', 'policy.changed', 'guardian.opt_out', 'sis.sync', 'payment.reconciled', 'hardship.requested', 'login.failure')")

    # ── Schools ──
    op.create_table(
        "schools",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="America/Los_Angeles"),
        sa.Column("sis_adapter_type", sa.String(64), nullable=True),
        sa.Column("sis_config", sa.Text(), nullable=True),
        sa.Column("reminder_policy", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── Students ──
    op.create_table(
        "students",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("school_id", sa.BigInteger(), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("sis_student_id", sa.String(64), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── Guardians ──
    op.create_table(
        "guardians",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("school_id", sa.BigInteger(), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("first_name", sa.String(100), nullable=False),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("phone_hash", sa.String(64), nullable=True, index=True),
        sa.Column("sms_opt_in", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("opt_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out_source", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("school_id", "phone", name="uq_guardian_school_phone"),
    )

    # ── Student-Guardian Links ──
    op.create_table(
        "student_guardian_links",
        sa.Column("student_id", sa.BigInteger(), sa.ForeignKey("students.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("guardian_id", sa.BigInteger(), sa.ForeignKey("guardians.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("relationship_type", sa.String(50), nullable=True),
        sa.Column("is_primary_contact", sa.Boolean(), nullable=False, server_default="false"),
    )

    # ── Invoices ──
    op.create_table(
        "invoices",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("school_id", sa.BigInteger(), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("student_id", sa.BigInteger(), sa.ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("guardian_id", sa.BigInteger(), sa.ForeignKey("guardians.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("invoice_number", sa.String(64), nullable=False),
        sa.Column("amount_due", sa.Numeric(10, 2), nullable=False),
        sa.Column("amount_paid", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("due_date", sa.Date(), nullable=False, index=True),
        sa.Column("status", sa.Enum("invoice_status", name="invoice_status"), nullable=False, server_default="pending"),
        sa.Column("sis_invoice_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("school_id", "invoice_number", name="uq_invoice_school_number"),
    )

    # ── Payments ──
    op.create_table(
        "payments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("invoice_id", sa.BigInteger(), sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.Enum("payment_status", name="payment_status"), nullable=False, server_default="pending"),
        sa.Column("payment_method", sa.String(50), nullable=True),
        sa.Column("external_reference", sa.String(255), nullable=True),
        sa.Column("confirmed_by", sa.String(100), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # ── Outbound Messages (Outbox + Sent Log) ──
    op.create_table(
        "outbound_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("school_id", sa.BigInteger(), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("invoice_id", sa.BigInteger(), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("guardian_id", sa.BigInteger(), sa.ForeignKey("guardians.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("message_key", sa.String(255), nullable=False, unique=True),
        sa.Column("reminder_type", sa.Enum("reminder_type", name="reminder_type"), nullable=False),
        sa.Column("status", sa.Enum("message_status", name="message_status"), nullable=False, server_default="pending"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("segments", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider", sa.String(50), nullable=False, server_default="twilio"),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("client_message_id", sa.String(255), nullable=True, index=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suppression_reason", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # Partial indexes for common queries
    op.execute("""
        CREATE INDEX ix_outbound_pending_scheduled
        ON outbound_messages (status, scheduled_at)
        WHERE status = 'pending'
    """)
    op.execute("""
        CREATE INDEX ix_outbound_unknown
        ON outbound_messages (status, updated_at)
        WHERE status = 'unknown_delivery'
    """)

    # ── Delivery Callbacks ──
    op.create_table(
        "delivery_callbacks",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), sa.ForeignKey("outbound_messages.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("provider", sa.String(50), nullable=False, server_default="twilio"),
        sa.Column("provider_event_id", sa.String(255), nullable=False, unique=True),
        sa.Column("provider_status", sa.String(50), nullable=False),
        sa.Column("raw_payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # ── Inbound Messages ──
    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("school_id", sa.BigInteger(), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("guardian_id", sa.BigInteger(), sa.ForeignKey("guardians.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("provider", sa.String(50), nullable=False, server_default="twilio"),
        sa.Column("provider_message_id", sa.String(255), nullable=False, unique=True),
        sa.Column("from_phone", sa.String(20), nullable=False),
        sa.Column("to_phone", sa.String(20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("intent", sa.Enum("inbound_intent", name="inbound_intent"), nullable=False, server_default="unknown"),
        sa.Column("intent_confidence", sa.Numeric(3, 2), nullable=False, server_default="1.00"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_task_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # ── Hardship Requests ──
    op.create_table(
        "hardship_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("school_id", sa.BigInteger(), sa.ForeignKey("schools.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("guardian_id", sa.BigInteger(), sa.ForeignKey("guardians.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("invoice_id", sa.BigInteger(), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("inbound_message_id", sa.BigInteger(), sa.ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.Enum("hardship_status", name="hardship_status"), nullable=False, server_default="requested"),
        sa.Column("request_body", sa.Text(), nullable=True),
        sa.Column("staff_notes", sa.Text(), nullable=True),
        sa.Column("assigned_to", sa.String(100), nullable=True),
        sa.Column("sla_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # ── Audit Events ──
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("school_id", sa.BigInteger(), sa.ForeignKey("schools.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("event_type", sa.Enum("audit_event_type", name="audit_event_type"), nullable=False, index=True),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("actor_type", sa.String(50), nullable=False, server_default="system"),
        sa.Column("actor_id", sa.String(100), nullable=True),
        sa.Column("source", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.execute("CREATE INDEX ix_audit_school_created ON audit_events (school_id, created_at)")
    op.execute("CREATE INDEX ix_audit_type_created ON audit_events (event_type, created_at)")


def downgrade() -> None:
    # Drop in reverse order (respect foreign keys)
    op.drop_table("audit_events")
    op.drop_table("hardship_requests")
    op.drop_table("inbound_messages")
    op.drop_table("delivery_callbacks")
    op.drop_table("outbound_messages")
    op.drop_table("payments")
    op.drop_table("invoices")
    op.drop_table("student_guardian_links")
    op.drop_table("guardians")
    op.drop_table("students")
    op.drop_table("schools")

    # Drop enums
    op.execute("DROP TYPE IF EXISTS audit_event_type")
    op.execute("DROP TYPE IF EXISTS inbound_intent")
    op.execute("DROP TYPE IF EXISTS hardship_status")
    op.execute("DROP TYPE IF EXISTS reminder_type")
    op.execute("DROP TYPE IF EXISTS message_status")
    op.execute("DROP TYPE IF EXISTS payment_status")
    op.execute("DROP TYPE IF EXISTS invoice_status")
