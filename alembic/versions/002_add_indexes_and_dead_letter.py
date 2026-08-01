"""Add dead_letter_messages table and query optimization indexes

Revision ID: 002
Revises: 001
Create Date: 2026-08-01

Improvements (from docs/30-improvements.md):
  - E9: Index optimization for common query patterns
  - R4: Dead letter queue for poison messages
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Dead Letter Messages (R4: Dead Letter Queue) ──
    op.create_table(
        "dead_letter_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "original_message_id",
            sa.BigInteger(),
            sa.ForeignKey("outbound_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "school_id",
            sa.BigInteger(),
            sa.ForeignKey("schools.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "guardian_id",
            sa.BigInteger(),
            sa.ForeignKey("guardians.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("message_key", sa.String(255), nullable=False),
        sa.Column("reminder_type", sa.String(50), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("original_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "dead_lettered_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    # ── E9: Index optimization for common query patterns ──

    # (1) Partial index: outbound_messages WHERE status='pending'
    #     Supports the retry-scan query: retry_count < max_retries
    op.execute(
        """
        CREATE INDEX ix_outbound_pending_retry
        ON outbound_messages (retry_count, max_retries)
        WHERE status = 'pending'
        """
    )

    # (2) Composite index: invoices(school_id, status, due_date)
    #     Supports dashboard + reminder queries filtering by school,
    #     status, and due date range.
    op.execute(
        """
        CREATE INDEX ix_invoice_school_status_due
        ON invoices (school_id, status, due_date)
        """
    )

    # (3) Partial index: inbound_messages WHERE intent='call' AND processed_at IS NULL
    #     Supports the staff callback queue query.
    op.execute(
        """
        CREATE INDEX ix_inbound_call_unprocessed
        ON inbound_messages (school_id, intent, processed_at)
        WHERE intent = 'call' AND processed_at IS NULL
        """
    )


def downgrade() -> None:
    # ── Drop indexes ──
    op.execute("DROP INDEX IF EXISTS ix_inbound_call_unprocessed")
    op.execute("DROP INDEX IF EXISTS ix_invoice_school_status_due")
    op.execute("DROP INDEX IF EXISTS ix_outbound_pending_retry")

    # ── Drop dead letter table ──
    op.drop_table("dead_letter_messages")