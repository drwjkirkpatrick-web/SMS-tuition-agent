"""
domain/hardship_service.py — Hardship request intake and routing
═══════════════════════════════════════════════════

Handles:
  - Create hardship request from inbound SMS
  - Update status (under_review, approved, denied, resolved)
  - SLA tracking (deadline = created_at + 24 hours)

Teaching notes:
  - Hardship requests are staff-facing tickets, not automated.
  - The school director or office staff must manually review and respond.
  - SLA tracking lets us alert staff if a request has been open too long.
═══════════════════════════════════════════════════
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import Guardian, HardshipRequest, HardshipStatus, InboundMessage, Invoice


class HardshipService:
    """
    Manages hardship request lifecycle.
    """

    SLA_HOURS: int = 24  # default response time

    async def create_request(
        self,
        session: AsyncSession,
        school_id: int,
        guardian_id: int,
        inbound_message_id: Optional[int] = None,
        invoice_id: Optional[int] = None,
        request_body: Optional[str] = None,
    ) -> HardshipRequest:
        """
        Create a new hardship request from an inbound SMS.
        Sets SLA deadline and queues for staff review.
        """
        now = datetime.utcnow()
        sla_deadline = now + timedelta(hours=self.SLA_HOURS)

        request = HardshipRequest(
            school_id=school_id,
            guardian_id=guardian_id,
            invoice_id=invoice_id,
            inbound_message_id=inbound_message_id,
            status=HardshipStatus.REQUESTED,
            request_body=request_body,
            sla_deadline=sla_deadline,
            created_at=now,
            updated_at=now,
        )
        session.add(request)
        await session.flush()
        return request

    async def update_status(
        self,
        session: AsyncSession,
        request: HardshipRequest,
        new_status: HardshipStatus,
        staff_notes: Optional[str] = None,
        assigned_to: Optional[str] = None,
    ) -> HardshipRequest:
        """
        Update hardship status. Only valid transitions allowed.
        """
        valid = {
            HardshipStatus.REQUESTED: {HardshipStatus.UNDER_REVIEW, HardshipStatus.APPROVED, HardshipStatus.DENIED},
            HardshipStatus.UNDER_REVIEW: {HardshipStatus.APPROVED, HardshipStatus.DENIED},
            HardshipStatus.APPROVED: {HardshipStatus.RESOLVED},
            HardshipStatus.DENIED: {HardshipStatus.RESOLVED},
            HardshipStatus.RESOLVED: set(),
        }
        if new_status not in valid.get(request.status, set()):
            raise ValueError(f"Invalid transition from {request.status.value} to {new_status.value}")

        request.status = new_status
        if staff_notes:
            request.staff_notes = staff_notes
        if assigned_to:
            request.assigned_to = assigned_to
        if new_status == HardshipStatus.RESOLVED:
            request.resolved_at = datetime.utcnow()
        request.updated_at = datetime.utcnow()
        await session.flush()
        return request

    async def find_overdue_requests(
        self,
        session: AsyncSession,
        school_id: int,
    ) -> list[HardshipRequest]:
        """
        Find hardship requests past their SLA deadline.
        Called by a periodic worker to alert staff.
        """
        now = datetime.utcnow()
        result = await session.execute(
            select(HardshipRequest).where(
                HardshipRequest.school_id == school_id,
                HardshipRequest.status.in_([HardshipStatus.REQUESTED, HardshipStatus.UNDER_REVIEW]),
                HardshipRequest.sla_deadline < now,
            )
        )
        return list(result.scalars().all())
