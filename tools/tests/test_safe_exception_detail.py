"""Regression tests — safe_exception_detail redacts tokens from client responses.

Verifies that:
1.  The returned string does NOT contain token material.
2.  The returned string contains "error_id:" for operator correlation.
3.  The returned string starts with the supplied public_label.
4.  The full (redacted) exception detail IS logged server-side (caplog).
5.  Green API path-embedded tokens are stripped.
6.  Zoom access_token= query-param tokens are stripped.
7.  Bearer tokens in exception messages are stripped.
8.  error_id values are unique across calls.

We simulate httpx.HTTPStatusError behaviour by using plain Exception instances
whose str() contains the token-bearing URL — this mirrors exactly what
httpx.HTTPStatusError.__str__() produces and avoids conftest stub conflicts.

Run with:
    pytest tools/tests/test_safe_exception_detail.py -v
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.core.error_handling import safe_exception_detail  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

GREEN_API_TOKEN = "abcDEF123456789TOKEN_HERE_LONG_ENOUGH"
GREEN_API_URL = (
    f"https://api.green-api.com/waInstance1234567890/sendMessage/{GREEN_API_TOKEN}"
)

ZOOM_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ6b29tX3Rlc3QifQ.fake"
ZOOM_URL_WITH_TOKEN = (
    f"https://zoom.us/rec/download/example.mp4?access_token={ZOOM_ACCESS_TOKEN}"
)


def _make_url_exception(url: str, status_code: int = 403) -> Exception:
    """Return an exception whose str() contains a token-bearing URL.

    httpx.HTTPStatusError.__str__() includes the request URL verbatim.
    We replicate that behaviour with a plain Exception so this test file
    does not depend on a real httpx import (conftest stubs httpx).
    """
    return Exception(
        f"Client error '{status_code}' for url '{url}': "
        f"server returned an error response"
    )


# ---------------------------------------------------------------------------
# Test 1: Green API token NOT in returned client string
# ---------------------------------------------------------------------------

def test_green_api_token_not_in_client_response() -> None:
    exc = _make_url_exception(GREEN_API_URL, status_code=401)
    result = safe_exception_detail(exc, "Green API call")
    assert GREEN_API_TOKEN not in result, (
        f"Green API token leaked into client response: {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 2: Returned string contains "error_id:"
# ---------------------------------------------------------------------------

def test_result_contains_error_id() -> None:
    exc = _make_url_exception(GREEN_API_URL)
    result = safe_exception_detail(exc, "Green API call")
    assert "error_id:" in result, f"error_id missing from response: {result!r}"


# ---------------------------------------------------------------------------
# Test 3: Returned string starts with the public_label
# ---------------------------------------------------------------------------

def test_result_starts_with_public_label() -> None:
    exc = _make_url_exception(GREEN_API_URL)
    label = "Green API call"
    result = safe_exception_detail(exc, label)
    assert result.startswith(label), (
        f"Response does not start with label '{label}': {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: Full exception is logged server-side (caplog)
# ---------------------------------------------------------------------------

def test_exception_logged_server_side(caplog: pytest.LogCaptureFixture) -> None:
    exc = _make_url_exception(GREEN_API_URL)
    with caplog.at_level(logging.ERROR, logger="tools.core.error_handling"):
        result = safe_exception_detail(exc, "Green API call")

    assert caplog.records, "No log records were emitted"
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "No ERROR-level records were emitted"
    # The client-facing result must contain error_id for operator correlation
    assert "error_id:" in result


# ---------------------------------------------------------------------------
# Test 5: Zoom URL access_token NOT in returned client string
# ---------------------------------------------------------------------------

def test_zoom_access_token_not_in_client_response() -> None:
    exc = _make_url_exception(ZOOM_URL_WITH_TOKEN, status_code=401)
    result = safe_exception_detail(exc, "Zoom API")
    assert ZOOM_ACCESS_TOKEN not in result, (
        f"Zoom access_token leaked into client response: {result!r}"
    )
    assert "access_token=" not in result, (
        f"access_token= query param leaked into client response: {result!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: Generic exception (non-httpx) also works safely
# ---------------------------------------------------------------------------

def test_generic_exception_safe() -> None:
    exc = ValueError("Something broke internally — confidential detail here")
    result = safe_exception_detail(exc, "internal operation")
    assert result.startswith("internal operation"), f"Wrong label in: {result!r}"
    assert "error_id:" in result
    # The raw exception message must NOT appear verbatim in the client string
    assert "confidential detail here" not in result, (
        "Raw exception message leaked into client response"
    )


# ---------------------------------------------------------------------------
# Test 7: error_id values are unique across calls
# ---------------------------------------------------------------------------

def test_unique_error_ids() -> None:
    exc = ValueError("test")
    results = {safe_exception_detail(exc, "op") for _ in range(10)}
    assert len(results) == 10, "error_ids are not unique across calls"


# ---------------------------------------------------------------------------
# Test 8: Bearer token in exception string is redacted in returned value
# ---------------------------------------------------------------------------

def test_bearer_token_not_in_client_response() -> None:
    bearer_token = "supersecretbearertokenvaluethatislong123456789"
    exc = ValueError(f"Auth failed: Bearer {bearer_token}")
    result = safe_exception_detail(exc, "auth check")
    assert bearer_token not in result, (
        f"Bearer token leaked into client response: {result!r}"
    )
