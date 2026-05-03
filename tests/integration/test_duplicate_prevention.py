"""
tests/integration/test_duplicate_prevention.py — Concurrency tests
═══════════════════════════════════════════════════

These tests verify duplicate prevention under concurrent load.
They require a real PostgreSQL database (use test container).

Scenarios:
  1. Multiple workers race for the same pending message
  2. Scheduler run twice → no duplicate rows
  3. Worker killed mid-send → reconciliation resolves
  4. ON CONFLICT DO NOTHING silently ignores duplicate inserts
═══════════════════════════════════════════════════
"""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.mock_adapter import MockAdapter
from domain.models import (
    Guardian,
    Invoice,
    InvoiceStatus,
    MessageStatus,
    OutboundMessage,
    ReminderType,
    School,
    Student,
)
from domain.outbox import OutboxService
from domain.reminder_service import ReminderService
from domain.dispatch_service import DispatchService
from infra.database import async_session_factory


@pytest.fixture
def mock_adapter():
    return MockAdapter(success_rate=1.0)


@pytest.fixture
async def seed_school_and_invoice(db_session: AsyncSession):
    """Create a school, student, guardian, and pending invoice."""
    school = School(name="Test School", timezone="America/Los_Angeles")
    db_session.add(school)
    await db_session.flush()

    student = Student(
        school_id=school.id,
        first_name="Emma",
        sis_student_id="STU-001",
    )
    db_session.add(student)
    await db_session.flush()

    guardian = Guardian(
        school_id=school.id,
        first_name="Sarah",
        phone="+15551234567",
        sms_opt_in=True,
    )
    db_session.add(guardian)
    await db_session.flush()

    invoice = Invoice(
        school_id=school.id,
        student_id=student.id,
        guardian_id=guardian.id,
        invoice_number="MAY-001",
        amount_due=Decimal("500.00"),
        amount_paid=Decimal("0.00"),
        due_date=date(2026, 5, 15),
        status=InvoiceStatus.PENDING,
    )
    db_session.add(invoice)
    await db_session.commit()

    return {"school": school, "student": student, "guardian": guardian, "invoice": invoice}


class TestSchedulerIdempotency:
    """Verify scheduler produces no duplicates when run twice."""

    async def test_duplicate_scheduler_run_no_duplicates(
        self, db_session, seed_school_and_invoice
    ):
        """Run scheduler twice on same day → only one outbox row."""
        data = seed_school_and_invoice
        school = data["school"]
        invoice = data["invoice"]
        guardian = data["guardian"]

        reminder = ReminderService()
        dispatch = DispatchService()

        today = date(2026, 5, 1)
        candidates = reminder.build_candidates(school, [invoice], today=today)
        assert len(candidates) == 1

        # First run
        result1 = await dispatch.insert_outbox_messages(db_session, candidates)
        await db_session.commit()

        # Second run (same candidates, same keys)
        result2 = await dispatch.insert_outbox_messages(db_session, candidates)
        await db_session.commit()

        # Query outbox
        result = await db_session.execute(
            select(OutboundMessage).where(
                OutboundMessage.message_key == candidates[0].message_key
            )
        )
        messages = result.scalars().all()
        assert len(messages) == 1, f"Expected 1 message, found {len(messages)}"

    async def test_different_reminder_types_different_keys(
        self, db_session, seed_school_and_invoice
    ):
        """DUE_14 and DUE_3 for same invoice have different keys."""
        data = seed_school_and_invoice
        school = data["school"]
        invoice = data["invoice"]

        reminder = ReminderService()
        dispatch = DispatchService()

        may_1 = date(2026, 5, 1)
        may_12 = date(2026, 5, 12)

        c1 = reminder.build_candidates(school, [invoice], today=may_1)
        c2 = reminder.build_candidates(school, [invoice], today=may_12)

        await dispatch.insert_outbox_messages(db_session, c1 + c2)
        await db_session.commit()

        result = await db_session.execute(select(OutboundMessage))
        messages = result.scalars().all()
        assert len(messages) == 2
        assert messages[0].message_key != messages[1].message_key


class TestWorkerRowLocking:
    """Verify two workers cannot claim the same message."""

    async def test_skip_locked_prevents_double_claim(
        self, db_session, seed_school_and_invoice
    ):
        """Two sessions polling with SKIP LOCKED get disjoint message sets."""
        data = seed_school_and_invoice
        school = data["school"]
        invoice = data["invoice"]

        reminder = ReminderService()
        dispatch = DispatchService()
        outbox = OutboxService()

        # Create 3 pending messages
        candidates = []
        for i in range(3):
            c = reminder.compute_message_key(
                school.id, invoice.student_id, invoice.guardian_id,
                invoice.id, ReminderType.DUE_14, date(2026, 5, 15),
                policy_version=f"v{i}",  # different policy version = different key
            )
            candidates.append(reminder.ReminderCandidate(
                school_id=school.id, invoice_id=invoice.id,
                student_id=invoice.student_id, guardian_id=invoice.guardian_id,
                reminder_type=ReminderType.DUE_14, due_date=date(2026, 5, 15),
                message_key=c, body_template="test",
            ))

        await dispatch.insert_outbox_messages(db_session, candidates)
        await db_session.commit()

        # Poll in first session
        messages_1 = await outbox.poll_pending(db_session, batch_size=10)
        assert len(messages_1) == 3

        # Open second session and poll
        async with async_session_factory() as session2:
            messages_2 = await outbox.poll_pending(session2, batch_size=10)
            # With FOR UPDATE SKIP LOCKED, session2 sees 0 because
            # session1 hasn't released locks (transaction still open)
            # In this test, db_session is still in transaction
            assert len(messages_2) == 0

    async def test_claim_changes_status(
        self, db_session, seed_school_and_invoice
    ):
        """Claiming a message changes status from PENDING to SENDING."""
        data = seed_school_and_invoice
        school = data["school"]
        invoice = data["invoice"]

        reminder = ReminderService()
        dispatch = DispatchService()
        outbox = OutboxService()

        c = reminder.ReminderCandidate(
            school_id=school.id, invoice_id=invoice.id,
            student_id=invoice.student_id, guardian_id=invoice.guardian_id,
            reminder_type=ReminderType.DUE_14, due_date=date(2026, 5, 15),
            message_key="test-key-123", body_template="test",
        )
        await dispatch.insert_outbox_messages(db_session, [c])
        await db_session.commit()

        # Re-query (needed because commit invalidated the session)
        result = await db_session.execute(
            select(OutboundMessage).where(OutboundMessage.message_key == "test-key-123")
        )
        msg = result.scalar_one()
        assert msg.status == MessageStatus.PENDING

        # Claim
        claimed = await outbox.claim_for_sending(db_session, msg)
        assert claimed is True
        assert msg.status == MessageStatus.SENDING


