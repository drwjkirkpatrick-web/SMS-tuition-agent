"""
adapters/csv_connector.py — CSV SIS Adapter
═══════════════════════════════════════════════════

The simplest SIS connector: reads CSV files exported from the school's
billing software (QuickBooks, FACTS, TuitionPay, etc.).

Expected CSV formats:
  students.csv:   sis_student_id,first_name,grade_level
  guardians.csv:  sis_guardian_id,first_name,phone,email,relationship,is_primary,student_ids
  invoices.csv:   sis_invoice_id,sis_student_id,invoice_number,amount_due,amount_paid,due_date,status
  payments.csv:   sis_payment_id,sis_invoice_id,amount,payment_method,paid_at

Teaching notes:
  - CSV is the "lowest common denominator" — every billing system exports it.
  - We use Python's built-in `csv.DictReader` (no extra dependencies).
  - Dedupe: we check if a record already exists in the DB before inserting.
  - The connector stores its checkpoint in the database (schools.sis_config JSON).
  - "Student IDs" in guardians.csv is a comma-separated list linking guardians to students.
═══════════════════════════════════════════════════
"""

import csv
import json
import os
from datetime import datetime
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adapters.sis_connector import (
    GuardianRecord,
    InvoiceRecord,
    PaymentRecord,
    SISConnector,
    StudentRecord,
    SyncCheckpoint,
)
from domain.models import Guardian, Invoice, Payment, School, Student, StudentGuardianLink
from infra.database import async_session_factory


class CSVConnector(SISConnector):
    """
    SIS connector that reads CSV files from a local directory.
    
    Config expected in `sis_config` JSON:
    {
        "csv_directory": "/data/sis_exports",
        "encoding": "utf-8",
        "delimiter": ","
    }
    """

    async def get_checkpoint(self) -> SyncCheckpoint:
        """Load checkpoint from the school's sis_config JSON."""
        async with async_session_factory() as session:
            result = await session.execute(
                select(School).where(School.id == self.school_id)
            )
            school = result.scalar_one_or_none()
            if school and school.sis_config:
                config = json.loads(school.sis_config)
                cp = config.get("checkpoint", {})
                return SyncCheckpoint(
                    last_sync_at=datetime.fromisoformat(cp["last_sync_at"]) if cp.get("last_sync_at") else None,
                    last_record_id=cp.get("last_record_id"),
                    checksum=cp.get("checksum"),
                )
            return SyncCheckpoint()

    async def save_checkpoint(self, checkpoint: SyncCheckpoint) -> None:
        """Save checkpoint back to the school's sis_config JSON."""
        async with async_session_factory() as session:
            result = await session.execute(
                select(School).where(School.id == self.school_id)
            )
            school = result.scalar_one_or_none()
            if school:
                config = json.loads(school.sis_config or "{}")
                config["checkpoint"] = {
                    "last_sync_at": checkpoint.last_sync_at.isoformat() if checkpoint.last_sync_at else None,
                    "last_record_id": checkpoint.last_record_id,
                    "checksum": checkpoint.checksum,
                }
                school.sis_config = json.dumps(config)
                await session.commit()

    async def test_connection(self) -> bool:
        """Check if the CSV directory exists and contains files."""
        csv_dir = self.config.get("csv_directory", "/data/sis_exports")
        return os.path.isdir(csv_dir) and any(
            f.endswith(".csv") for f in os.listdir(csv_dir)
        )

    async def sync_students(self, checkpoint: SyncCheckpoint) -> AsyncIterator[StudentRecord]:
        """Read students.csv and yield records."""
        csv_dir = self.config.get("csv_directory", "/data/sis_exports")
        filepath = os.path.join(csv_dir, "students.csv")
        if not os.path.exists(filepath):
            return
            yield  # type: ignore[unreachable]

        with open(filepath, "r", encoding=self.config.get("encoding", "utf-8")) as f:
            reader = csv.DictReader(f, delimiter=self.config.get("delimiter", ","))
            for row in reader:
                yield StudentRecord(
                    sis_student_id=row["sis_student_id"].strip(),
                    first_name=row["first_name"].strip(),
                    grade_level=row.get("grade_level", "").strip() or None,
                )

    async def sync_guardians(self, checkpoint: SyncCheckpoint) -> AsyncIterator[GuardianRecord]:
        csv_dir = self.config.get("csv_directory", "/data/sis_exports")
        filepath = os.path.join(csv_dir, "guardians.csv")
        if not os.path.exists(filepath):
            return
            yield  # type: ignore[unreachable]

        with open(filepath, "r", encoding=self.config.get("encoding", "utf-8")) as f:
            reader = csv.DictReader(f, delimiter=self.config.get("delimiter", ","))
            for row in reader:
                yield GuardianRecord(
                    sis_guardian_id=row.get("sis_guardian_id", "").strip() or None,
                    first_name=row["first_name"].strip(),
                    phone=row["phone"].strip(),
                    email=row.get("email", "").strip() or None,
                    relationship=row.get("relationship", "").strip() or None,
                    is_primary=row.get("is_primary", "true").lower() in ("true", "1", "yes"),
                )

    async def sync_invoices(self, checkpoint: SyncCheckpoint) -> AsyncIterator[InvoiceRecord]:
        csv_dir = self.config.get("csv_directory", "/data/sis_exports")
        filepath = os.path.join(csv_dir, "invoices.csv")
        if not os.path.exists(filepath):
            return
            yield  # type: ignore[unreachable]

        with open(filepath, "r", encoding=self.config.get("encoding", "utf-8")) as f:
            reader = csv.DictReader(f, delimiter=self.config.get("delimiter", ","))
            for row in reader:
                yield InvoiceRecord(
                    sis_invoice_id=row["sis_invoice_id"].strip(),
                    sis_student_id=row["sis_student_id"].strip(),
                    invoice_number=row["invoice_number"].strip(),
                    amount_due=float(row["amount_due"]),
                    amount_paid=float(row.get("amount_paid", "0")),
                    due_date=row["due_date"].strip(),
                    status=row.get("status", "pending").strip(),
                )

    async def sync_payments(self, checkpoint: SyncCheckpoint) -> AsyncIterator[PaymentRecord]:
        csv_dir = self.config.get("csv_directory", "/data/sis_exports")
        filepath = os.path.join(csv_dir, "payments.csv")
        if not os.path.exists(filepath):
            return
            yield  # type: ignore[unreachable]

        with open(filepath, "r", encoding=self.config.get("encoding", "utf-8")) as f:
            reader = csv.DictReader(f, delimiter=self.config.get("delimiter", ","))
            for row in reader:
                yield PaymentRecord(
                    sis_payment_id=row.get("sis_payment_id", "").strip() or None,
                    sis_invoice_id=row["sis_invoice_id"].strip(),
                    amount=float(row["amount"]),
                    payment_method=row.get("payment_method", "").strip() or None,
                    paid_at=row.get("paid_at", "").strip() or None,
                )


