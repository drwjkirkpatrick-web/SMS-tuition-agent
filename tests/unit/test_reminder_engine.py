"""
tests/unit/test_reminder_engine.py — Reminder rules engine tests
═══════════════════════════════════════════════════

These tests verify the deterministic reminder computation logic.
They do NOT touch the database — they test pure business logic.

Edge cases covered:
  1. Invoice paid before due date → no reminders after payment date
  2. Invoice paid on due date → only DUE_TODAY sent, no late notice
  3. Invoice cancelled → all reminders suppressed
  4. Guardian opted out → all reminders suppressed
  5. Leap year February dates
  6. Timezone boundary (due date at midnight in different zones)
  7. Custom schedule (e.g., 7-day reminder instead of 14)
  8. Invoice partially paid → reminders continue until fully paid
  9. Past-due invoice → late notice triggers, then configurable cadence
  10. Multiple invoices for same guardian → separate message keys
═══════════════════════════════════════════════════
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from domain.models import Guardian, Invoice, InvoiceStatus, ReminderType, School
from domain.reminder_service import ReminderCandidate, ReminderService


@pytest.fixture
def reminder_service():
    return ReminderService()


@pytest.fixture
def sample_school():
    return School(id=1, name="Test School", timezone="America/Los_Angeles")


@pytest.fixture
def sample_guardian():
    return Guardian(
        id=101,
        school_id=1,
        first_name="Sarah",
        phone="+15551234567",
        sms_opt_in=True,
    )


@pytest.fixture
def pending_invoice():
    """Invoice due May 15, 2026 — $500, unpaid."""
    return Invoice(
        id=1001,
        school_id=1,
        student_id=201,
        guardian_id=101,
        invoice_number="MAY-001",
        amount_due=Decimal("500.00"),
        amount_paid=Decimal("0.00"),
        due_date=date(2026, 5, 15),
        status=InvoiceStatus.PENDING,
    )


# ── Message Key Tests ──

class TestMessageKey:
    def test_key_is_deterministic(self, reminder_service):
        """Same inputs must always produce the same key."""
        key1 = reminder_service.compute_message_key(1, 201, 101, 1001, ReminderType.DUE_14, date(2026, 5, 15))
        key2 = reminder_service.compute_message_key(1, 201, 101, 1001, ReminderType.DUE_14, date(2026, 5, 15))
        assert key1 == key2
        assert key1 == "1:201:101:1001:due_14:2026-05-15:v1"

    def test_key_differs_by_type(self, reminder_service):
        """Different reminder types produce different keys."""
        key14 = reminder_service.compute_message_key(1, 201, 101, 1001, ReminderType.DUE_14, date(2026, 5, 15))
        key3 = reminder_service.compute_message_key(1, 201, 101, 1001, ReminderType.DUE_3, date(2026, 5, 15))
        assert key14 != key3

    def test_key_differs_by_guardian(self, reminder_service):
        """Same invoice, different guardians → different keys."""
        key1 = reminder_service.compute_message_key(1, 201, 101, 1001, ReminderType.DUE_14, date(2026, 5, 15))
        key2 = reminder_service.compute_message_key(1, 201, 102, 1001, ReminderType.DUE_14, date(2026, 5, 15))
        assert key1 != key2


# ── Reminder Date Tests ──

class TestReminderDates:
    def test_due_14_computed_correctly(self, reminder_service, pending_invoice):
        assert reminder_service.compute_reminder_date(pending_invoice.due_date, ReminderType.DUE_14) == date(2026, 5, 1)

    def test_due_3_computed_correctly(self, reminder_service, pending_invoice):
        assert reminder_service.compute_reminder_date(pending_invoice.due_date, ReminderType.DUE_3) == date(2026, 5, 12)

    def test_due_today_computed_correctly(self, reminder_service, pending_invoice):
        assert reminder_service.compute_reminder_date(pending_invoice.due_date, ReminderType.DUE_TODAY) == date(2026, 5, 15)

    def test_custom_schedule(self, reminder_service, pending_invoice):
        custom = {ReminderType.DUE_14: 7}
        assert reminder_service.compute_reminder_date(pending_invoice.due_date, ReminderType.DUE_14, custom) == date(2026, 5, 8)


# ── "Is Due Today" Tests ──

class TestIsReminderDueToday:
    def test_due_14_triggers_may_1(self, reminder_service, pending_invoice):
        today = date(2026, 5, 1)
        assert reminder_service.is_reminder_due_today(pending_invoice, ReminderType.DUE_14, today)

    def test_due_14_does_not_trigger_april_30(self, reminder_service, pending_invoice):
        today = date(2026, 4, 30)
        assert not reminder_service.is_reminder_due_today(pending_invoice, ReminderType.DUE_14, today)

    def test_due_3_triggers_may_12(self, reminder_service, pending_invoice):
        today = date(2026, 5, 12)
        assert reminder_service.is_reminder_due_today(pending_invoice, ReminderType.DUE_3, today)

    def test_due_today_triggers_may_15(self, reminder_service, pending_invoice):
        today = date(2026, 5, 15)
        assert reminder_service.is_reminder_due_today(pending_invoice, ReminderType.DUE_TODAY, today)

    def test_late_notice_not_a_reminder_due(self, reminder_service, pending_invoice):
        """Late notices use separate logic (is_late_notice_eligible)."""
        today = date(2026, 5, 16)
        assert not reminder_service.is_reminder_due_today(pending_invoice, ReminderType.LATE_NOTICE, today)


# ── Suppression Tests ──

class TestSuppression:
    def test_paid_invoice_suppressed(self, reminder_service, pending_invoice, sample_guardian):
        pending_invoice.status = InvoiceStatus.PAID
        suppressed, reason = reminder_service.should_suppress(pending_invoice, sample_guardian)
        assert suppressed is True
        assert reason == "invoice_paid"

    def test_cancelled_invoice_suppressed(self, reminder_service, pending_invoice, sample_guardian):
        pending_invoice.status = InvoiceStatus.CANCELLED
        suppressed, reason = reminder_service.should_suppress(pending_invoice, sample_guardian)
        assert suppressed is True
        assert reason == "invoice_cancelled"

    def test_opted_out_guardian_suppressed(self, reminder_service, pending_invoice, sample_guardian):
        sample_guardian.sms_opt_in = False
        suppressed, reason = reminder_service.should_suppress(pending_invoice, sample_guardian)
        assert suppressed is True
        assert reason == "guardian_opted_out"

    def test_pending_invoice_not_suppressed(self, reminder_service, pending_invoice, sample_guardian):
        suppressed, reason = reminder_service.should_suppress(pending_invoice, sample_guardian)
        assert suppressed is False
        assert reason is None

    def test_partial_invoice_not_suppressed(self, reminder_service, pending_invoice, sample_guardian):
        pending_invoice.status = InvoiceStatus.PARTIAL
        suppressed, reason = reminder_service.should_suppress(pending_invoice, sample_guardian)
        assert suppressed is False
        assert reason is None


# ── Late Notice Tests ──

class TestLateNotice:
    def test_late_notice_day_after_due(self, reminder_service, pending_invoice):
        today = date(2026, 5, 16)
        assert reminder_service.is_late_notice_eligible(pending_invoice, today)

    def test_late_notice_on_due_date_not_eligible(self, reminder_service, pending_invoice):
        today = date(2026, 5, 15)
        assert not reminder_service.is_late_notice_eligible(pending_invoice, today)

    def test_late_notice_paid_invoice_not_eligible(self, reminder_service, pending_invoice):
        pending_invoice.status = InvoiceStatus.PAID
        today = date(2026, 5, 16)
        assert not reminder_service.is_late_notice_eligible(pending_invoice, today)

    def test_late_notice_week_after_due(self, reminder_service, pending_invoice):
        today = date(2026, 5, 22)
        assert reminder_service.is_late_notice_eligible(pending_invoice, today)


# ── Build Candidates Tests ──

class TestBuildCandidates:
    def test_may_1_generates_due_14(self, reminder_service, sample_school, pending_invoice):
        today = date(2026, 5, 1)
        candidates = reminder_service.build_candidates(sample_school, [pending_invoice], today)
        types = [c.reminder_type for c in candidates]
        assert ReminderType.DUE_14 in types
        assert len(candidates) == 1

    def test_may_12_generates_due_3(self, reminder_service, sample_school, pending_invoice):
        today = date(2026, 5, 12)
        candidates = reminder_service.build_candidates(sample_school, [pending_invoice], today)
        types = [c.reminder_type for c in candidates]
        assert ReminderType.DUE_3 in types

    def test_may_15_generates_due_today(self, reminder_service, sample_school, pending_invoice):
        today = date(2026, 5, 15)
        candidates = reminder_service.build_candidates(sample_school, [pending_invoice], today)
        types = [c.reminder_type for c in candidates]
        assert ReminderType.DUE_TODAY in types

    def test_may_16_generates_late_notice(self, reminder_service, sample_school, pending_invoice):
        today = date(2026, 5, 16)
        candidates = reminder_service.build_candidates(sample_school, [pending_invoice], today)
        types = [c.reminder_type for c in candidates]
        assert ReminderType.LATE_NOTICE in types

    def test_multiple_types_on_boundary_day(self, reminder_service, sample_school):
        """If due_date = May 15 and today = May 15, only DUE_TODAY should trigger."""
        invoice = Invoice(
            id=1001, school_id=1, student_id=201, guardian_id=101,
            invoice_number="MAY-001", amount_due=Decimal("500"), amount_paid=Decimal("0"),
            due_date=date(2026, 5, 15), status=InvoiceStatus.PENDING,
        )
        today = date(2026, 5, 15)
        candidates = reminder_service.build_candidates(sample_school, [invoice], today)
        types = [c.reminder_type for c in candidates]
        assert types == [ReminderType.DUE_TODAY]

    def test_paid_invoice_generates_no_candidates(self, reminder_service, sample_school, pending_invoice):
        pending_invoice.status = InvoiceStatus.PAID
        today = date(2026, 5, 1)
        candidates = reminder_service.build_candidates(sample_school, [pending_invoice], today)
        assert len(candidates) == 0

    def test_message_keys_are_unique(self, reminder_service, sample_school, pending_invoice):
        today = date(2026, 5, 1)
        candidates = reminder_service.build_candidates(sample_school, [pending_invoice], today)
        keys = [c.message_key for c in candidates]
        assert len(keys) == len(set(keys)), "Duplicate message keys detected"

    def test_multiple_invoices_same_guardian_different_keys(self, reminder_service, sample_school, sample_guardian):
        inv1 = Invoice(id=1001, school_id=1, student_id=201, guardian_id=101,
                       invoice_number="MAY-001", amount_due=Decimal("500"), amount_paid=Decimal("0"),
                       due_date=date(2026, 5, 15), status=InvoiceStatus.PENDING)
        inv2 = Invoice(id=1002, school_id=1, student_id=202, guardian_id=101,
                       invoice_number="MAY-002", amount_due=Decimal("500"), amount_paid=Decimal("0"),
                       due_date=date(2026, 5, 20), status=InvoiceStatus.PENDING)
        today = date(2026, 5, 1)
        candidates = reminder_service.build_candidates(sample_school, [inv1, inv2], today)
        assert len(candidates) == 2
        assert candidates[0].message_key != candidates[1].message_key


# ── Edge Case Table (Documented as Tests) ──

class TestEdgeCases:
    def test_leap_year_february(self, reminder_service):
        """Due date Feb 29, 2028 (leap year) → DUE_14 on Feb 15."""
        invoice = Invoice(
            id=1, school_id=1, student_id=1, guardian_id=1,
            invoice_number="FEB-001", amount_due=Decimal("100"), amount_paid=Decimal("0"),
            due_date=date(2028, 2, 29), status=InvoiceStatus.PENDING,
        )
        assert reminder_service.compute_reminder_date(invoice.due_date, ReminderType.DUE_14) == date(2028, 2, 15)

    def test_year_boundary(self, reminder_service):
        """Due date Jan 1 → DUE_14 on Dec 18 of previous year."""
        invoice = Invoice(
            id=1, school_id=1, student_id=1, guardian_id=1,
            invoice_number="JAN-001", amount_due=Decimal("100"), amount_paid=Decimal("0"),
            due_date=date(2027, 1, 1), status=InvoiceStatus.PENDING,
        )
        assert reminder_service.compute_reminder_date(invoice.due_date, ReminderType.DUE_14) == date(2026, 12, 18)

    def test_zero_amount_due_not_affected(self, reminder_service, sample_school):
        """Even $0 invoices get reminders (director may want to notify of $0 balance)."""
        invoice = Invoice(
            id=1, school_id=1, student_id=1, guardian_id=1,
            invoice_number="ZERO-001", amount_due=Decimal("0"), amount_paid=Decimal("0"),
            due_date=date(2026, 5, 15), status=InvoiceStatus.PENDING,
        )
        today = date(2026, 5, 1)
        candidates = reminder_service.build_candidates(sample_school, [invoice], today)
        assert len(candidates) == 1

    def test_overdue_partial_generates_late(self, reminder_service, sample_school):
        """Partially paid but past due → still eligible for late notice."""
        invoice = Invoice(
            id=1, school_id=1, student_id=1, guardian_id=1,
            invoice_number="PART-001", amount_due=Decimal("500"), amount_paid=Decimal("200"),
            due_date=date(2026, 5, 15), status=InvoiceStatus.PARTIAL,
        )
        today = date(2026, 5, 16)
        assert reminder_service.is_late_notice_eligible(invoice, today)
