"""Hardening tests for admin endpoints — US-019 + US-020 from ralph 2026-05-13.

Covers:
- US-019 (rate-limit decorators): every admin endpoint must carry
  ``@limiter.limit(...)``; the 9-endpoint module-level docstring claim
  "rate-limited to 5/min" was a lie until this story landed.
- US-020 (backfill size cap): ``POST /admin/backfill-deep-analysis`` must
  reject requests whose total queued items exceed ``MAX_BACKFILL_ITEMS``
  (default 15, env-overrideable).  Protects against API-bill DoS from a
  paste-twice operator typo.

Run with:
    pytest tools/tests/test_admin_routes_hardening.py -v
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module import strategy.
#
# This test file follows the SAME pop-and-reimport pattern as
# ``test_admin_routes.py`` so that fastapi/slowapi/etc. resolve to their
# real implementations (not the conftest MagicMock stubs).
#
# Per memory note ``feedback_test_sys_modules_pop_pollutes.md``: popping
# conftest-stubbed modules can pollute tests that run alphabetically after
# this file.  We MUST share the SAME re-imported module instances as
# ``test_admin_routes.py`` so the two files see the same ``_processing_tasks``
# dict and the same FastAPI ``app``.  Strategy: if those modules are ALREADY
# real (a previous test file already popped+reimported), do nothing.
# Otherwise pop+reimport here.  Either way, both files end up bound to
# whichever real module copy got loaded first.
# ---------------------------------------------------------------------------


def _is_real_module(name: str) -> bool:
    """Return True if ``name`` is in sys.modules AND has a real ``__file__``
    (the conftest stubs are ``types.ModuleType`` instances with no file)."""
    mod = sys.modules.get(name)
    if mod is None:
        return False
    return getattr(mod, "__file__", None) is not None


_NEED_REAL = (
    "fastapi",
    "slowapi",
    "httpx",
    "pydantic",
)

# Only pop+reimport if any required module is still a stub.  This avoids
# clobbering bindings captured by test_admin_routes.py (which collects before
# us alphabetically and may have already done the pop+reimport dance).
if not all(_is_real_module(m) for m in _NEED_REAL):
    for _mod_name in list(sys.modules):
        if _mod_name.startswith(
            (
                "fastapi",
                "slowapi",
                "httpx",
                "pydantic",
                "tools.app.server",
                "tools.app.admin_routes",
            )
        ):
            sys.modules.pop(_mod_name, None)


from httpx import ASGITransport, AsyncClient  # noqa: E402

# Import order matters: server.py defines ``limiter`` then imports
# admin_routes at the end of its module body.  Importing server FIRST
# ensures limiter is defined before admin_routes.py's top-level
# ``from tools.app.server import limiter`` runs.
import tools.app.server as srv  # noqa: E402
from tools.app.server import app  # noqa: E402
import tools.app.admin_routes as admin_routes_mod  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_WEBHOOK_SECRET = "test-secret-xyz"
_AUTH_HEADER = {"Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset slowapi token buckets before/after every test so prior calls
    don't push us over the 5/min admin limit during the test run."""
    srv.limiter.reset()
    yield
    srv.limiter.reset()


