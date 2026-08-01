"""
infra/backup.py — Automated PostgreSQL backup utilities
═══════════════════════════════════════════════════

Encrypted database backups for disaster recovery and compliance.

Pipeline:
  1. ``pg_dump`` streams the database to a SQL file.
  2. The dump is encrypted at rest with Fernet (symmetric AES-128-CBC +
     HMAC-SHA256). Only encrypted files are written to disk.
  3. ``cleanup_old_backups`` prunes old files on a daily/weekly
     retention schedule.

Teaching notes:
  - ``pg_dump`` is invoked via ``asyncio.create_subprocess_exec`` so the
    event loop is not blocked during large dumps. We read stdout to
    capture the SQL without writing an intermediate plaintext file.
  - The encryption key comes from ``Settings.backup_encryption_key``
    (a ``SecretStr``). In production, rotate this key quarterly and
    store it in a secrets manager — never in the repository.
  - ``cleanup_old_backups`` keeps *keep_daily* most recent daily backups
    and *keep_weekly* end-of-week backups. Older files are deleted and
    the count of deleted files is returned for monitoring/alerting.
  - ``run_backup_job`` is the Celery-friendly entry point: it pulls
    settings, calls ``create_backup``, then prunes old backups in one
    atomic job suitable for ``celery_app.conf.beat_schedule``.
═══════════════════════════════════════════════════
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.fernet import Fernet

from infra.settings import get_settings

logger = logging.getLogger(__name__)

# Backup filename prefix and extension
_BACKUP_PREFIX = "sms_db_backup"
_BACKUP_SUFFIX = ".sql.enc"


async def create_backup(
    database_url: str,
    output_dir: str,
    encryption_key: str,
) -> str:
    """
    Dump the database and write an encrypted backup file.

    Args:
        database_url: SQLAlchemy-style PostgreSQL URL
            (e.g., ``postgresql+asyncpg://user:pass@host:5432/db``).
            The ``+asyncpg`` driver suffix is stripped for ``pg_dump``,
            which expects a plain ``postgresql://`` URL.
        output_dir: Directory where the ``.sql.enc`` file is written.
            Created if it does not exist.
        encryption_key: Fernet-compatible key (32 url-safe base64 bytes).
            Generate with ``Fernet.generate_key()``.

    Returns:
        The absolute path to the encrypted backup file.

    Raises:
        RuntimeError: If ``pg_dump`` exits with a non-zero status.
        ValueError: If *encryption_key* is not a valid Fernet key.
    """
    # Ensure the output directory exists
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Strip the SQLAlchemy async driver suffix so pg_dump accepts the URL
    pg_url = database_url.replace("+asyncpg", "").replace("+psycopg2", "")

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"{_BACKUP_PREFIX}_{timestamp}{_BACKUP_SUFFIX}"
    backup_path = out_path / backup_filename

    logger.info("Starting database backup → %s", backup_path)

    # ── 1. Run pg_dump via subprocess ──
    process = await asyncio.create_subprocess_exec(
        "pg_dump",
        pg_url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    dump_bytes, stderr_bytes = await process.communicate()

    if process.returncode != 0:
        stderr_msg = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        logger.error("pg_dump failed (exit %s): %s", process.returncode, stderr_msg)
        raise RuntimeError(f"pg_dump exited with status {process.returncode}: {stderr_msg}")

    # ── 2. Encrypt the dump ──
    try:
        fernet = Fernet(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)
    except Exception as exc:
        raise ValueError(f"Invalid Fernet encryption key: {exc}") from exc

    encrypted = fernet.encrypt(dump_bytes)

    # ── 3. Write the encrypted file ──
    backup_path.write_bytes(encrypted)
    logger.info("Backup complete: %s (%d bytes encrypted)", backup_path, len(encrypted))

    return str(backup_path.resolve())


def cleanup_old_backups(
    backup_dir: str,
    keep_daily: int = 7,
    keep_weekly: int = 4,
) -> int:
    """
    Delete old backup files beyond the retention policy.

    Retention logic:
      - Keep the *keep_daily* most recent backups (regardless of age).
      - Additionally keep up to *keep_weekly* end-of-week backups
        (Sunday) that are older than the daily window, so you have
        longer-term recovery points.

    Args:
        backup_dir: Directory containing ``.sql.enc`` backup files.
        keep_daily: Number of most-recent daily backups to keep.
        keep_weekly: Number of weekly (Sunday) backups to keep beyond
            the daily window.

    Returns:
        The number of backup files deleted.
    """
    backup_path = Path(backup_dir)
    if not backup_path.is_dir():
        logger.warning("Backup directory does not exist: %s", backup_dir)
        return 0

    # Gather all backup files, sorted by modification time (newest first)
    files = sorted(
        backup_path.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    if not files:
        return 0

    kept: set[Path] = set()
    deleted_count = 0

    # ── Keep the most-recent daily backups ──
    for f in files[:keep_daily]:
        kept.add(f)

    # ── Keep up to keep_weekly Sunday backups beyond the daily window ──
    weekly_kept = 0
    for f in files[keep_daily:]:
        if weekly_kept >= keep_weekly:
            break
        mtime = datetime.fromtimestamp(f.stat().st_mtime)
        # weekday() == 6 → Sunday
        if mtime.weekday() == 6:
            kept.add(f)
            weekly_kept += 1

    # ── Delete everything not retained ──
    for f in files:
        if f not in kept:
            try:
                f.unlink()
                deleted_count += 1
                logger.info("Deleted old backup: %s", f.name)
            except OSError as exc:
                logger.error("Failed to delete %s: %s", f, exc)

    return deleted_count


async def run_backup_job() -> dict:
    """
    Celery-friendly entry point that runs a full backup cycle.

    Pulls configuration from ``Settings``, creates a new encrypted
    backup, then prunes old backups per the retention policy.

    Suitable for wrapping in a Celery task::

        @celery_app.task(name="infra.backup.run_backup_job")
        def run_backup_task():
            import asyncio
            from infra.backup import run_backup_job
            return asyncio.run(run_backup_job())

    Or calling from an async Celery worker.

    Returns:
        A dict with the backup path, number of old backups deleted,
        and a timestamp — useful for Celery result inspection and
        monitoring alerts.
    """
    settings = get_settings()

    database_url = settings.database_url.get_secret_value()
    encryption_key = settings.backup_encryption_key
    if encryption_key is None:
        raise RuntimeError(
            "BACKUP_ENCRYPTION_KEY is not set — cannot create encrypted backup"
        )
    encryption_key_str = encryption_key.get_secret_value()

    # Default backup directory (can be overridden by env or volume mount)
    backup_dir = os.getenv("BACKUP_DIR", "/var/backups/sms-tuition-agent")

    backup_path = await create_backup(
        database_url=database_url,
        output_dir=backup_dir,
        encryption_key=encryption_key_str,
    )

    deleted = cleanup_old_backups(backup_dir)

    result = {
        "backup_path": backup_path,
        "old_backups_deleted": deleted,
        "timestamp": datetime.utcnow().isoformat(),
    }
    logger.info("Backup job complete: %s", result)
    return result