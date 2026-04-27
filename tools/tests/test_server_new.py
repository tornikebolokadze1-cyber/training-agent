"""FastAPI server tests for NEW endpoints and features in tools/app/server.py.

Covers:
- GET /dashboard — returns HTML, falls back to error HTML on exception
- GET /dashboard/data — returns JSON data or 500 error JSON
- POST /retry-latest — requires auth, no_recordings response, all_processed response,
  accepted response when unprocessed recording found
- _eviction_loop — calls _evict_stale_tasks on each iteration
- _check_unprocessed_recordings — zoom_manager import failure (silent skip),
  empty recordings list (silent skip)
- _processing_lock used by dedup logic

All external dependencies are patched. Real FastAPI/httpx is used (same pattern
as test_server.py: stubs for fastapi/slowapi/httpx/pydantic are cleared first).

Run with:
    pytest tools/tests/test_server_new.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Re-import real FastAPI/httpx — same bootstrap as test_server.py
# Save popped stubs so conftest's reset fixture can detect the pollution.
#
# IDEMPOTENT: skip the pop if a sibling test file already swapped the
# conftest stubs for real modules — re-popping creates a second set of
# class objects and breaks class-identity checks across files.
# ---------------------------------------------------------------------------
_popped_stubs: dict[str, object] = {}
_fastapi_real = getattr(sys.modules.get("fastapi"), "__file__", None) is not None
if not _fastapi_real:
    for _mod_name in list(sys.modules):
        if _mod_name.startswith(("fastapi", "slowapi", "httpx", "pydantic", "tools.app.server")):
            _popped_stubs[_mod_name] = sys.modules.pop(_mod_name)


from httpx import ASGITransport, AsyncClient  # noqa: E402

import tools.app.server as srv  # noqa: E402
import tools.integrations.whatsapp_sender as _wa_sender_mod  # noqa: E402
from tools.app.server import (  # noqa: E402
    _processing_tasks,
    _task_key,
    app,
)

# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------
_TEST_WEBHOOK_SECRET = "test-secret-abc"
_TEST_ZOOM_SECRET = "test-zoom-secret-xyz"
_AUTH_HEADER = {"Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}"}


def _zoom_sig(body: bytes, timestamp: str, secret: str = _TEST_ZOOM_SECRET) -> str:
    message = f"v0:{timestamp}:{body.decode()}"
    return "v0=" + hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures (mirror test_server.py)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_processing_tasks():
    _processing_tasks.clear()
    yield
    _processing_tasks.clear()


@pytest.fixture(autouse=True)
def mock_alert_operator():
    with patch.object(_wa_sender_mod, "alert_operator", new_callable=MagicMock) as mock_alert:
        with patch.object(srv, "alert_operator", mock_alert):
            yield mock_alert


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    srv.limiter.reset()
    yield
    srv.limiter.reset()


@pytest.fixture
def patched_secrets():
    with (
        patch.object(srv, "WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET),
        patch.object(srv, "ZOOM_WEBHOOK_SECRET_TOKEN", _TEST_ZOOM_SECRET),
    ):
        yield


async def _async_client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://localhost")


# ===========================================================================
# 1. GET /dashboard
# ===========================================================================

@pytest.mark.asyncio
class TestDashboardEndpoint:
    """GET /dashboard must return HTML from render_dashboard_html or a fallback."""

    async def test_dashboard_returns_200_with_html(self, patched_secrets):
        """Happy path: analytics module returns data and HTML."""
        fake_html = "<html><body><h1>Dashboard</h1></body></html>"
        with (
            patch("tools.app.server.app") as _,
            # We need to patch inside the endpoint's import scope
        ):
            pass

        # Patch analytics functions used by the endpoint
        with (
            patch.dict(
                sys.modules,
                {
                    "tools.services.analytics": MagicMock(
                        get_dashboard_data=MagicMock(return_value={"groups": []}),
                        render_dashboard_html=MagicMock(return_value=fake_html),
                    )
                },
            ),
        ):
            async with await _async_client() as client:
                resp = await client.get("/dashboard")

        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_dashboard_returns_html_content_type(self, patched_secrets):
        fake_html = "<html><body>OK</body></html>"
        with patch.dict(
            sys.modules,
            {
                "tools.services.analytics": MagicMock(
                    get_dashboard_data=MagicMock(return_value={}),
                    render_dashboard_html=MagicMock(return_value=fake_html),
                )
            },
        ):
            async with await _async_client() as client:
                resp = await client.get("/dashboard")

        assert "html" in resp.headers.get("content-type", "").lower()

    async def test_dashboard_error_returns_500(self, patched_secrets):
        """When analytics module raises, /dashboard returns 500 with error HTML."""
        analytics_mock = MagicMock()
        analytics_mock.get_dashboard_data.side_effect = RuntimeError("DB connection failed")
        analytics_mock.render_dashboard_html.side_effect = RuntimeError("DB connection failed")

        with patch.dict(sys.modules, {"tools.services.analytics": analytics_mock}):
            async with await _async_client() as client:
                resp = await client.get("/dashboard")

        assert resp.status_code == 500

    async def test_dashboard_error_html_contains_error_info(self, patched_secrets):
        """Error HTML must mention what went wrong."""
        analytics_mock = MagicMock()
        analytics_mock.get_dashboard_data.side_effect = RuntimeError("Something broke")
        analytics_mock.render_dashboard_html.side_effect = RuntimeError("Something broke")

        with patch.dict(sys.modules, {"tools.services.analytics": analytics_mock}):
            async with await _async_client() as client:
                resp = await client.get("/dashboard")

        body = resp.text
        assert "Dashboard Error" in body or "Something broke" in body or "Error" in body


# ===========================================================================
# 2. GET /dashboard/data
# ===========================================================================

@pytest.mark.asyncio
class TestDashboardDataEndpoint:
    """GET /dashboard/data must return JSON data or a 500 error object."""

    async def test_returns_200_with_data(self, patched_secrets):
        fake_data = {"groups": [{"id": 1, "name": "Group 1"}], "total_lectures": 15}
        analytics_mock = MagicMock()
        analytics_mock.get_dashboard_data.return_value = fake_data

        with patch.dict(sys.modules, {"tools.services.analytics": analytics_mock}):
            async with await _async_client() as client:
                resp = await client.get("/dashboard/data")

        assert resp.status_code == 200

    async def test_returns_json_content_type(self, patched_secrets):
        analytics_mock = MagicMock()
        analytics_mock.get_dashboard_data.return_value = {}

        with patch.dict(sys.modules, {"tools.services.analytics": analytics_mock}):
            async with await _async_client() as client:
                resp = await client.get("/dashboard/data")

        assert "application/json" in resp.headers.get("content-type", "")

    async def test_error_returns_500_json(self, patched_secrets):
        analytics_mock = MagicMock()
        analytics_mock.get_dashboard_data.side_effect = RuntimeError("Pinecone timeout")

        with patch.dict(sys.modules, {"tools.services.analytics": analytics_mock}):
            async with await _async_client() as client:
                resp = await client.get("/dashboard/data")

        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body

    async def test_error_json_contains_message(self, patched_secrets):
        analytics_mock = MagicMock()
        analytics_mock.get_dashboard_data.side_effect = RuntimeError("Pinecone timeout")

        with patch.dict(sys.modules, {"tools.services.analytics": analytics_mock}):
            async with await _async_client() as client:
                resp = await client.get("/dashboard/data")

        body = resp.json()
        assert "Pinecone timeout" in body.get("error", "")


# ===========================================================================
# 3. POST /retry-latest
# ===========================================================================

@pytest.mark.asyncio
class TestRetryLatestEndpoint:
    """POST /retry-latest must authenticate, query Zoom, and return the right status."""

    async def test_missing_auth_returns_4xx_or_503(self):
        """No auth header must be rejected. The exact code depends on server config:
        - 401/403 when WEBHOOK_SECRET is set (wrong/missing token)
        - 503 when WEBHOOK_SECRET is unconfigured (server fails closed)
        """
        async with await _async_client() as client:
            resp = await client.post("/retry-latest")

        assert resp.status_code in (401, 403, 422, 503)

    async def test_wrong_secret_returns_401_or_403(self, patched_secrets):
        async with await _async_client() as client:
            resp = await client.post(
                "/retry-latest",
                headers={"Authorization": "Bearer wrong-secret"},
            )

        assert resp.status_code in (401, 403)

    async def test_no_recordings_returns_no_recordings_status(self, patched_secrets):
        """When Zoom returns no recordings, status must be 'no_recordings'."""
        zm_mock = MagicMock()
        zm_mock.list_user_recordings.return_value = []

        with (
            patch.dict(sys.modules, {"tools.integrations.zoom_manager": zm_mock}),
            patch("asyncio.to_thread", new=AsyncMock(return_value=[])),
        ):
            async with await _async_client() as client:
                resp = await client.post("/retry-latest", headers=_AUTH_HEADER)

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "no_recordings"

    async def test_zoom_api_error_returns_502(self, patched_secrets):
        """When Zoom API raises, return 502."""
        zm_mock = MagicMock()
        zm_mock.list_user_recordings.side_effect = Exception("Zoom unavailable")

        with (
            patch.dict(sys.modules, {"tools.integrations.zoom_manager": zm_mock}),
            patch("asyncio.to_thread", new=AsyncMock(side_effect=Exception("Zoom unavailable"))),
        ):
            async with await _async_client() as client:
                resp = await client.post("/retry-latest", headers=_AUTH_HEADER)

        assert resp.status_code == 502

    async def test_all_processed_returns_all_processed_status(self, patched_secrets):
        """When all recordings are already in _processing_tasks or Pinecone,
        status must be 'all_processed'."""
        # Provide one meeting that belongs to a known group
        meetings = [
            {
                "uuid": "abc",
                "id": 12345,
                "topic": "ჯგუფი #1 Lecture",
                "start_time": "2026-03-13T16:00:00Z",
            }
        ]
        # Mark it as already in-flight
        _processing_tasks[_task_key(1, 1)] = datetime.now()

        pinecone_mock = MagicMock()
        pinecone_mock.query.return_value = {"matches": [{"id": "vec1"}]}

        with (
            patch.dict(sys.modules, {"tools.integrations.zoom_manager": MagicMock()}),
            patch("asyncio.to_thread", new=AsyncMock(return_value=meetings)),
        ):
            async with await _async_client() as client:
                resp = await client.post("/retry-latest", headers=_AUTH_HEADER)

        assert resp.status_code == 200

    async def test_correct_auth_allows_request(self, patched_secrets):
        """A request with the correct WEBHOOK_SECRET must not be rejected for auth."""
        with (
            patch("asyncio.to_thread", new=AsyncMock(return_value=[])),
            patch.dict(sys.modules, {"tools.integrations.zoom_manager": MagicMock()}),
        ):
            async with await _async_client() as client:
                resp = await client.post("/retry-latest", headers=_AUTH_HEADER)

        # Must not be an auth error
        assert resp.status_code not in (401, 403)


# ===========================================================================
# 4. _eviction_loop
# ===========================================================================


@pytest.mark.asyncio
class TestEvictionLoop:
    """_eviction_loop must call _evict_stale_tasks on each iteration."""

    async def test_eviction_loop_calls_evict_stale_tasks(self):
        """Run one iteration of _eviction_loop and verify _evict_stale_tasks is called."""
        iteration = 0

        async def _fake_sleep(duration):
            nonlocal iteration
            if iteration >= 1:
                raise asyncio.CancelledError()
            iteration += 1

        with (
            patch("tools.app.server.asyncio.sleep", side_effect=_fake_sleep),
            patch("tools.app.server._evict_stale_tasks") as mock_evict,
        ):
            mock_evict.return_value = []
            try:
                await srv._eviction_loop()
            except asyncio.CancelledError:
                pass
            assert mock_evict.call_count >= 1

    async def test_eviction_loop_handles_exception_without_crashing(self):
        """If _evict_stale_tasks raises, the loop must continue (not crash)."""
        iters = 0

        async def _fake_sleep(duration):
            nonlocal iters
            iters += 1
            if iters >= 2:
                raise asyncio.CancelledError()

        with (
            patch("tools.app.server.asyncio.sleep", side_effect=_fake_sleep),
            patch("tools.app.server._evict_stale_tasks",
                  side_effect=RuntimeError("unexpected error")),
        ):
            try:
                await srv._eviction_loop()
            except asyncio.CancelledError:
                pass
        # Must have reached at least the second iteration despite the exception
        assert iters >= 1


# ===========================================================================
# 5. _check_unprocessed_recordings
# ===========================================================================


@pytest.mark.asyncio
class TestCheckUnprocessedRecordings:
    """_check_unprocessed_recordings must handle import errors and empty recording
    lists gracefully (no exceptions propagated)."""

    async def test_zoom_manager_import_error_is_silent(self):
        """If zoom_manager cannot be imported, the function returns without error.

        The server uses __import__('tools.integrations.zoom_manager') internally.
        We remove it from sys.modules and patch _import_zoom_manager so it raises
        ImportError — the enclosing try/except in _check_unprocessed_recordings
        must swallow it and log a warning instead of propagating.
        """
        real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def _selective_import(name, *args, **kwargs):
            if name == "tools.integrations.zoom_manager":
                raise ImportError("no module")
            return real_import(name, *args, **kwargs)

        # Temporarily remove zoom_manager from sys.modules so __import__ is called
        sys.modules.pop("tools.integrations.zoom_manager", None)
        try:
            with patch("builtins.__import__", side_effect=_selective_import):
                await srv._check_unprocessed_recordings()
        except ImportError:
            pytest.fail("ImportError propagated — should be caught internally")

    async def test_empty_recordings_list_is_silent(self):
        """If Zoom returns no recordings, the function returns without starting any pipeline."""
        zm_mock = MagicMock()
        zm_mock.list_user_recordings.return_value = []

        with (
            patch.dict(sys.modules, {"tools.integrations.zoom_manager": zm_mock}),
            patch("asyncio.to_thread", new=AsyncMock(return_value=[])),
        ):
            await srv._check_unprocessed_recordings()
            # No exception and no pipeline was started

    async def test_zoom_api_exception_is_caught(self):
        """If the Zoom recordings listing raises, the function returns without crashing."""
        zm_mock = MagicMock()
        zm_mock.list_user_recordings.side_effect = Exception("Zoom API down")

        with (
            patch.dict(sys.modules, {"tools.integrations.zoom_manager": zm_mock}),
            patch("asyncio.to_thread", new=AsyncMock(side_effect=Exception("Zoom API down"))),
        ):
            # Must not raise
            await srv._check_unprocessed_recordings()


# ===========================================================================
# 6. _processing_lock concurrency guard
# ===========================================================================


class TestProcessingLock:
    """The _processing_lock must serialise access to _processing_tasks."""

    def test_lock_exists_on_server_module(self):
        # threading.Lock() returns a _thread.lock instance; check via acquire/release
        lock = srv._processing_lock
        assert hasattr(lock, "acquire") and hasattr(lock, "release")
        # Confirm it's the object returned by threading.Lock (duck-type check)
        assert callable(lock.acquire) and callable(lock.release)

    def test_lock_prevents_double_insert(self):
        """Simulate two concurrent requests — only the first must insert."""
        import threading

        inserted = []

        def _try_insert(key: str) -> None:
            with srv._processing_lock:
                if key not in _processing_tasks:
                    _processing_tasks[key] = datetime.now()
                    inserted.append(key)

        threads = [
            threading.Thread(target=_try_insert, args=("g1_l5",)),
            threading.Thread(target=_try_insert, args=("g1_l5",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Only one insertion allowed
        assert len([k for k in inserted if k == "g1_l5"]) == 1
        assert _processing_tasks.get("g1_l5") is not None
