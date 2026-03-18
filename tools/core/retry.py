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
from typing import Any, Callable, TypeVar

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
                        from tools.whatsapp_sender import alert_operator

                        alert_operator(f"{operation_name} FAILED: {exc}")
                    except Exception as alert_err:
                        logger.error(
                            "alert_operator also failed: %s", alert_err
                        )
                return copy.deepcopy(default)

        return wrapper  # type: ignore[return-value]

    return decorator
