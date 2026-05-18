"""Tests for the public /healthz endpoint — US-009.

The /healthz endpoint MUST:
- Return 200 with {ok: true, version, timestamp, is_railway}
- Not require authentication (CI deploy gates need anonymous access)
- Not leak any secret, GROUPS config, env state, or internal path

Test pattern mirrors test_server.py: pop stubs so FastAPI/slowapi/httpx are
real, then import the server module fresh.
"""

from __future__ import annotations

import sys

import pytest

# Pop conftest stubs for packages we need real implementations of, and
# also pop tools.app.server so it re-imports with the real FastAPI.
for _mod_name in list(sys.modules):
    if _mod_name.startswith(("fastapi", "slowapi", "httpx", "pydantic", "tools.app.server")):
        sys.modules.pop(_mod_name, None)

from httpx import ASGITransport, AsyncClient  # noqa: E402

from tools.app.server import app  # noqa: E402


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_healthz_returns_200_without_auth():
    """GET /healthz with no Authorization header returns 200 + ok=true."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "version" in body
    assert "timestamp" in body
    assert "is_railway" in body


async def test_healthz_does_not_require_secret():
    """Sending an invalid bearer must NOT cause /healthz to 401/403."""
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer this-is-not-the-real-secret"}
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/healthz", headers=headers)

    # Should still return 200 — /healthz is intentionally unauthenticated.
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_healthz_does_not_leak_secrets():
    """Body must not contain WEBHOOK_SECRET, GROUPS config, or file paths."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/healthz")

    raw = response.text
    # Secrets / config / internal-path leaks we must NOT expose.
    forbidden_substrings = (
        "WEBHOOK_SECRET",
        "ZOOM_CLIENT_SECRET",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "PINECONE_API_KEY",
        "GREEN_API_TOKEN",
        "drive_folder_id",
        "analysis_folder_id",
        "/Users/",
        "/home/",
        "Traceback",
    )
    for s in forbidden_substrings:
        assert s not in raw, f"/healthz body must not contain {s!r}"

    # Sanity: GROUPS dict structure should NOT appear in the response.
    body = response.json()
    # Top-level keys MUST be exactly the four we documented — no extras.
    assert set(body.keys()) == {"ok", "version", "timestamp", "is_railway"}


async def test_healthz_timestamp_is_iso8601():
    """The timestamp field should look like an ISO-8601 UTC string."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/healthz")

    ts = response.json()["timestamp"]
    # Minimal shape check: YYYY-MM-DDTHH:MM:SS...Z or with microseconds.
    assert isinstance(ts, str)
    assert ts.endswith("Z")
    assert "T" in ts
    # Must parse without raising.
    from datetime import datetime
    datetime.fromisoformat(ts.rstrip("Z"))