@pytest.fixture
def patched_secrets():
    """Patch WEBHOOK_SECRET on the live server module bound to admin_routes.

    Mirrors the pattern in test_admin_routes.py: if another test file has
    already popped+reimported ``tools.app.server`` after us, the ``srv`` name
    we captured at the top points at a stale module — we patch BOTH if so.
    """
    live_srv = sys.modules.get("tools.app.server", srv)
    with patch.object(srv, "WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET):
        if live_srv is not srv:
            with patch.object(live_srv, "WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET):
                yield
        else:
            yield


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://localhost")


# ===========================================================================
# US-019 — rate-limit decorators on every admin endpoint
# ===========================================================================


# All admin endpoints we expect to be rate-limited.  Sourced from the
# ``@admin_router.*`` decorators in admin_routes.py (verified via grep).
_EXPECTED_ADMIN_ENDPOINTS: list[tuple[str, str]] = [
    ("POST", "/admin/retry-lecture"),
    ("POST", "/admin/reset-pipeline"),
    ("GET", "/admin/lecture-status"),
    ("POST", "/admin/force-refresh-token"),
    ("GET", "/admin/system-report"),
    ("POST", "/admin/backfill-deep-analysis"),
    ("GET", "/admin/whatsapp-webhook-status"),
    ("POST", "/admin/whatsapp-webhook-repair"),
    ("POST", "/admin/whatsapp-catchup"),
]


def _route_limit_names(limiter) -> set[str]:
    """Return the set of qualified function names slowapi has registered limits for."""
    return set(limiter._route_limits.keys())


def test_each_admin_endpoint_has_rate_limit():
    """Every admin endpoint must have ``@limiter.limit`` applied.

    Inspects ``srv.limiter._route_limits`` (slowapi's internal mapping of
    decorated function names → Limit objects) and asserts every endpoint
    function defined in admin_routes is present.
    """
    limited_names = _route_limit_names(srv.limiter)

    # Build the set of admin endpoint function names by walking FastAPI's
    # route registry and matching prefix /admin/.
    admin_routes_seen: list[tuple[str, str, str]] = []  # (method, path, endpoint_name)
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/admin/"):
            continue
        methods = getattr(route, "methods", set()) or set()
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        qualname = f"{endpoint.__module__}.{endpoint.__qualname__}"
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            admin_routes_seen.append((method, path, qualname))

    # Cross-check: every expected (method, path) shows up in the registry.
    expected_paths = {(m, p) for m, p in _EXPECTED_ADMIN_ENDPOINTS}
    actual_paths = {(m, p) for m, p, _ in admin_routes_seen}
    missing = expected_paths - actual_paths
    assert not missing, (
        f"Expected admin endpoints missing from FastAPI route registry: {missing}"
    )

    # Now the actual assertion: every admin endpoint function must be in
    # slowapi's _route_limits.
    unlimited: list[str] = []
    for method, path, qualname in admin_routes_seen:
        if qualname not in limited_names:
            unlimited.append(f"{method} {path} ({qualname})")
    assert not unlimited, (
        "Admin endpoints lacking @limiter.limit decorator (US-019):\n  "
        + "\n  ".join(unlimited)
    )


def test_post_endpoints_use_write_rate_limit():
    """POST admin endpoints should be limited at 5/minute (mutating ops),
    GET admin endpoints at 20/minute (read-only).  We don't assert exact
    values per-endpoint here — the previous test already proves a limit
    exists; this one sanity-checks the policy by reading the Limit string.
    """
    # Map qualified function name → list of Limit objects
    route_limits = srv.limiter._route_limits

    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/admin/"):
            continue
        methods = getattr(route, "methods", set()) or set()
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        qualname = f"{endpoint.__module__}.{endpoint.__qualname__}"
        limits = route_limits.get(qualname, [])
        assert limits, f"no limit found for {qualname}"

        # Each Limit has a .limit attribute which holds the parsed
        # RateLimitItem (e.g. "5/minute").  We accept either 5/min for
        # writes or 20/min for reads.
        limit_strs = [str(lim.limit) for lim in limits]
        has_post = "POST" in methods
        has_get = "GET" in methods

        if has_post:
            assert any("5" in s for s in limit_strs), (
                f"POST {path} ({qualname}) should be 5/min, got {limit_strs}"
            )
        elif has_get:
            assert any("20" in s for s in limit_strs), (
                f"GET {path} ({qualname}) should be 20/min, got {limit_strs}"
            )


# ===========================================================================
# US-020 — backfill size cap
# ===========================================================================


def _make_lectures(n: int) -> list[str]:
    """Build n valid lecture keys (g1_l1..g1_l15, then g2_l1..., wrapping cohorts)."""
    keys: list[str] = []
    groups = sorted((admin_routes_mod._configured_group_numbers() or [1, 2]))
    if not groups:
        groups = [1, 2]
    g_idx = 0
    lec = 1
    while len(keys) < n:
        keys.append(f"g{groups[g_idx]}_l{lec}")
        lec += 1
        if lec > 15:
            lec = 1
            g_idx = (g_idx + 1) % len(groups)
    return keys


@pytest.mark.asyncio
async def test_backfill_rejects_oversized_request(patched_secrets):
    """POST 16 items with the default cap of 15 — expect 400 + Georgian message."""
    payload = {"lectures": _make_lectures(16)}
    async with await _client() as c:
        resp = await c.post(
            "/admin/backfill-deep-analysis",
            json=payload,
            headers=_AUTH_HEADER,
        )
    assert resp.status_code == 400, (
        f"expected 400 for oversized request, got {resp.status_code}: {resp.text}"
    )
    detail = resp.json().get("detail", "")
    # Georgian message text (see admin_routes.py US-020 cap)
    assert "მოთხოვნა" in detail, f"missing Georgian DoS message in detail: {detail!r}"
    assert "MAX_BACKFILL_ITEMS" in detail, (
        f"missing MAX_BACKFILL_ITEMS reference in detail: {detail!r}"
    )


@pytest.mark.asyncio
async def test_backfill_accepts_at_limit(patched_secrets, monkeypatch):
    """POST exactly 15 items — expect 202 (accepted, queued).

    We patch out the background-task spawn and Pinecone reconstruction so
    the endpoint returns quickly without touching real services.
    """

    # Stub the background sync function so no real work happens
    monkeypatch.setattr(
        admin_routes_mod,
        "_run_backfill_sync",
        lambda *a, **kw: None,
    )

    payload = {"lectures": _make_lectures(15)}
    async with await _client() as c:
        resp = await c.post(
            "/admin/backfill-deep-analysis",
            json=payload,
            headers=_AUTH_HEADER,
        )
    assert resp.status_code in (200, 202), (
        f"expected 202 for at-limit request, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("total_queued") == 15
    assert body.get("status") == "accepted"


@pytest.mark.asyncio
async def test_backfill_cap_configurable_via_env(patched_secrets, monkeypatch):
    """Setting MAX_BACKFILL_ITEMS=3 should reject a 4-item request."""
    monkeypatch.setenv("MAX_BACKFILL_ITEMS", "3")

    payload = {"lectures": _make_lectures(4)}
    async with await _client() as c:
        resp = await c.post(
            "/admin/backfill-deep-analysis",
            json=payload,
            headers=_AUTH_HEADER,
        )
    assert resp.status_code == 400, (
        f"expected 400 with MAX_BACKFILL_ITEMS=3, got {resp.status_code}: {resp.text}"
    )
    detail = resp.json().get("detail", "")
    assert "MAX_BACKFILL_ITEMS=3" in detail, (
        f"expected the env-set cap to appear in the error message, got {detail!r}"
    )


@pytest.mark.asyncio
async def test_backfill_cap_counts_across_all_three_lists(patched_secrets):
    """The cap covers ``lectures`` + ``reprocess`` + ``full_rebuild`` combined,
    not each list individually.  10 + 10 = 20 should still fail with the default
    cap of 15."""
    payload = {
        "lectures": _make_lectures(10),
        "reprocess": _make_lectures(5)[5:] + _make_lectures(10)[:5],  # different keys
        "full_rebuild": [],
    }
    # Simpler: just split _make_lectures(20) across the two fields.
    keys = _make_lectures(20)
    payload = {
        "lectures": keys[:10],
        "reprocess": keys[10:],
        "full_rebuild": [],
    }
    async with await _client() as c:
        resp = await c.post(
            "/admin/backfill-deep-analysis",
            json=payload,
            headers=_AUTH_HEADER,
        )
    assert resp.status_code == 400, (
        f"combined-list cap should reject 20 items, got {resp.status_code}: {resp.text}"
    )
    detail = resp.json().get("detail", "")
    assert "20" in detail, f"expected total count 20 in detail, got {detail!r}"
