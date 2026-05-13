"""Secure error handling utilities for the Training Agent system.

Provides helpers that log full exception details server-side while returning
sanitised, token-free strings safe to include in HTTP response bodies.
"""

from __future__ import annotations

import logging
import re
import uuid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patterns that must never appear in client-facing strings
# ---------------------------------------------------------------------------

_REDACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Bearer tokens (e.g. Authorization header values in logged URLs)
    ("Bearer", re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)),
    # URL query-param tokens (access_token=..., token=...)
    ("access_token", re.compile(r"[?&](?:access_token|token)=[^&\s\"']+", re.IGNORECASE)),
    # Green API tokens embedded in URL paths: /waInstanceXXX/sendMessage/<TOKEN>
    # Green API paths look like /waInstance1234567890/sendMessage/ABCDEF01234...
    ("green_api_path_token", re.compile(
        r"/waInstance\d+/\w+/[A-Za-z0-9]{20,}", re.IGNORECASE
    )),
    # Generic long tokens after a colon or slash (40+ hex/base64 chars)
    ("long_token", re.compile(r"(?<=[:/])[A-Za-z0-9_\-]{40,}")),
]


def _redact(text: str) -> str:
    """Replace known sensitive patterns in *text* with [REDACTED]."""
    for label, pattern in _REDACT_PATTERNS:
        text = pattern.sub(f"[REDACTED:{label}]", text)
    return text


def safe_exception_detail(exc: BaseException, public_label: str) -> str:
    """Return a sanitised client-facing error string and log the full detail.

    Guarantees that no token-bearing URL or credential is returned to the
    HTTP client. The full exception string (after basic redaction) is logged
    server-side together with a unique error_id so operators can correlate
    client errors with server logs.

    Args:
        exc: The caught exception.  May be any :class:`BaseException` subclass,
            including ``httpx.HTTPStatusError`` whose ``__str__`` embeds the
            full request URL.
        public_label: Short human-readable label for the operation that failed
            (e.g. ``"Green API call"``). Included verbatim in the returned
            string and in the log record.

    Returns:
        A string of the form ``"<public_label> failed (error_id: <uuid4>)"``
        that is safe to include in an ``HTTPException(detail=...)`` body.

    Example::

        try:
            result = call_green_api(...)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=safe_exception_detail(exc, "Green API call"),
            )
    """
    error_id = str(uuid.uuid4())
    exc_type = type(exc).__name__
    exc_str_raw = str(exc)
    exc_str_redacted = _redact(exc_str_raw)

    logger.error(
        "External call failed",
        extra={
            "error_id": error_id,
            "public_label": public_label,
            "exc_type": exc_type,
            "exc_detail": exc_str_redacted,
        },
        exc_info=exc,
    )

    return f"{public_label} failed (error_id: {error_id})"


__all__: list[str] = ["safe_exception_detail"]
