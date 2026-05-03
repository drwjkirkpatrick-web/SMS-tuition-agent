"""
domain/invoice_service.py — Invoice lifecycle management
═══════════════════════════════════════════════════

Pure business logic for invoices. All DB operations are async and
accept a session parameter (dependency injection pattern).

Key operations:
  - create_invoice()
  - update_invoice_status()  — transitions: pending → partial → paid → overdue
  - get_balance()            — amount_due - amount_paid
  - mark_overdue()           — find all past-due pending invoices

Teaching notes:
  - "Service" in domain-driven design = a stateless object that
    encapsulates business rules. It has no identity of its own.
  - We pass `session: AsyncSession` into every method so the caller
    controls the transaction boundary (important for outbox pattern).
  - Status transitions are validated: you can't go from PAID to PENDING.
═══════════════════════════════════════════════════
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import Guardian, Invoice, InvoiceStatus, Payment, PaymentStatus, Student


class InvalidStatusTransitionError(ValueError):
    """Raised when an invoice status change violates business rules."""
    pass


class InvoiceService:
    """
    Stateless service for invoice operations.
    Instantiate once (or use as a module-level singleton).
    """

    # Valid status transitions (directed graph)
    VALID_TRANSITIONS: dict[InvoiceStatus, set[InvoiceStatus]] = {
        InvoiceStatus.PENDING: {
            InvoiceStatus.PARTIAL,
            InvoiceStatus.PAID,
            InvoiceStatus.OVERDUE,
            InvoiceStatus.CANCELLED,
        },
        InvoiceStatus.PARTIAL: {
            InvoiceStatus.PAID,
            InvoiceStatus.OVERDUE,
            InvoiceStatus.CANCELLED,
        },
        InvoiceStatus.PAID: set(),  # terminal state
        InvoiceStatus.OVERDUE: {
            InvoiceStatus.PARTIAL,
            InvoiceStatus.PAID,
            InvoiceStatus.CANCELLED,
        },
        InvoiceStatus.CANCELLED: set(),  # terminal state
    }

    async def get_invoice(
        self,
        session: AsyncSession,
        invoice_id: int,
    ) -> Optional[Invoice]:
        """Fetch an invoice by ID."""
        result = await session.execute(
            select(Invoice).where(Invoice.id == invoice_id)
        )
        return result.scalar_one_or_none()

    async def create_invoice(
        self,
        session: AsyncSession,
        school_id: int,
        student_id: int,
        guardian_id: int,
        invoice_number: str,
        amount_due: Decimal,
        due_date: date,
        sis_invoice_id: Optional[str] = None,
    ) -> Invoice:
        """
        Create a new pending invoice.
        
        Args:
            amount_due: must be > 0
            due_date: must be today or in the future (configurable)
        """
        if amount_due <= 0:
            raise ValueError("amount_due must be positive")
        if due_date < date.today():
            raise ValueError("due_date cannot be in the past")

        invoice = Invoice(
            school_id=school_id,
            student_id=student_id,
            guardian_id=guardian_id,
            invoice_number=invoice_number,
            amount_due=amount_due,
            amount_paid=Decimal("0.00"),
            due_date=due_date,
            status=InvoiceStatus.PENDING,
            sis_invoice_id=sis_invoice_id,
        )
        session.add(invoice)
        await session.flush()  # get invoice.id without committing
        return invoice

    async def update_status(
        self,
        session: AsyncSession,
        invoice: Invoice,
        new_status: InvoiceStatus,
    ) -> Invoice:
        """
        Transition an invoice to a new status with validation.
        Also recalculates amount_paid from payments.
        """
        current = invoice.status
        if new_status not in self.VALID_TRANSITIONS.get(current, set()):
            raise InvalidStatusTransitionError(
                f"Cannot transition from {current.value} to {new_status.value}"
            )

        invoice.status = new_status
        invoice.updated_at = datetime.utcnow()
        
        # If transitioning to PAID, ensure amount_paid == amount_due
        if new_status == InvoiceStatus.PAID:
            invoice.amount_paid = invoice.amount_due

        await session.flush()
        return invoice

    async def record_payment(
        self,
        session: AsyncSession,
        invoice: Invoice,
        amount: Decimal,
        payment_method: Optional[str] = None,
        external_reference: Optional[str] = None,
    ) -> Payment:
        """
        Record a payment against an invoice and update invoice status.
        Returns the created Payment record.
        """
        if amount <= 0:
            raise ValueError("Payment amount must be positive")

        # Create payment record
        payment = Payment(
            invoice_id=invoice.id,
            amount=amount,
            status=PaymentStatus.CONFIRMED,
            payment_method=payment_method,
            external_reference=external_reference,
            confirmed_by="system",
            confirmed_at=datetime.utcnow(),
        )
        session.add(payment)
        await session.flush()

        # Update invoice
        invoice.amount_paid = Decimal(str(invoice.amount_paid)) + amount
        
        # Determine new status
        if invoice.amount_paid >= invoice.amount_due:
            invoice.status = InvoiceStatus.PAID
            invoice.amount_paid = invoice.amount_due  # cap at amount_due
        elif invoice.amount_paid > 0:
            invoice.status = InvoiceStatus.PARTIAL
        
        invoice.updated_at = datetime.utcnow()
        await session.flush()
        return payment

    async def get_balance(self, invoice: Invoice) -> Decimal:
        """Return remaining balance (may be negative if overpaid)."""
        return Decimal(str(invoice.amount_due)) - Decimal(str(invoice.amount_paid))

    async def find_overdue_invoices(
        self,
        session: AsyncSession,
        school_id: int,
        as_of: Optional[date] = None,
    ) -> list[Invoice]:
        """
        Find all invoices that are past due and not fully paid.
        Used by the scheduler for late notices.
        """
        as_of = as_of or date.today()
        result = await session.execute(
            select(Invoice).where(
                Invoice.school_id == school_id,
                Invoice.due_date < as_of,
                Invoice.status.in_([InvoiceStatus.PENDING, InvoiceStatus.PARTIAL]),
            )
        )
        return list(result.scalars().all())

    async def is_fully_paid(self, invoice: Invoice) -> bool:
        """Check if invoice has zero or negative balance."""
        balance = await self.get_balance(invoice)
        return balance <= 0
