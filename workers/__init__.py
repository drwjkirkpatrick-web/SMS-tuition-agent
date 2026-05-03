"""
workers package — Celery background task workers
═══════════════════════════════════════════════════

This package contains all Celery tasks organized by domain:
  - reminders.py      — daily reminder scheduling
  - sends.py          — SMS dispatch (Step 11–12)
  - reconciliation.py — payment and delivery reconciliation (Step 14, 17)
  - inbound.py        — inbound SMS parsing (Step 16)

Each task is registered with @celery_app.task and routed to a named queue.
═══════════════════════════════════════════════════
"""

# Re-export tasks so autodiscover finds them
from workers.reminders import compute_reminder_candidates  # noqa: F401
