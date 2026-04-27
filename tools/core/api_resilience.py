"""Unified API resilience layer for all external service calls.

Provides circuit breaker, intelligent retry with error-type-aware backoff,
and Gemini quota fallback. Replaces per-module ad-hoc retry logic with a
single consistent strategy.

Usage::

    @resilient_api_call(service="gemini", operation="transcribe")
    def call_gemini(prompt: str) -> str:
        return client.generate(prompt)

    # Check circuit health (exposed via /health endpoint)
    status = get_circuit_status()
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

_AUTH_STATUS_CODES = (401, 403)
_SERVER_ERROR_CODES = (500, 502, 503, 504)
_QUOTA_STATUS_CODES = (429,)
_CLAUDE_OVERLOADED_CODE = 529


def _extract_status_code(exc: Exception) -> int | None:
    """Best-effort extraction of an HTTP status code from an exception."""
    # httpx.HTTPStatusError
    if hasattr(exc, "response") and hasattr(exc.response, "status_code"):
        return int(exc.response.status_code)

    # google.api_core / anthropic / generic exceptions with status_code attr
    if hasattr(exc, "status_code"):
        return int(exc.status_code)

    # Fall back to string matching for wrapped errors
    error_str = str(exc)
    for code in (529, 429, 401, 403, 500, 502, 503, 504):
        if str(code) in error_str:
            return code

    return None


def _is_timeout(exc: Exception) -> bool:
    """Check if the exception represents a timeout."""
    if isinstance(exc, (TimeoutError,)):
        return True
    type_name = type(exc).__name__.lower()
    if "timeout" in type_name:
        return True
    return "timeout" in str(exc).lower()


def _is_quota_error(exc: Exception) -> bool:
    """Check if an error is a quota/rate-limit issue."""
    code = _extract_status_code(exc)
    if code in _QUOTA_STATUS_CODES:
        return True
    error_str = str(exc).lower()
    return any(
        kw in error_str
        for kw in ("resource exhausted", "quota", "rate limit", "too many requests")
    )


def _is_auth_error(exc: Exception) -> bool:
    """Check if an error is an authentication/authorization failure."""
    code = _extract_status_code(exc)
    return code in _AUTH_STATUS_CODES


def _is_claude_overloaded(exc: Exception) -> bool:
    """Check if Claude returned a 529 overloaded response."""
    code = _extract_status_code(exc)
    if code == _CLAUDE_OVERLOADED_CODE:
        return True
    error_str = str(exc).lower()
    return "overloaded" in error_str and "529" in str(exc)


def _is_server_error(exc: Exception) -> bool:
    """Check for 5xx server errors (excluding 529)."""
    code = _extract_status_code(exc)
    if code is None:
        return False
    return code in _SERVER_ERROR_CODES


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Rejecting calls
    HALF_OPEN = "half_open"  # Testing one call


class CircuitOpenError(Exception):
    """Raised when calling a service whose circuit is open."""

    def __init__(self, service: str, until: float) -> None:
        remaining = max(0, until - time.monotonic())
        super().__init__(
            f"Circuit breaker OPEN for '{service}' — "
            f"retry in {remaining:.0f}s"
        )
        self.service = service
        self.retry_after = remaining


@dataclass
class _CircuitMetrics:
    """Thread-safe failure tracking for a single service circuit."""

    failure_window_seconds: float = 300.0  # 5 minutes
    failure_threshold: int = 10
    cooldown_seconds: float = 300.0  # 5 minutes
    success_threshold: int = 2  # successes to close from half-open

    # Mutable state (guarded by _lock)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _state: CircuitState = CircuitState.CLOSED
    _failure_timestamps: list[float] = field(default_factory=list)
    _opened_at: float = 0.0
    _half_open_successes: int = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.cooldown_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_successes = 0
                    logger.info("Circuit → HALF_OPEN (cooldown elapsed)")
            return self._state

    def record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_timestamps.clear()
                    logger.info("Circuit → CLOSED (%d consecutive successes)",
                                self.success_threshold)
            # In CLOSED state, just clear old failures
            elif self._state == CircuitState.CLOSED:
                self._prune_old_failures()

    def record_failure(self) -> None:
        with self._lock:
            now = time.monotonic()

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open re-opens
                self._state = CircuitState.OPEN
                self._opened_at = now
                self._half_open_successes = 0
                logger.warning("Circuit → OPEN (failure during HALF_OPEN)")
                return

            self._failure_timestamps.append(now)
            self._prune_old_failures()

            if len(self._failure_timestamps) >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = now
                logger.warning(
                    "Circuit → OPEN (%d failures in %.0fs window)",
                    len(self._failure_timestamps),
                    self.failure_window_seconds,
                )

    def _prune_old_failures(self) -> None:
        """Remove failure timestamps outside the window. Must hold _lock."""
        cutoff = time.monotonic() - self.failure_window_seconds
        self._failure_timestamps = [
            ts for ts in self._failure_timestamps if ts > cutoff
        ]

    def reset(self) -> None:
        """Force reset the circuit to CLOSED (for testing)."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_timestamps.clear()
            self._opened_at = 0.0
            self._half_open_successes = 0


# Global circuit registry
_circuits: dict[str, _CircuitMetrics] = {}
_circuits_lock = threading.Lock()

# Known services
KNOWN_SERVICES = ("gemini", "claude", "zoom", "drive", "whatsapp", "pinecone")


def _get_circuit(service: str) -> _CircuitMetrics:
    """Get or create the circuit breaker for a service."""
    with _circuits_lock:
        if service not in _circuits:
            _circuits[service] = _CircuitMetrics()
        return _circuits[service]


