"""
api/admin.py — Admin and staff API endpoints
═══════════════════════════════════════════════════

Read-only endpoints for:
  - Dashboard stats (messages sent/failed/pending)
  - Hardship/CALL queue (staff-facing)
  - Invoice lookup
  - Guardian opt-in status

Authentication: simple token in X-Admin-Token header
(bcrypt hash compared against ADMIN_TOKEN_HASH in settings).

Teaching notes:
  - These endpoints are for the Mission Control dashboard (Step 20).
  - All endpoints are GET only (no writes) for safety.
  - Data is masked (phone numbers, names) before returning.
═══════════════════════════════════════════════════
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from passlib.hash import bcrypt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.models import (
    AuditEvent,
    Guardian,
    HardshipRequest,
    HardshipStatus,
    InboundMessage,
    Invoice,
    InvoiceStatus,
    MessageStatus,
    OutboundMessage,
    Payment,
    School,
    Student,
)
from domain.masking import mask_name, mask_phone
from infra.database import get_db
from infra.settings import get_settings

router = APIRouter()


async def verify_admin_token(x_admin_token: Optional[str] = Header(None)) -> None:
    """Verify the admin token from X-Admin-Token header."""
    settings = get_settings()
    if not settings.admin_token_hash:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Admin auth not configured")
    if not x_admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing X-Admin-Token")
    if not bcrypt.verify(x_admin_token, settings.admin_token_hash):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token")


# ── Dashboard Stats ──

@router.get("/dashboard/stats", dependencies=[Depends(verify_admin_token)])
async def dashboard_stats(
    school_id: int = 1,
    session: AsyncSession = Depends(get_db),
) -> dict:
    """Aggregate counts for the dashboard."""
    # Message counts
    msg_counts = {}
    for s in MessageStatus:
        count_result = await session.execute(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.school_id == school_id,
                OutboundMessage.status == s,
            )
        )
        msg_counts[s.value] = count_result.scalar()
    
    # Invoice counts
    inv_counts = {}
    for s in InvoiceStatus:
        count_result = await session.execute(
            select(func.count(Invoice.id)).where(
                Invoice.school_id == school_id,
                Invoice.status == s,
            )
        )
        inv_counts[s.value] = count_result.scalar()
    
    # Hardship queue
    hardship_count = await session.execute(
        select(func.count(HardshipRequest.id)).where(
            HardshipRequest.school_id == school_id,
            HardshipRequest.status.in_([HardshipStatus.REQUESTED, HardshipStatus.UNDER_REVIEW]),
        )
    )
    
    return {
        "messages": msg_counts,
        "invoices": inv_counts,
        "hardship_queue": hardship_count.scalar(),
        "school_id": school_id,
    }


# ── Hardship / CALL Queue ──

@router.get("/queue/hardship", dependencies=[Depends(verify_admin_token)])
async def hardship_queue(
    school_id: int = 1,
    status: Optional[str] = None,
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List hardship requests for staff review."""
    stmt = select(HardshipRequest).where(HardshipRequest.school_id == school_id)
    if status:
        stmt = stmt.where(HardshipRequest.status == status)
    else:
        stmt = stmt.where(
            HardshipRequest.status.in_([
                HardshipStatus.REQUESTED.value,
                HardshipStatus.UNDER_REVIEW.value,
            ])
        )
    stmt = stmt.order_by(HardshipRequest.created_at)
    
    result = await session.execute(stmt)
    items = []
    for req in result.scalars().all():
        guardian = req.guardian
        items.append({
            "id": req.id,
            "guardian_name": mask_name(guardian.first_name) if guardian else None,
            "guardian_phone": mask_phone(guardian.phone) if guardian else None,
            "status": req.status.value,
            "request_body": req.request_body,
            "assigned_to": req.assigned_to,
            "sla_deadline": req.sla_deadline.isoformat() if req.sla_deadline else None,
            "created_at": req.created_at.isoformat() if req.created_at else None,
        })
    return items


@router.get("/queue/callback", dependencies=[Depends(verify_admin_token)])
async def callback_queue(
    school_id: int = 1,
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List CALL requests from inbound messages."""
    result = await session.execute(
        select(InboundMessage).where(
            InboundMessage.school_id == school_id,
            InboundMessage.intent == "call",
            InboundMessage.processed_at.is_(None),
        ).order_by(InboundMessage.created_at)
    )
    items = []
    for msg in result.scalars().all():
        guardian = msg.guardian
        items.append({
            "id": msg.id,
            "guardian_name": mask_name(guardian.first_name) if guardian else None,
            "guardian_phone": mask_phone(guardian.phone) if guardian else None,
            "body": msg.body,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        })
    return items


# ── Invoice Lookup ──

@router.get("/invoices", dependencies=[Depends(verify_admin_token)])
async def list_invoices(
    school_id: int = 1,
    status: Optional[str] = None,
    guardian_id: Optional[int] = None,
    session: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List invoices with optional filtering."""
    stmt = select(Invoice).where(Invoice.school_id == school_id)
    if status:
        stmt = stmt.where(Invoice.status == status)
    if guardian_id:
        stmt = stmt.where(Invoice.guardian_id == guardian_id)
    stmt = stmt.order_by(Invoice.due_date)
    
    result = await session.execute(stmt)
    items = []
    for inv in result.scalars().all():
        student = inv.student
        guardian = inv.guardian
        balance = float(inv.amount_due) - float(inv.amount_paid)
        items.append({
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "student_name": mask_name(student.first_name) if student else None,
            "guardian_name": mask_name(guardian.first_name) if guardian else None,
            "amount_due": str(inv.amount_due),
            "amount_paid": str(inv.amount_paid),
            "balance": f"{balance:.2f}",
            "due_date": str(inv.due_date),
            "status": inv.status.value,
        })
    return items
