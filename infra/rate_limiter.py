"""
infra/rate_limiter.py — Redis-backed rate limiting for admin API endpoints
═══════════════════════════════════════════════════

Protects admin endpoints from brute-force token attacks and abusive
polling. Uses a sliding-window counter via Redis INCR + EXPIRE.

Teaching notes:
  - We use INCR + EXPIRE (not a Lua script) for simplicity. The first
    request in a window sets the TTL; subsequent requests only INCR.
    This means the window is *fixed* from the first request, not truly
    sliding — acceptable for rate-limiting admin dashboards.
  - `decode_responses=True` on the Redis client means INCR returns a
    Python `str`, so we cast to `int` explicitly.
  - The FastAPI dependency reads the client IP from `Request.client.host`.
    Behind a reverse proxy, use `X-Forwarded-For` (configure via
    `uvicorn --proxy-headers` or a middleware to trust the header).
═══════════════════════════════════════════════════
"""

from fastapi import HTTPException, Request, status

from infra.redis_pool import redis_client


class RateLimiter:
    """
    Redis-backed fixed-window rate limiter.

    Usage:
        limiter = RateLimiter()
        allowed, retry_after = await limiter.check_rate_limit(
            key="admin:203.0.113.5",
            limit=60,
            window_seconds=60,
        )
        if not allowed:
            raise HTTPException(429, ..., headers={"Retry-After": str(retry_after)})
    """

    async def check_rate_limit(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        """
        Check whether a request is allowed under the rate limit.

        Uses Redis INCR + EXPIRE:
          1. INCR the counter key.
          2. If this is the first request (count == 1), set EXPIRE.
          3. If count > limit, reject and compute retry-after.

        Args:
            key: Unique identifier for the rate-limit bucket
                 (e.g., ``"admin:ip:203.0.113.5"``).
            limit: Maximum requests allowed in the window.
            window_seconds: Size of the fixed window in seconds.

        Returns:
            A tuple of ``(allowed, retry_after_seconds)``.
            When *allowed* is ``False``, *retry_after_seconds* indicates
            how long the client should wait before retrying.
        """
        redis_key = f"ratelimit:{key}"

        # Increment the counter; set TTL only on the first request
        count = int(await redis_client.incr(redis_key))
        if count == 1:
            await redis_client.expire(redis_key, window_seconds)

        if count > limit:
            # Compute remaining TTL so the client knows how long to wait
            ttl = await redis_client.ttl(redis_key)
            retry_after = ttl if ttl > 0 else window_seconds
            return (False, retry_after)

        return (True, 0)


# Module-level singleton — safe because RateLimiter is stateless
# (all state lives in Redis).
rate_limiter = RateLimiter()


# ── FastAPI Dependency ──


async def rate_limit_dependency(request: Request) -> None:
    """
    FastAPI dependency that limits admin endpoints to 60 requests/minute
    per client IP.

    Usage in a router:

        from infra.rate_limiter import rate_limit_dependency

        @router.get(
            "/dashboard/stats",
            dependencies=[Depends(verify_admin_token), Depends(rate_limit_dependency)],
        )
        async def dashboard_stats(...): ...

    Raises:
        HTTPException: 429 Too Many Requests with a ``Retry-After``
            header when the limit is exceeded.
    """
    # client.host is the peer IP; behind a proxy, ensure Uvicorn is
    # started with --proxy-headers so this reflects X-Forwarded-For.
    client_ip = request.client.host if request.client else "unknown"

    allowed, retry_after = await rate_limiter.check_rate_limit(
        key=f"admin:ip:{client_ip}",
        limit=60,
        window_seconds=60,
    )

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )