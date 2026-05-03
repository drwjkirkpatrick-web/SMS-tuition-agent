"""
infra/settings.py — Typed application configuration
═══════════════════════════════════════════════════

Pydantic Settings reads environment variables and validates them at
startup. If a required secret is missing or malformed, the app refuses
to start with a clear error message.

Teaching notes:
  - `model_config = SettingsConfigDict(env_file=".env")` lets you use
    a .env file in development; in production, env vars override the file.
  - `Field(..., validation_alias="OLD_NAME")` supports migrating
    config names without breaking existing deployments.
  - `SecretStr` hides the value in logs and tracebacks (shows *****).
═══════════════════════════════════════════════════
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # allow unknown env vars (don't crash)
    )

    # ── Application ──
    app_env: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Controls logging level and error detail exposure",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
    )
    school_timezone: str = Field(
        default="America/Los_Angeles",
        description="IANA timezone name for quiet hours and due dates",
    )

    # ── Database ──
    database_url: SecretStr = Field(
        ...,  # required (no default)
        description="PostgreSQL async connection string",
        examples=["postgresql+asyncpg://user:pass@db:5432/dbname"],
    )

    # ── Redis ──
    redis_url: SecretStr = Field(
        ...,  # required
        description="Redis connection string (Celery broker + cache)",
        examples=["redis://redis:6379/0"],
    )

    # ── SMS Provider: Twilio ──
    twilio_account_sid: SecretStr = Field(
        ...,  # required
        description="Twilio Account SID (starts with AC)",
    )
    twilio_auth_token: SecretStr = Field(
        ...,  # required
        description="Twilio Auth Token",
    )
    twilio_phone_number: str = Field(
        ...,  # required
        description="Twilio phone number in E.164 format (+15551234567)",
        pattern=r"^\+1[0-9]{10}$",  # US/Canada only for now
    )

    # ── Webhook Security ──
    webhook_secret: SecretStr = Field(
        ...,  # required
        description="Shared secret to verify inbound webhook signatures",
    )

    # ── Backup / Encryption ──
    backup_encryption_key: SecretStr = Field(
        default=None,
        description="32-byte hex key for backup encryption",
    )

    # ── Admin Dashboard ──
    admin_token_hash: str = Field(
        default=None,
        description="bcrypt hash of the director dashboard token",
    )

    # ── Operational ──
    max_sms_segments: int = Field(
        default=2,
        description="Maximum SMS segments per message (1 = 160 chars)",
    )
    quiet_hours_start: int = Field(
        default=21,
        description="Hour (24h) when quiet hours begin (default 21:00 / 9 PM)",
        ge=0,
        le=23,
    )
    quiet_hours_end: int = Field(
        default=8,
        description="Hour (24h) when quiet hours end (default 8:00 AM)",
        ge=0,
        le=23,
    )
    reminder_retry_max: int = Field(
        default=3,
        description="Maximum send retries per message",
        ge=0,
        le=10,
    )
    unknown_delivery_reconcile_minutes: int = Field(
        default=10,
        description="Minutes between reconciliation checks",
        ge=1,
    )


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings instance.
    
    `lru_cache` means we parse the environment only once per process.
    This is important for Celery workers (they import settings frequently).
    """
    return Settings()
