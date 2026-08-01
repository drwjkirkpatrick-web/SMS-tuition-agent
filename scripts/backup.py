#!/usr/bin/env python3
"""
scripts/backup.py — Encrypted database backup runner
═══════════════════════════════════════════════════

Runs an encrypted ``pg_dump`` backup of the application database,
then purges old backups according to the retention policy
(7 daily + 4 weekly).

Intended to run as a sidecar container on a nightly cron, e.g.:
  docker compose run --rm backup python -m scripts.backup

Requirements:
  - ``infra.backup`` module with ``create_backup()`` and
    ``cleanup_old_backups()`` callables.
  - ``BACKUP_ENCRYPTION_KEY`` set in the environment (32-byte hex).
═══════════════════════════════════════════════════
"""

import asyncio
import json
import sys
from typing import Any

from infra import backup
from infra.settings import get_settings

BACKUP_OUTPUT_DIR = "/data/backups"
KEEP_DAILY = 7
KEEP_WEEKLY = 4


async def main() -> None:
    """
    Load settings, create an encrypted backup, then clean up old backups.
    Prints a JSON summary to stdout.
    """
    settings = get_settings()
    database_url = settings.database_url.get_secret_value()
    encryption_key = settings.backup_encryption_key

    # backup_encryption_key is a SecretStr; extract raw value if set
    if encryption_key is not None:
        encryption_key = encryption_key.get_secret_value()

    summary: dict[str, Any] = {
        "database_url": database_url.split("@")[-1] if "@" in database_url else "unknown",
        "output_dir": BACKUP_OUTPUT_DIR,
        "encrypted": encryption_key is not None,
        "backup": None,
        "cleanup": None,
        "errors": [],
    }

    # ── Create backup ──
    try:
        backup_result = await backup.create_backup(
            database_url=database_url,
            output_dir=BACKUP_OUTPUT_DIR,
            encryption_key=encryption_key,
        )
        summary["backup"] = backup_result
    except Exception as exc:
        summary["errors"].append(f"create_backup failed: {exc!r}")

    # ── Cleanup old backups ──
    try:
        cleanup_result = await backup.cleanup_old_backups(
            output_dir=BACKUP_OUTPUT_DIR,
            keep_daily=KEEP_DAILY,
            keep_weekly=KEEP_WEEKLY,
        )
        summary["cleanup"] = cleanup_result
    except Exception as exc:
        summary["errors"].append(f"cleanup_old_backups failed: {exc!r}")

    print(json.dumps(summary, indent=2, default=str))

    if summary["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())