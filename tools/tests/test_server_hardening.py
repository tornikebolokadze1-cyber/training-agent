"""Server hardening tests — US-013, US-018, US-021 (ralph 2026-05-13).

Covers:
- US-013: operator-visible HTTP responses use ``_g_label`` cohort names
  (e.g. ``მარტის ჯგუფი #1``) instead of bare ``Group N``.
- US-018: rate-limit key prefers first hop in ``X-Forwarded-For`` so the
  Railway proxy IP does not collapse every external client into one bucket.
- US-021: every response carries a ``Permissions-Policy`` header; the
  ``Strict-Transport-Security`` header is set only when running on Railway.

Test pattern mirrors test_server.py / test_healthz_endpoint.py: pop the
conftest stubs for FastAPI / slowapi / httpx / pydantic and reimport the
server module so it picks up the real implementations.

Run with:
    python -m pytest tools/tests/test_server_hardening.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Pop conftest stubs so FastAPI/slowapi/httpx/pydantic load real modules,
# then re-import server.py against them.
for _mod_name in list(sys.modules):
    if _mod_name.startswith(("fastapi", "slowapi", "httpx", "pydantic", "tools.app.server")):
        sys.modules.pop(_mod_name, None)

from httpx import ASGITransport, AsyncClient  # noqa: E402

import tools.app.server as srv  # noqa: E402
from tools.app.server import (  # noqa: E402
    _client_ip_key,
    _processing_tasks,
    _task_key,
    app,
)


_TEST_WEBHOOK_SECRET = "test-secret-hardening"
_AUTH_HEADER = {"Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}"}


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset slowapi counters between tests so per-IP buckets don't leak."""
    srv.limiter.reset()
    yield
    srv.limiter.reset()


@pytest.fixture
def patched_secret():
    """Patch WEBHOOK_SECRET on the server module."""
    with patch.object(srv, "WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET):
        yield


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://localhost")


# ===========================================================================
# US-013 — cohort labels in HTTP responses
# ===========================================================================


@pytest.mark.asyncio
async def test_cohort_label_in_409_response(patched_secret):
    """409 detail must show cohort name (e.g. 'მარტის ჯგუფი') not 'Group 1'.

    Pre-seed _processing_tasks so the dedup path fires deterministically.
    """
    payload = {
        "download_url": "https://zoom.us/rec/download/test.mp4",
        "access_token": "tok",
        "group_number": 1,
        "lecture_number": 7,
        "drive_folder_id": "drive_folder_abc",
    }
    key = _task_key(payload["group_number"], payload["lecture_number"])
    _processing_tasks[key] = datetime.now()

    try:
        async with await _client() as client:
            resp = await client.post(
                "/process-recording",
                json=payload,
                headers=_AUTH_HEADER,
            )
    finally:
        _processing_tasks.pop(key, None)

    assert resp.status_code == 409
    detail = resp.json().get("detail", "")
    # Either configured cohort label appears, OR the legacy "ჯგუფი #N" fallback.
    assert "ჯგუფი" in detail, (
        f"expected Georgian cohort label in 409 detail, got: {detail!r}"
    )
    # And the bare ``Group 1`` form must NOT appear.
    assert "Group 1" not in detail


# ===========================================================================
# US-018 — _client_ip_key behaviour
# ===========================================================================


def _mock_request(xff: str | None, client_host: str = "10.0.0.1"):
    """Build a minimal request-like object: only .headers and .client are read."""
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    # slowapi's get_remote_address reads request.client.host.
    client = SimpleNamespace(host=client_host)
    return SimpleNamespace(headers=headers, client=client)


def test_xff_key_uses_first_hop():
    """First IP in X-Forwarded-For wins over later hops and over client.host."""
    req = _mock_request("1.2.3.4, 5.6.7.8, 9.10.11.12", client_host="172.16.0.99")
    assert _client_ip_key(req) == "1.2.3.4"


def test_xff_key_uses_single_xff_value():
    """A single XFF value (no comma) is returned verbatim, trimmed."""
    req = _mock_request("  8.8.8.8  ", client_host="172.16.0.99")
    assert _client_ip_key(req) == "8.8.8.8"


def test_xff_key_handles_empty_header():
    """No XFF header at all → fall back to client.host via get_remote_address."""
    req = _mock_request(None, client_host="10.0.0.7")
    assert _client_ip_key(req) == "10.0.0.7"


def test_xff_key_handles_malformed():
    """XFF that is just commas/whitespace falls back to client.host."""
    req = _mock_request(", , ", client_host="10.0.0.8")
    # First split element is "" — falsy, so we fall through.
    assert _client_ip_key(req) == "10.0.0.8"


def test_xff_key_handles_empty_string():
    """XFF set to the empty string also falls back."""
    req = _mock_request("", client_host="10.0.0.9")
    assert _client_ip_key(req) == "10.0.0.9"


# ===========================================================================
# US-021 — HSTS + Permissions-Policy headers
# ===========================================================================


@pytest.mark.asyncio
async def test_permissions_policy_header_present():
    """Permissions-Policy must be set on every response, regardless of env."""
    async with await _client() as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 200
    pp = resp.headers.get("Permissions-Policy", "")
    # All four powerful features must be explicitly disabled.
    for token in ("camera=()", "microphone=()", "geolocation=()", "payment=()"):
        assert token in pp, f"Permissions-Policy missing {token!r}; got: {pp!r}"


@pytest.mark.asyncio
async def test_hsts_header_present_on_railway():
    """When IS_RAILWAY is true, Strict-Transport-Security is set."""
    with patch.object(srv, "IS_RAILWAY", True):
        async with await _client() as client:
            resp = await client.get("/healthz")

    assert resp.status_code == 200
    hsts = resp.headers.get("Strict-Transport-Security", "")
    assert "max-age=31536000" in hsts
    assert "includeSubDomains" in hsts
    assert "preload" in hsts


@pytest.mark.asyncio
async def test_hsts_header_absent_off_railway():
    """When IS_RAILWAY is false, HSTS must NOT be sent (local dev safety)."""
    with patch.object(srv, "IS_RAILWAY", False):
        async with await _client() as client:
            resp = await client.get("/healthz")

    assert resp.status_code == 200
    # Either the header is absent, or it must not preload.
    assert "Strict-Transport-Security" not in resp.headers, (
        "HSTS leaked into local dev response — preload would poison the browser"
    )