class TestStateMachine:
    """Verify strict state transitions prevent invalid moves."""

    async def test_sent_cannot_retry(
        self, db_session, seed_school_and_invoice
    ):
        """Once sent, message cannot transition back to pending."""
        data = seed_school_and_invoice
        school = data["school"]
        invoice = data["invoice"]

        reminder = ReminderService()
        dispatch = DispatchService()
        outbox = OutboxService()

        c = reminder.ReminderCandidate(
            school_id=school.id, invoice_id=invoice.id,
            student_id=invoice.student_id, guardian_id=invoice.guardian_id,
            reminder_type=ReminderType.DUE_14, due_date=date(2026, 5, 15),
            message_key="sm-test-1", body_template="test",
        )
        await dispatch.insert_outbox_messages(db_session, [c])
        await db_session.commit()

        result = await db_session.execute(
            select(OutboundMessage).where(OutboundMessage.message_key == "sm-test-1")
        )
        msg = result.scalar_one()

        # Move to SENDING then SENT
        await outbox.claim_for_sending(db_session, msg)
        await outbox.transition_status(db_session, msg, MessageStatus.SENT, provider_message_id="SID123")
        assert msg.status == MessageStatus.SENT

        # Attempt illegal transition: SENT → PENDING
        with pytest.raises(ValueError):
            await outbox.transition_status(db_session, msg, MessageStatus.PENDING)

    async def test_failed_can_retry(
        self, db_session, seed_school_and_invoice
    ):
        """Failed message can transition back to pending for retry."""
        data = seed_school_and_invoice
        school = data["school"]
        invoice = data["invoice"]

        reminder = ReminderService()
        dispatch = DispatchService()
        outbox = OutboxService()

        c = reminder.ReminderCandidate(
            school_id=school.id, invoice_id=invoice.id,
            student_id=invoice.student_id, guardian_id=invoice.guardian_id,
            reminder_type=ReminderType.DUE_14, due_date=date(2026, 5, 15),
            message_key="sm-test-2", body_template="test",
        )
        await dispatch.insert_outbox_messages(db_session, [c])
        await db_session.commit()

        result = await db_session.execute(
            select(OutboundMessage).where(OutboundMessage.message_key == "sm-test-2")
        )
        msg = result.scalar_one()

        await outbox.claim_for_sending(db_session, msg)
        await outbox.transition_status(db_session, msg, MessageStatus.FAILED)
        assert msg.status == MessageStatus.FAILED
        assert msg.retry_count == 0

        # Retry: FAILED → PENDING
        await outbox.transition_status(db_session, msg, MessageStatus.PENDING)
        assert msg.status == MessageStatus.PENDING
        assert msg.retry_count == 1
        assert msg.next_retry_at is not None


class TestWebhookDedupe:
    """Verify delivery callbacks are deduplicated."""

    async def test_duplicate_callback_ignored(
        self, db_session, seed_school_and_invoice
    ):
        """Same provider_event_id inserted twice → only one row."""
        from domain.models import DeliveryCallback
        from domain.reconciliation_service import ReconciliationService

        data = seed_school_and_invoice
        school = data["school"]
        invoice = data["invoice"]

        reminder = ReminderService()
        dispatch = DispatchService()
        recon = ReconciliationService()

        c = reminder.ReminderCandidate(
            school_id=school.id, invoice_id=invoice.id,
            student_id=invoice.student_id, guardian_id=invoice.guardian_id,
            reminder_type=ReminderType.DUE_14, due_date=date(2026, 5, 15),
            message_key="sm-test-3", body_template="test",
        )
        await dispatch.insert_outbox_messages(db_session, [c])
        await db_session.commit()

        result = await db_session.execute(
            select(OutboundMessage).where(OutboundMessage.message_key == "sm-test-3")
        )
        msg = result.scalar_one()
        msg.status = MessageStatus.SENDING
        await db_session.commit()

        # First callback
        await recon.process_delivery_callback(
            db_session, msg, "evt-123", "delivered", '{"raw": true}'
        )
        await db_session.commit()

        # Second callback (same event ID)
        await recon.process_delivery_callback(
            db_session, msg, "evt-123", "delivered", '{"raw": true}'
        )
        await db_session.commit()

        # Query callbacks
        result = await db_session.execute(
            select(DeliveryCallback).where(DeliveryCallback.provider_event_id == "evt-123")
        )
        callbacks = result.scalars().all()
        assert len(callbacks) == 1
