"""
adapters/sis_connector.py — SIS Connector Interface
═══════════════════════════════════════════════════

A "connector" is an adapter that translates between the school's
Student Information System (SIS) and our database schema.

Design pattern: Abstract Base Class (ABC).
  - Defines WHAT every connector must do (sync_students, sync_invoices, etc.)
  - Concrete implementations (CSV, REST API, etc.) fill in HOW.

Teaching notes:
  - ABC enforces that all connectors implement the same methods.
  - If someone writes a new connector for PowerSchool, mypy will
    complain if they forget `sync_invoices()`.
  - `SyncCheckpoint` tracks the last successful sync so we only pull
    changes (incremental sync), not the entire database every time.
═══════════════════════════════════════════════════
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Optional


@dataclass
class SyncCheckpoint:
    """
    Tracks sync progress for incremental updates.
    
    last_sync_at: the timestamp of the last successful sync
    last_record_id: the highest ID processed (for cursor-based pagination)
    checksum: hash of the last batch (detect if source data rewound)
    """
    last_sync_at: Optional[datetime] = None
    last_record_id: Optional[int] = None
    checksum: Optional[str] = None


@dataclass
class StudentRecord:
    """Normalized student data from any SIS."""
    sis_student_id: str          # external ID from SIS
    first_name: str
    grade_level: Optional[str] = None


@dataclass
class GuardianRecord:
    """Normalized guardian data from any SIS."""
    first_name: str
    phone: str                   # E.164 format expected
    sis_guardian_id: Optional[str] = None
    email: Optional[str] = None
    relationship: Optional[str] = None  # "parent", "grandparent", etc.
    is_primary: bool = True


@dataclass
class InvoiceRecord:
    """Normalized invoice data from any SIS."""
    sis_invoice_id: str
    sis_student_id: str
    invoice_number: str
    amount_due: float            # use Decimal in production
    amount_paid: float
    due_date: str                # ISO 8601 date string
    status: str                  # pending, partial, paid, overdue, cancelled


@dataclass
class PaymentRecord:
    """Normalized payment data from any SIS."""
    sis_invoice_id: str
    amount: float
    sis_payment_id: Optional[str] = None
    payment_method: Optional[str] = None
    paid_at: Optional[str] = None  # ISO 8601 datetime


class SISConnector(ABC):
    """
    Abstract base class for all SIS connectors.
    
    Each method returns an AsyncIterator so we can stream large datasets
    without loading everything into memory (important on Raspberry Pi).
    """

    def __init__(self, school_id: int, config: dict):
        self.school_id = school_id
        self.config = config

    @abstractmethod
    async def get_checkpoint(self) -> SyncCheckpoint:
        """Read the last sync checkpoint from persistent storage."""
        ...

    @abstractmethod
    async def save_checkpoint(self, checkpoint: SyncCheckpoint) -> None:
        """Save checkpoint after a successful sync."""
        ...

    @abstractmethod
    async def sync_students(self, checkpoint: SyncCheckpoint) -> AsyncIterator[StudentRecord]:
        """Yield students created or modified since last_sync_at."""
        ...

    @abstractmethod
    async def sync_guardians(self, checkpoint: SyncCheckpoint) -> AsyncIterator[GuardianRecord]:
        """Yield guardians created or modified since last_sync_at."""
        ...

    @abstractmethod
    async def sync_invoices(self, checkpoint: SyncCheckpoint) -> AsyncIterator[InvoiceRecord]:
        """Yield invoices created or modified since last_sync_at."""
        ...

    @abstractmethod
    async def sync_payments(self, checkpoint: SyncCheckpoint) -> AsyncIterator[PaymentRecord]:
        """Yield payments created or modified since last_sync_at."""
        ...

    @abstractmethod
    async def test_connection(self) -> bool:
        """Verify the connector can reach the SIS data source."""
        ...
