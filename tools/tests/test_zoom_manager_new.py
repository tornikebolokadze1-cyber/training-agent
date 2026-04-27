"""Tests for NEW features added to tools/integrations/zoom_manager.py.

Covers:
- list_user_recordings — date defaults, param forwarding, response parsing
- list_user_recordings — empty meeting list from Zoom
- list_user_recordings — passes from_date / to_date to _zoom_request
- ZoomAPIError exception attributes (status_code, message in str)

All network calls are mocked via _zoom_request.

Run with:
    pytest tools/tests/test_zoom_manager_new.py -v
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

import tools.integrations.zoom_manager as zm


# ===========================================================================
# Helpers
# ===========================================================================

def _patch_zoom_request(return_value: dict):
    """Patch _zoom_request to return a fixed dict without network calls.

    Uses patch.object on the module directly (not string path) to ensure
    robustness against httpx stub pollution from other test modules.
    """
    return patch.object(zm, "_zoom_request", return_value=return_value)


@pytest.fixture(autouse=True)
def _mock_zoom_credentials():
    """Ensure tests never attempt real Zoom API calls.

    Also patches get_access_token to prevent real token requests
    when _zoom_request patch is bypassed by other test modules
    polluting sys.modules (test ordering issue).
    """
    with (
        patch.object(zm, "ZOOM_ACCOUNT_ID", "test-account"),
        patch.object(zm, "ZOOM_CLIENT_ID", "test-client"),
        patch.object(zm, "ZOOM_CLIENT_SECRET", "test-secret"),
        patch.object(zm, "get_access_token", return_value="mock-token"),
    ):
        yield


# ===========================================================================
# 1. list_user_recordings
# ===========================================================================


class TestListUserRecordings:
    """list_user_recordings must call _zoom_request with correct params and
    return the meetings list from the response."""

    def test_returns_meetings_list(self):
        fake_response = {
            "meetings": [
                {"uuid": "abc", "id": 123, "topic": "ჯგუფი #1 Lecture 1", "start_time": "2026-03-13T16:00:00Z"},
                {"uuid": "def", "id": 456, "topic": "ჯგუფი #2 Lecture 2", "start_time": "2026-03-16T16:00:00Z"},
            ]
        }
        with _patch_zoom_request(fake_response):
            result = zm.list_user_recordings("2026-03-13", "2026-03-16")

        assert len(result) == 2
        assert result[0]["uuid"] == "abc"
        assert result[1]["id"] == 456

    def test_empty_response_returns_empty_list(self):
        with _patch_zoom_request({"meetings": []}):
            result = zm.list_user_recordings("2026-03-01", "2026-03-01")

        assert result == []

    def test_missing_meetings_key_returns_empty_list(self):
        # Zoom API may return an empty object when no recordings exist
        with _patch_zoom_request({}):
            result = zm.list_user_recordings("2026-03-01", "2026-03-01")

        assert result == []

    def test_default_dates_are_today(self):
        """When from_date / to_date are None, both default to today's ISO date."""
        today = date.today().isoformat()
        captured_params = {}

        def _capture_request(method, path, params=None, **kwargs):
            captured_params.update(params or {})
            return {"meetings": []}

        with patch.object(zm, "_zoom_request", side_effect=_capture_request):
            zm.list_user_recordings()

        assert captured_params.get("from") == today
        assert captured_params.get("to") == today

    def test_explicit_dates_forwarded_to_api(self):
        """Explicit from_date / to_date must be forwarded as query params."""
        captured_params = {}

        def _capture_request(method, path, params=None, **kwargs):
            captured_params.update(params or {})
            return {"meetings": []}

        with patch.object(zm, "_zoom_request", side_effect=_capture_request):
            zm.list_user_recordings("2026-03-10", "2026-03-15")

        assert captured_params["from"] == "2026-03-10"
        assert captured_params["to"] == "2026-03-15"

    def test_page_size_30_sent(self):
        """The API call must request page_size=30 to cover a typical week."""
        captured_params = {}

        def _capture_request(method, path, params=None, **kwargs):
            captured_params.update(params or {})
            return {"meetings": []}

        with patch.object(zm, "_zoom_request", side_effect=_capture_request):
            zm.list_user_recordings("2026-03-01", "2026-03-07")

        assert captured_params.get("page_size") == 30

    def test_calls_users_me_recordings_endpoint(self):
        """Must call the /users/me/recordings endpoint."""
        captured_paths: list[str] = []

        def _capture_request(method, path, params=None, **kwargs):
            captured_paths.append(path)
            return {"meetings": []}

        with patch.object(zm, "_zoom_request", side_effect=_capture_request):
            zm.list_user_recordings("2026-03-13", "2026-03-13")

        assert any("/users/me/recordings" in p for p in captured_paths)

    def test_uses_get_method(self):
        """Must use GET HTTP method."""
        captured_methods: list[str] = []

        def _capture_request(method, path, params=None, **kwargs):
            captured_methods.append(method)
            return {"meetings": []}

        with patch.object(zm, "_zoom_request", side_effect=_capture_request):
            zm.list_user_recordings()

        assert captured_methods == ["GET"]

    def test_returns_list_of_dicts(self):
        meetings = [
            {"uuid": "x1", "id": 1, "topic": "Test", "start_time": "2026-03-13T16:00:00Z"},
        ]
        with _patch_zoom_request({"meetings": meetings}):
            result = zm.list_user_recordings("2026-03-13", "2026-03-13")

        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, dict)

    def test_propagates_zoom_api_error(self):
        """If _zoom_request raises ZoomAPIError, it must propagate."""
        with patch(
            "tools.integrations.zoom_manager._zoom_request",
            side_effect=zm.ZoomAPIError(403, "Forbidden"),
        ):
            with pytest.raises(zm.ZoomAPIError):
                zm.list_user_recordings("2026-03-13", "2026-03-13")

    def test_multiple_meetings_all_returned(self):
        """All meetings in the Zoom response are returned, not just the first."""
        meetings = [
            {"uuid": f"uuid{i}", "id": i, "topic": f"Lecture {i}", "start_time": "2026-03-13T16:00:00Z"}
            for i in range(10)
        ]
        with _patch_zoom_request({"meetings": meetings}):
            result = zm.list_user_recordings("2026-03-01", "2026-03-31")

        assert len(result) == 10


# ===========================================================================
# 2. ZoomAPIError
# ===========================================================================


class TestZoomAPIError:
    """ZoomAPIError must expose status_code and a readable string representation."""

    def test_status_code_attribute(self):
        err = zm.ZoomAPIError(404, "Meeting not found")
        assert err.status_code == 404

    def test_str_contains_status_code(self):
        err = zm.ZoomAPIError(404, "Meeting not found")
        assert "404" in str(err)

    def test_str_contains_message(self):
        err = zm.ZoomAPIError(503, "Service unavailable")
        assert "Service unavailable" in str(err)

    def test_is_exception(self):
        err = zm.ZoomAPIError(500, "Internal error")
        assert isinstance(err, Exception)

    def test_can_be_raised_and_caught(self):
        with pytest.raises(zm.ZoomAPIError) as exc_info:
            raise zm.ZoomAPIError(403, "Forbidden")
        assert exc_info.value.status_code == 403