def get_circuit_status() -> dict[str, dict[str, Any]]:
    """Return the current state of all circuit breakers.

    Returns a dict keyed by service name with state and recent failure count.
    Intended for the /health endpoint.
    """
    result: dict[str, dict[str, Any]] = {}
    for svc in KNOWN_SERVICES:
        circuit = _get_circuit(svc)
        state = circuit.state
        with circuit._lock:
            recent_failures = len(circuit._failure_timestamps)
        result[svc] = {
            "state": state.value,
            "recent_failures": recent_failures,
        }
        if state == CircuitState.OPEN:
            remaining = max(
                0, circuit.cooldown_seconds - (time.monotonic() - circuit._opened_at)
            )
            result[svc]["retry_after_seconds"] = round(remaining)
    return result


# ---------------------------------------------------------------------------
# Retry strategies per error type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RetryStrategy:
    """Describes how to handle a specific class of error."""

    should_retry: bool
    delay_seconds: float
    max_attempts: int | None = None  # None means use the decorator default
    alert: bool = False


def _classify_error(exc: Exception) -> _RetryStrategy:
    """Determine the retry strategy for a given exception."""
    if _is_auth_error(exc):
        return _RetryStrategy(should_retry=False, delay_seconds=0, alert=True)

    if _is_claude_overloaded(exc):
        return _RetryStrategy(
            should_retry=True, delay_seconds=30.0, max_attempts=5
        )

    if _is_quota_error(exc):
        return _RetryStrategy(
            should_retry=True, delay_seconds=60.0, max_attempts=3
        )

    if _is_timeout(exc):
        return _RetryStrategy(
            should_retry=True, delay_seconds=0, max_attempts=2
        )

    if _is_server_error(exc):
        return _RetryStrategy(
            should_retry=True, delay_seconds=2.0, max_attempts=3
        )

    # Default: generic error, retry with backoff
    return _RetryStrategy(should_retry=True, delay_seconds=2.0)


# ---------------------------------------------------------------------------
# @resilient_api_call decorator
# ---------------------------------------------------------------------------


def resilient_api_call(
    service: str,
    operation: str = "",
    *,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    alert_on_auth: bool = True,
    gemini_quota_fallback: bool = False,
):
    """Decorator that wraps external API calls with resilience logic.

    Args:
        service: Service name for circuit breaker (e.g. "gemini", "zoom").
        operation: Human-readable operation label for logging.
        max_attempts: Default maximum retry attempts.
        backoff_base: Base seconds for exponential backoff.
        alert_on_auth: Whether to alert operator on auth errors.
        gemini_quota_fallback: If True and service is "gemini", switch to free
            API key on quota errors before retrying.
    """

    def decorator(func):  # noqa: ANN001, ANN202
        op_label = operation or func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            circuit = _get_circuit(service)

            # Check circuit state
            state = circuit.state
            if state == CircuitState.OPEN:
                raise CircuitOpenError(
                    service,
                    circuit._opened_at + circuit.cooldown_seconds,
                )

            effective_max = max_attempts
            last_exc: Exception | None = None
            attempt = 0

            while attempt < effective_max:
                attempt += 1
                try:
                    logger.debug(
                        "[%s:%s] attempt %d/%d",
                        service, op_label, attempt, effective_max,
                    )
                    result = func(*args, **kwargs)
                    circuit.record_success()
                    return result

                except Exception as exc:
                    last_exc = exc
                    strategy = _classify_error(exc)

                    logger.warning(
                        "[%s:%s] attempt %d/%d failed: %s (retry=%s, delay=%.1fs)",
                        service,
                        op_label,
                        attempt,
                        effective_max,
                        exc,
                        strategy.should_retry,
                        strategy.delay_seconds,
                    )

                    # Record failure for circuit breaker
                    circuit.record_failure()

                    # Non-retryable: fail immediately
                    if not strategy.should_retry:
                        if strategy.alert and alert_on_auth:
                            _alert_auth_failure(service, op_label, exc)
                        raise

                    # Gemini quota fallback: inject use_free=True and retry
                    if (
                        gemini_quota_fallback
                        and _is_quota_error(exc)
                        and not kwargs.get("use_free")
                    ):
                        logger.warning(
                            "[%s:%s] quota hit — switching to free Gemini key",
                            service, op_label,
                        )
                        kwargs["use_free"] = True
                        # Don't count this as an attempt
                        attempt -= 1
                        continue

                    # Extend max_attempts if strategy says so
                    # (e.g. 529 Claude overloaded → allow up to 5)
                    if strategy.max_attempts is not None:
                        effective_max = max(effective_max, strategy.max_attempts)

                    # Last attempt exhausted
                    if attempt >= effective_max:
                        break

                    # Calculate delay: strategy-specific or exponential backoff
                    if strategy.delay_seconds > 0:
                        delay = strategy.delay_seconds
                    else:
                        delay = backoff_base * (2 ** (attempt - 1))

                    logger.info(
                        "[%s:%s] waiting %.1fs before retry...",
                        service, op_label, delay,
                    )
                    time.sleep(delay)

            # All retries exhausted
            logger.error(
                "[%s:%s] all %d attempts exhausted",
                service, op_label, effective_max,
            )
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator


def _alert_auth_failure(service: str, operation: str, exc: Exception) -> None:
    """Send an operator alert for authentication failures."""
    try:
        from tools.integrations.whatsapp_sender import alert_operator

        alert_operator(
            f"AUTH FAILURE [{service}:{operation}]: {exc}\n"
            f"Immediate attention required — API key may be invalid or revoked."
        )
    except Exception as alert_err:
        logger.error(
            "Failed to send auth failure alert: %s", alert_err
        )
