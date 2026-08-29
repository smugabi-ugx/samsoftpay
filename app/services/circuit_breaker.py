"""A tiny, process-local circuit breaker for outbound rail I/O.

The charge path calls the MTN rail SYNCHRONOUSLY inside the web request. If MTN
slows to its timeout, every concurrent charge parks a gunicorn thread for the
full timeout and the whole pool starves — a slow dependency cascades into a dead
web tier. The breaker fails FAST once the rail has clearly stopped answering, so
new charges reject cleanly (retryable) instead of holding a thread for 20s.

States: CLOSED (normal) -> OPEN after `fail_threshold` consecutive network
failures -> HALF-OPEN after `reset_timeout` seconds (one probe allowed) ->
CLOSED again on the probe's success, or back to OPEN on its failure. Only
network TIMEOUTS / connection errors count as failures; a rail that RESPONDS
(even to reject) is proof it is up and resets the breaker.

Process-local by design: each gunicorn/Celery worker protects its own thread
pool; no shared state, no external dependency.
"""
import threading
import time


class RailUnavailableError(Exception):
    """Raised when the breaker is OPEN — the rail was not called. The charge was
    NOT sent, so the caller must treat this as a clean, retryable rejection (NOT
    an ambiguous 'may have reached the rail' outcome)."""


class CircuitBreaker:
    def __init__(self, name: str, *, fail_threshold: int = 5, reset_timeout: float = 30.0):
        self.name = name
        self.fail_threshold = fail_threshold
        self.reset_timeout = reset_timeout
        self._fails = 0
        self._opened_at = None          # monotonic time the breaker opened, or None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        """True if a call may proceed (CLOSED, or HALF-OPEN probe)."""
        with self._lock:
            if self._opened_at is None:
                return True
            if time.monotonic() - self._opened_at >= self.reset_timeout:
                # HALF-OPEN: let exactly one probe through; keep the clock so a
                # failing probe re-opens for another full cooldown.
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._fails = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._fails += 1
            if self._fails >= self.fail_threshold and self._opened_at is None:
                self._opened_at = time.monotonic()
            elif self._opened_at is not None:
                # a probe (HALF-OPEN) failed — restart the cooldown
                self._opened_at = time.monotonic()

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None and \
                (time.monotonic() - self._opened_at) < self.reset_timeout


# Shared breaker for the MTN collections rail (the synchronous, thread-holding
# path the audit flagged). Tunable via env if ever needed; defaults are sane.
mtn_collection_breaker = CircuitBreaker("mtn-collection", fail_threshold=5, reset_timeout=30.0)
