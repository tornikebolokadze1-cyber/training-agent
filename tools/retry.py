"""Shared retry utility for transient HTTP errors.

Provides a common retry-with-backoff pattern used across multiple modules.
Specialized retry logic (e.g., Zoom token refresh, Gemini polling) remains
inline in their respective modules where the semantics differ materially.
"""

from __future__ import annotations

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
                delay = backoff_base ** attempt
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
