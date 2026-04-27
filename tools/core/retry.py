"""Shared retry utility for transient HTTP errors.

Provides a common retry-with-backoff pattern used across multiple modules.
Specialized retry logic (e.g., Zoom token refresh, Gemini polling) remains
inline in their respective modules where the semantics differ materially.
"""

from __future__ import annotations

import copy
import functools
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Defaults matching the most common pattern across the codebase
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE = 2.0  # seconds


def retry_with_backoff(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    retryable_exceptions: tuple[type[BaseException], ...] = (Exception,),
    operation_name: str = "operation",
    **kwargs: Any,
) -> T:
    """Execute a function with exponential backoff retry.

    Args:
        func: Callable to execute.
        *args: Positional arguments for func.
        max_retries: Maximum number of attempts.
        backoff_base: Base for exponential backoff (seconds).
        retryable_exceptions: Exception types to catch and retry.
        operation_name: Human-readable name for logging.
        **kwargs: Keyword arguments for func.

    Returns:
        The return value of func on success.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc: BaseException | None = None

    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                    operation_name,
                    attempt,
                    max_retries,
                    exc,
                    delay,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "%s failed after %d attempts: %s",
                    operation_name,
                    max_retries,
                    exc,
                )

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# safe_operation — decorator to replace try/except/alert_operator boilerplate
# ---------------------------------------------------------------------------

F = TypeVar("F", bound=Callable[..., Any])


def safe_operation(
    operation_name: str,
    *,
    alert: bool = True,
    default: Any = None,
    dlq_operation: str = "",
) -> Callable[[F], F]:
    """Decorator that catches exceptions, logs them, and optionally alerts the operator.

    Replaces the repeated pattern::

        try:
            ...
        except Exception as e:
            logger.error("Failed to ...: %s", e)
            try:
                alert_operator(f"... FAILED: {e}")
            except Exception as alert_err:
                logger.error("alert_operator also failed: %s", alert_err)
            return None

    Usage::

        @safe_operation("Drive summary upload", alert=True)
        def _upload_summary_to_drive(group_number, lecture_number, summary):
            # happy-path only — no try/except needed
            ...

    Args:
        operation_name: Human-readable label for log messages and alerts.
        alert: Whether to call ``alert_operator`` on failure (default True).
            Uses a lazy import to avoid circular dependencies.
        default: Value to return when the wrapped function raises (default None).
        dlq_operation: If set, enqueue to Dead Letter Queue on failure for
            later retry.  The value is used as the DLQ operation name.

    Returns:
        A decorator that wraps the target function with error handling.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                logger.error("Failed to %s: %s", operation_name, exc)
                if alert:
                    try:
                        import tools.integrations.whatsapp_sender as _ws_mod

                        _ws_mod.alert_operator(f"{operation_name} FAILED: {exc}")
                    except Exception as alert_err:
                        logger.error(
                            "alert_operator also failed: %s", alert_err
                        )
                if dlq_operation:
                    try:
                        from tools.core.dlq import enqueue as _dlq_enqueue

                        # Capture the function args as payload
                        _dlq_payload: dict[str, Any] = {
                            "args": [repr(a) for a in args[:3]],
                            "operation": operation_name,
                        }
                        _dlq_enqueue(dlq_operation, _dlq_payload, str(exc))
                    except Exception as dlq_err:
                        logger.debug("DLQ enqueue failed: %s", dlq_err)
                return copy.deepcopy(default)

        return wrapper  # type: ignore[return-value]

    return decorator
