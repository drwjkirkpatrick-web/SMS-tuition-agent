"""
infra/circuit_breaker.py — Circuit breaker for external API calls
═══════════════════════════════════════════════════

Wraps calls to external services (Twilio, payment gateways) with a
circuit-breaker pattern so that a flapping dependency does not exhaust
Celery worker threads or burn API quota on requests doomed to fail.

States:
  CLOSED    — Requests flow normally. Failures are counted.
  OPEN      — All requests are rejected immediately. After a cooldown
              period, the breaker transitions to HALF_OPEN.
  HALF_OPEN — A single "test" request is allowed. If it succeeds, the
              breaker closes. If it fails, it re-opens.

Teaching notes:
  - State is stored in Redis (not in-memory) so all Celery workers
    share the same circuit state. A worker that sees OPEN will short-
    circuit without even trying the API.
  - We use separate Redis keys for failure count, state, and the
    open-timestamp. A Lua script would be more atomic, but for the
    expected throughput (Twilio sends) the small race window is
    acceptable — at worst, one extra test request slips through.
  - Default thresholds: 5 consecutive failures → OPEN for 60s.
    These can be tuned per integration key.
═══════════════════════════════════════════════════
"""

from enum import Enum

from infra.redis_pool import redis_client


class CircuitState(str, Enum):
    """Circuit breaker states (stored as strings in Redis)."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# Default tuning parameters
DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_COOLDOWN_SECONDS = 60


class CircuitBreaker:
    """
    Redis-backed circuit breaker for external API calls.

    Each *key* (e.g., ``"twilio"``, ``"stripe"``) maintains an
    independent circuit. All state lives in Redis so it is shared
    across Celery workers and FastAPI processes.

    Usage:

        breaker = CircuitBreaker()
        if await breaker.can_execute("twilio"):
            try:
                await twilio_client.send(...)
                await breaker.record_success("twilio")
            except Exception:
                await breaker.record_failure("twilio")
        else:
            raise Exception("Twilio circuit is open — try later")
    """

    def __init__(
        self,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds

    # ── Redis key helpers ──

    @staticmethod
    def _state_key(key: str) -> str:
        return f"circuit:{key}:state"

    @staticmethod
    def _failures_key(key: str) -> str:
        return f"circuit:{key}:failures"

    @staticmethod
    def _opened_key(key: str) -> str:
        return f"circuit:{key}:opened_at"

    # ── Public API ──

    async def can_execute(self, key: str) -> bool:
        """
        Check whether a request to *key* may proceed.

        Returns ``True`` when the circuit is CLOSED or when it is
        HALF_OPEN (allowing a single test request). Returns ``False``
        when the circuit is OPEN and the cooldown has not elapsed.

        Side effects:
          - If the OPEN cooldown has expired, transitions to HALF_OPEN.
        """
        state = await redis_client.get(self._state_key(key))

        # No state key → circuit has never tripped → CLOSED
        if state is None or state == CircuitState.CLOSED.value:
            return True

        if state == CircuitState.OPEN.value:
            # Check if the cooldown has elapsed
            opened_at = await redis_client.get(self._opened_key(key))
            if opened_at is None:
                # Stale state with no timestamp — treat as expired
                await self._transition(key, CircuitState.HALF_OPEN)
                return True

            elapsed = int(opened_at)
            import time
            if (int(time.time()) - elapsed) >= self.cooldown_seconds:
                # Cooldown expired → allow one test request
                await self._transition(key, CircuitState.HALF_OPEN)
                return True
            return False

        if state == CircuitState.HALF_OPEN.value:
            # Only one test request is allowed in HALF_OPEN.
            # The caller that gets here is that test request.
            return True

        return True

    async def record_success(self, key: str) -> None:
        """
        Record a successful call. Resets the failure counter and
        closes the circuit (from HALF_OPEN or CLOSED).
        """
        pipe = redis_client.pipeline()
        pipe.set(self._state_key(key), CircuitState.CLOSED.value)
        pipe.delete(self._failures_key(key))
        pipe.delete(self._opened_key(key))
        await pipe.execute()

    async def record_failure(self, key: str) -> None:
        """
        Record a failed call. Increments the failure counter.

        - In CLOSED: if failures reach the threshold, open the circuit.
        - In HALF_OPEN: a test request failed — re-open immediately.
        - In OPEN: no-op (already open).
        """
        state = await redis_client.get(self._state_key(key))

        # A failure in HALF_OPEN means the test request failed → re-open
        if state == CircuitState.HALF_OPEN.value:
            await self._open_circuit(key)
            return

        # Increment failure count (INCR initializes at 1 if key missing)
        failures = int(await redis_client.incr(self._failures_key(key)))

        if failures >= self.failure_threshold:
            await self._open_circuit(key)

    # ── Internal helpers ──

    async def _open_circuit(self, key: str) -> None:
        """Transition to OPEN and record the timestamp."""
        import time
        pipe = redis_client.pipeline()
        pipe.set(self._state_key(key), CircuitState.OPEN.value)
        pipe.set(self._opened_key(key), int(time.time()))
        # Keep the failure count so we can inspect it, but it will be
        # reset on the next record_success().
        await pipe.execute()

    async def _transition(self, key: str, new_state: CircuitState) -> None:
        """Transition the circuit to *new_state*."""
        await redis_client.set(self._state_key(key), new_state.value)


# Module-level singleton — safe because all state is in Redis.
circuit_breaker = CircuitBreaker()