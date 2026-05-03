"""
infra/redis_pool.py — Async Redis connection pool
═══════════════════════════════════════════════════

We use Redis for:
  1. Celery broker (task queue)
  2. Celery result backend (task return values)
  3. Rate limiting / caching (optional, future)

This file provides a standalone async Redis client for use in
FastAPI endpoints (e.g., health checks, caching).
Celery handles its own Redis connections internally.
═══════════════════════════════════════════════════
"""

import redis.asyncio as aioredis

from infra.settings import get_settings

_settings = get_settings()
REDIS_URL = _settings.redis_url.get_secret_value()

# Async Redis client (used by FastAPI endpoints)
redis_client = aioredis.from_url(
    REDIS_URL,
    decode_responses=True,  # return strings, not bytes
)


async def ping_redis() -> bool:
    """Health check helper."""
    try:
        return await redis_client.ping()
    except Exception:
        return False


async def close_redis():
    """Close Redis connection pool on shutdown."""
    await redis_client.close()
