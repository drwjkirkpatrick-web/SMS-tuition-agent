"""
domain/policy_service.py — Director-configurable reminder policy
═══════════════════════════════════════════════════

School directors can configure:
  - Reminder schedule (which days before due date)
  - Quiet hours (no sends between X and Y)
  - Max reminder attempts per invoice
  - Tone variant (professional, friendly, urgent)
  - Late notice cadence (daily, every 3 days, weekly)

The policy is stored as JSON in `schools.reminder_policy` and
consumed by the scheduler (Step 9) and send workers (Step 10).

Teaching notes:
  - JSON storage is flexible — directors can add custom fields
    without database migrations.
  - Policy changes are logged to audit_events.
  - Invalid policies are caught by Pydantic validation.
═══════════════════════════════════════════════════
"""

import json
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from domain.models import ReminderType
from infra.audit_logger import AuditContext, log_audit_event
from infra.database import async_session_factory
from sqlalchemy import select


class ReminderSchedule(BaseModel):
    """Days before due date for each reminder type."""
    due_14: int = Field(default=14, ge=0, le=365)
    due_3: int = Field(default=3, ge=0, le=365)
    due_today: int = Field(default=0, ge=0, le=365)


class QuietHours(BaseModel):
    start_hour: int = Field(default=21, ge=0, le=23)
    end_hour: int = Field(default=8, ge=0, le=23)


class ReminderPolicy(BaseModel):
    """
    Validated reminder policy for a school.
    """
    version: str = Field(default="v1")
    schedule: ReminderSchedule = Field(default_factory=ReminderSchedule)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    max_reminder_attempts: int = Field(default=3, ge=1, le=20)
    tone_variant: str = Field(default="professional")  # professional, friendly, urgent
    late_notice_cadence_days: int = Field(default=7, ge=1, le=30)
    max_sms_segments: int = Field(default=2, ge=1, le=5)
    enabled: bool = Field(default=True)

    @field_validator("tone_variant")
    @classmethod
    def validate_tone(cls, v: str) -> str:
        allowed = {"professional", "friendly", "urgent"}
        if v not in allowed:
            raise ValueError(f"tone_variant must be one of {allowed}")
        return v


class PolicyService:
    """
    Load, validate, and update school reminder policies.
    """

    async def load_policy(self, school_id: int) -> ReminderPolicy:
        """
        Load policy from school record. Returns default if not set.
        """
        async with async_session_factory() as session:
            from domain.models import School
            result = await session.execute(select(School).where(School.id == school_id))
            school = result.scalar_one_or_none()
            if school and school.reminder_policy:
                data = json.loads(school.reminder_policy)
                return ReminderPolicy(**data)
            return ReminderPolicy()

    async def save_policy(
        self,
        school_id: int,
        policy: ReminderPolicy,
        changed_by: str = "director",
    ) -> None:
        """
        Save policy to school record and log audit event.
        """
        async with async_session_factory() as session:
            from domain.models import School
            result = await session.execute(select(School).where(School.id == school_id))
            school = result.scalar_one_or_none()
            if not school:
                raise ValueError(f"School {school_id} not found")
            
            old_policy = school.reminder_policy
            school.reminder_policy = policy.model_dump_json()
            await session.commit()
            
            await log_audit_event(
                event_type="policy.changed",
                entity_type="school",
                entity_id=str(school_id),
                summary=f"Reminder policy updated by {changed_by}",
                details=json.dumps({
                    "old": old_policy,
                    "new": school.reminder_policy,
                }),
                context=AuditContext(school_id=school_id, actor_type="user", actor_id=changed_by),
            )

    def schedule_to_dict(self, policy: ReminderPolicy) -> dict[ReminderType, int]:
        """
        Convert policy schedule to dict used by ReminderService.
        """
        return {
            ReminderType.DUE_14: policy.schedule.due_14,
            ReminderType.DUE_3: policy.schedule.due_3,
            ReminderType.DUE_TODAY: policy.schedule.due_today,
        }

    def is_quiet_hours(self, policy: ReminderPolicy, hour: int) -> bool:
        """
        Check if given hour falls in quiet hours.
        Handles wrap-around (e.g., 21:00–08:00 crosses midnight).
        """
        start = policy.quiet_hours.start_hour
        end = policy.quiet_hours.end_hour
        if start < end:
            return start <= hour < end
        else:
            return hour >= start or hour < end
