"""
domain/reminder_service.py — Reminder eligibility and message key generation
═══════════════════════════════════════════════════

This service decides WHO gets WHAT reminder WHEN.
It does NOT send messages — it only builds the list of intended reminders
and inserts them into the outbox (transactionally, Step 10).

Core logic:
  1. Load all active invoices for a school
  2. For each invoice, determine which reminder types are due today
  3. Check suppression rules (already paid, opted out, max attempts reached)
  4. Build deterministic message_key for deduplication
  5. Return a list of OutboundMessage candidates

Teaching notes:
  - "Eligibility" is pure business logic — no side effects (no DB writes).
    The scheduler (Step 9) calls this, then writes to the outbox.
  - `message_key` is computed deterministically so duplicate runs of
    the scheduler produce identical keys, which the DB rejects.
  - All date math uses the school's timezone (from `school.timezone`).
═══════════════════════════════════════════════════
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from zoneinfo import ZoneInfo

from domain.models import (
    Guardian,
    Invoice,
    InvoiceStatus,
    MessageStatus,
    OutboundMessage,
    ReminderType,
    School,
)


@dataclass(frozen=True)
class ReminderCandidate:
    """
    A single reminder that SHOULD be sent (before suppression checks).
    Frozen = immutable, hashable, safe to use in sets.
    """
    school_id: int
    invoice_id: int
    student_id: int
    guardian_id: int
    reminder_type: ReminderType
    due_date: date
    message_key: str
    body_template: str              # e.g., "Hi {guardian}, reminder for {student}..."


class ReminderService:
    """
    Computes reminder candidates and suppression rules.
    """

    # Default reminder schedule (days before due date)
    DEFAULT_SCHEDULE: dict[ReminderType, int] = {
        ReminderType.DUE_14: 14,
        ReminderType.DUE_3: 3,
        ReminderType.DUE_TODAY: 0,
    }

    def compute_message_key(
        self,
        school_id: int,
        student_id: int,
        guardian_id: int,
        invoice_id: int,
        reminder_type: ReminderType,
        due_date: date,
        policy_version: str = "v1",
    ) -> str:
        """
        Deterministic key for deduplication.
        
        Format: {school}:{student}:{guardian}:{invoice}:{type}:{due_date}:{policy}
        Example: 1:101:201:1001:due_14:2026-05-15:v1
        """
        return (
            f"{school_id}:{student_id}:{guardian_id}:{invoice_id}:"
            f"{reminder_type.value}:{due_date.isoformat()}:{policy_version}"
        )

    def compute_reminder_date(
        self,
        due_date: date,
        reminder_type: ReminderType,
        schedule: Optional[dict[ReminderType, int]] = None,
    ) -> date:
        """
        Given a due date and reminder type, return the calendar date
        when that reminder should be sent.
        """
        sched = schedule or self.DEFAULT_SCHEDULE
        days_before = sched.get(reminder_type, 0)
        return due_date - timedelta(days=days_before)

    def is_reminder_due_today(
        self,
        invoice: Invoice,
        reminder_type: ReminderType,
        today: date,
        schedule: Optional[dict[ReminderType, int]] = None,
    ) -> bool:
        """
        Check if a specific reminder type should trigger today.
        
        Examples:
          - DUE_14 for due_date=May 15 → triggers on May 1
          - DUE_3 for due_date=May 15 → triggers on May 12
          - DUE_TODAY for due_date=May 15 → triggers on May 15
        """
        reminder_date = self.compute_reminder_date(invoice.due_date, reminder_type, schedule)
        return reminder_date == today

    def is_late_notice_eligible(
        self,
        invoice: Invoice,
        today: date,
    ) -> bool:
        """
        Late notices trigger the day AFTER due date if still unpaid.
        """
        if invoice.status in (InvoiceStatus.PAID, InvoiceStatus.CANCELLED):
            return False
        return invoice.due_date < today

    def should_suppress(
        self,
        invoice: Invoice,
        guardian: Guardian,
    ) -> tuple[bool, Optional[str]]:
        """
        Check if a reminder should be suppressed. Returns (suppressed, reason).
        
        Suppression rules:
          1. Invoice fully paid → suppress
          2. Guardian opted out → suppress
          3. Invoice cancelled → suppress
        """
        if invoice.status == InvoiceStatus.PAID:
            return True, "invoice_paid"
        if invoice.status == InvoiceStatus.CANCELLED:
            return True, "invoice_cancelled"
        if not guardian.sms_opt_in:
            return True, "guardian_opted_out"
        return False, None

    def build_candidates(
        self,
        school: School,
        invoices: list[Invoice],
        today: Optional[date] = None,
        schedule: Optional[dict[ReminderType, int]] = None,
        policy_version: str = "v1",
    ) -> list[ReminderCandidate]:
        """
        Build all reminder candidates for a school on a given day.
        
        This is the main entry point called by the daily scheduler.
        Returns only candidates that are due today (not suppressed yet).
        """
        today = today or date.today()
        candidates: list[ReminderCandidate] = []

        for invoice in invoices:
            # Skip terminal states
            if invoice.status in (InvoiceStatus.PAID, InvoiceStatus.CANCELLED):
                continue

            # Determine which reminder types are due today
            due_types: list[ReminderType] = []
            for rtype in (ReminderType.DUE_14, ReminderType.DUE_3, ReminderType.DUE_TODAY):
                if self.is_reminder_due_today(invoice, rtype, today, schedule):
                    due_types.append(rtype)

            # Late notice (separate logic)
            if self.is_late_notice_eligible(invoice, today):
                due_types.append(ReminderType.LATE_NOTICE)

            for rtype in due_types:
                key = self.compute_message_key(
                    school_id=school.id,
                    student_id=invoice.student_id,
                    guardian_id=invoice.guardian_id,
                    invoice_id=invoice.id,
                    reminder_type=rtype,
                    due_date=invoice.due_date,
                    policy_version=policy_version,
                )
                candidates.append(ReminderCandidate(
                    school_id=school.id,
                    invoice_id=invoice.id,
                    student_id=invoice.student_id,
                    guardian_id=invoice.guardian_id,
                    reminder_type=rtype,
                    due_date=invoice.due_date,
                    message_key=key,
                    body_template=self._get_template_name(rtype),
                ))

        return candidates

    def _get_template_name(self, reminder_type: ReminderType) -> str:
        """Map reminder type to template name (Step 15)."""
        template_map = {
            ReminderType.DUE_14: "reminder_due_14",
            ReminderType.DUE_3: "reminder_due_3",
            ReminderType.DUE_TODAY: "reminder_due_today",
            ReminderType.LATE_NOTICE: "reminder_late",
            ReminderType.PAYMENT_CONFIRMED: "payment_confirmed",
            ReminderType.CALLBACK_ACK: "callback_ack",
            ReminderType.HARDSHIP_ACK: "hardship_ack",
        }
        return template_map.get(reminder_type, "generic")
