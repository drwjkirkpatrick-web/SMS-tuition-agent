#!/usr/bin/env python3
"""
scripts/health_check.py — Celery worker health check
═══════════════════════════════════════════════════

Verifies that Celery workers are alive and processing by pinging
the Celery control plane over Redis. Intended for Docker healthcheck
or Kubernetes liveness/readiness probes.

Exit codes:
  0 — healthy (at least one worker responded to ping)
  1 — unhealthy (no workers responded, or error contacting broker)

Usage:
  docker compose exec worker python -m scripts.health_check
  python scripts/health_check.py --redis-url redis://localhost:6379/0
═══════════════════════════════════════════════════
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from workers.celery_app import celery_app


async def check_worker_health(redis_url: str) -> dict[str, Any]:
    """
    Ping Celery workers and check active task count.

    Uses ``celery_app.control.inspect()`` which communicates with workers
    over the Redis broker. No direct Redis connection is needed — Celery
    manages the broker connection internally.

    Args:
        redis_url: Redis broker URL (for informational/logging purposes;
                   Celery already knows its broker from celery_app config).

    Returns:
        Dict with keys:
            - healthy (bool): True if at least one worker responded.
            - workers_online (int): Number of workers that responded to ping.
            - active_tasks (int): Total active tasks across all workers.
            - details (dict): Per-worker ping replies and active task lists.
    """
    details: dict[str, Any] = {
        "redis_url": redis_url,
        "ping": {},
        "active": {},
        "errors": [],
    }
    workers_online = 0
    active_tasks = 0

    # inspect() with no args targets all online workers
    inspect = celery_app.control.inspect(timeout=5)

    # ── Ping ──
    try:
        ping_replies = inspect.ping()
    except Exception as exc:
        details["errors"].append(f"ping failed: {exc!r}")
        ping_replies = None

    if ping_replies:
        details["ping"] = ping_replies
        workers_online = len(ping_replies)
    else:
        # ping_replies is None when no workers are online
        details["ping"] = {}
        workers_online = 0

    # ── Active tasks ──
    try:
        active_replies = inspect.active()
    except Exception as exc:
        details["errors"].append(f"active() failed: {exc!r}")
        active_replies = None

    if active_replies:
        details["active"] = active_replies
        for _worker, tasks in active_replies.items():
            if isinstance(tasks, list):
                active_tasks += len(tasks)
    else:
        details["active"] = {}

    healthy = workers_online > 0 and len(details["errors"]) == 0

    return {
        "healthy": healthy,
        "workers_online": workers_online,
        "active_tasks": active_tasks,
        "details": details,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Health check for SMS Tuition Agent Celery workers.",
    )
    parser.add_argument(
        "--redis-url",
        default=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        help="Redis broker URL (default: $REDIS_URL or redis://localhost:6379/0)",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    result = await check_worker_health(args.redis_url)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))