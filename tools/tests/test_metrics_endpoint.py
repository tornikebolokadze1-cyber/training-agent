"""Tests for the public /metrics endpoint — US-026.

The /metrics endpoint MUST:
- Return 200 with Prometheus text exposition format
- Not require authentication (Prometheus scrapers can't attach bearer tokens)
- Expose http_requests_total, http_request_duration_seconds,
  whatsapp_messages_sent_total, pipeline_runs_total
- Aggregate by ROUTE TEMPLATE (not URL path), so UUID-like path params do
  not blow up cardinality. (If raw request.url.path were used, every
  /process-recording with a different correlation header would produce
  a distinct label set.)

Pattern mirrors test_healthz_endpoint.py: pop stubs so FastAPI/slowapi/httpx
are real, then import the server module fresh so the prometheus_client
metrics + middleware are registered against the real FastAPI app.
"""

from __future__ import annotations

import sys

import pytest

# Pop conftest stubs for packages we need real implementations of, and
# also pop tools.app.server so it re-imports cleanly with the real stack.
for _mod_name in list(sys.modules):
    if _mod_name.startswith(
        ("fastapi", "slowapi", "httpx", "pydantic", "tools.app.server")
    ):
        sys.modules.pop(_mod_name, None)

from httpx import ASGITransport, AsyncClient  # noqa: E402

from tools.app.server import app  # noqa: E402
from tools.integrations.whatsapp_sender import WHATSAPP_SENT  # noqa: E402
from tools.app.scheduler import PIPELINE_RUNS  # noqa: E402


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_metrics_returns_200_without_auth() -> None:
    """GET /metrics with no Authorization header returns 200 + text/plain."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    # Prometheus exposition format is text/plain; version=0.0.4
    assert response.headers["content-type"].startswith("text/plain")


async def test_metrics_contains_http_requests_total() -> None:
    """After one prior GET, /metrics body contains http_requests_total."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        # Drive the middleware via a known-good public endpoint
        await client.get("/healthz")
        response = await client.get("/metrics")

    body = response.text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


async def test_metrics_contains_whatsapp_sent() -> None:
    """After incrementing WHATSAPP_SENT once, the metric appears in /metrics."""
    WHATSAPP_SENT.labels(result="sent").inc()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/metrics")

    body = response.text
    assert "whatsapp_messages_sent_total" in body
    # Label must be present at least once (value > 0 for result=sent).
    assert 'result="sent"' in body


async def test_metrics_contains_pipeline_runs() -> None:
    """After incrementing PIPELINE_RUNS once, the metric appears in /metrics."""
    PIPELINE_RUNS.labels(state="started").inc()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        response = await client.get("/metrics")

    body = response.text
    assert "pipeline_runs_total" in body
    assert 'state="started"' in body


async def test_metrics_does_not_leak_route_params() -> None:
    """Cardinality guard: route label is the matched template, not the raw URL.

    A request to a path that does NOT match any registered FastAPI route
    must NOT appear as its raw URL in the metric output — otherwise an
    attacker (or a buggy client) could explode metric cardinality by
    spraying unique URL paths. Such requests must land on
    ``route="unknown"`` (or simply not appear with their literal path).
    """
    # A UUID-like path no FastAPI route claims:
    unmatched_path = "/should-not-be-a-route-abc123-def456-uuidlike"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://localhost") as client:
        await client.get(unmatched_path)
        response = await client.get("/metrics")

    body = response.text
    # The literal unmatched path must not appear as a route label value.
    assert unmatched_path not in body, (
        f"/metrics leaked raw URL path {unmatched_path!r} into a label — "
        "this would let any client explode cardinality."
    )