async def persist_students(
    session: AsyncSession,
    school_id: int,
    records: AsyncIterator[StudentRecord],
) -> int:
    """
    Persist student records with deduplication by sis_student_id.
    Returns count of students inserted or updated.
    
    Teaching note: We use `MERGE`-like behavior (INSERT ... ON CONFLICT DO UPDATE)
    but SQLAlchemy 2.0 doesn't have native merge yet, so we do select-then-upsert.
    """
    count = 0
    async for record in records:
        # Check if exists
        result = await session.execute(
            select(Student).where(
                Student.school_id == school_id,
                Student.sis_student_id == record.sis_student_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            # Update
            existing.first_name = record.first_name
            existing.updated_at = datetime.utcnow()
        else:
            # Insert
            session.add(Student(
                school_id=school_id,
                first_name=record.first_name,
                sis_student_id=record.sis_student_id,
            ))
        count += 1
    await session.flush()
    return count


async def persist_guardians(
    session: AsyncSession,
    school_id: int,
    records: AsyncIterator[GuardianRecord],
) -> int:
    """Persist guardians with dedupe by phone per school."""
    count = 0
    async for record in records:
        result = await session.execute(
            select(Guardian).where(
                Guardian.school_id == school_id,
                Guardian.phone == record.phone,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.first_name = record.first_name
            existing.updated_at = datetime.utcnow()
        else:
            session.add(Guardian(
                school_id=school_id,
                first_name=record.first_name,
                phone=record.phone,
            ))
        count += 1
    await session.flush()
    return count
