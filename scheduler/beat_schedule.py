"""
scheduler/beat_schedule.py
═══════════════════════════════════════════════════

The actual Beat schedule lives in workers/celery_app.py (see
`beat_schedule` dict). This file is reserved for:
  - Custom scheduler classes (if we move beyond Celery Beat)
  - Director-configurable schedule overrides stored in DB
  - Runbook documentation for cron-like scheduling

For now, it is a placeholder. Step 19 will add dynamic policy loading.
═══════════════════════════════════════════════════
"""
