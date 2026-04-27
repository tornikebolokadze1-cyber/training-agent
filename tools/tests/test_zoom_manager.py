"""Unit tests for tools/zoom_manager.py.

Covers:
- get_access_token: caching, credential validation, retry on failure
- _zoom_request: retry logic, 401 token refresh, error handling
- create_meeting: payload structure, timezone normalization
- get_meeting_recordings: response parsing
- download_recording: streaming, error handling
- ZoomAuthError / ZoomAPIError / ZoomDownloadError exceptions

Run with:
    pytest tools/tests/test_zoom_manager.py -v
"""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.integrations.zoom_manager as zm

# ===========================================================================
# Helpers
# ===========================================================================

def _reset_token_cache():
    zm._token_cache.clear()


# ===========================================================================
# 1. Exception classes
# ===========================================================================


class TestExceptions:
    def test_zoom_auth_error(self):
        err = zm.ZoomAuthError("bad creds")
        assert "bad creds" in str(err)

    def test_zoom_api_error_stores_status_code(self):
        err = zm.ZoomAPIError(404, "not found")
        assert err.status_code == 404
        assert "404" in str(err)

    def test_zoom_download_error(self):
        err = zm.ZoomDownloadError("download failed")
        assert "download failed" in str(err)


# ===========================================================================
# 2. get_access_token — caching and validation
# ===========================================================================


class TestGetAccessToken:
    def setup_method(self):
        _reset_token_cache()

    def test_raises_when_credentials_missing(self):
        with patch.object(zm, "ZOOM_ACCOUNT_ID", ""), \
             patch.object(zm, "ZOOM_CLIENT_ID", ""), \
             patch.object(zm, "ZOOM_CLIENT_SECRET", ""):
            with pytest.raises(zm.ZoomAuthError, match="Missing"):
                zm.get_access_token()

    def test_returns_cached_token_when_valid(self):
        zm._token_cache["access_token"] = "cached-token"
        zm._token_cache["expires_at"] = time.time() + 3600  # expires in 1 hour

        with patch.object(zm, "ZOOM_ACCOUNT_ID", "acc"), \
             patch.object(zm, "ZOOM_CLIENT_ID", "cid"), \
             patch.object(zm, "ZOOM_CLIENT_SECRET", "secret"):
            token = zm.get_access_token()

        assert token == "cached-token"

    def test_fetches_new_token_when_cache_expired(self):
        zm._token_cache["access_token"] = "old-token"
        zm._token_cache["expires_at"] = time.time() - 100  # expired

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new-token",
            "expires_in": 3600,
        }

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch.object(zm, "ZOOM_ACCOUNT_ID", "acc"), \
             patch.object(zm, "ZOOM_CLIENT_ID", "cid"), \
             patch.object(zm, "ZOOM_CLIENT_SECRET", "secret"), \
             patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client):
            token = zm.get_access_token()

        assert token == "new-token"

    def test_retries_on_failure_then_raises(self):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Server Error"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch.object(zm, "ZOOM_ACCOUNT_ID", "acc"), \
             patch.object(zm, "ZOOM_CLIENT_ID", "cid"), \
             patch.object(zm, "ZOOM_CLIENT_SECRET", "secret"), \
             patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client), \
             patch("tools.integrations.zoom_manager.time.sleep"):
            with pytest.raises(zm.ZoomAuthError, match="Failed to obtain"):
                zm.get_access_token()

        assert mock_client.post.call_count == zm.MAX_RETRIES


# ===========================================================================
# 3. _zoom_request — retry and 401 handling
# ===========================================================================


class TestZoomRequest:
    def setup_method(self):
        _reset_token_cache()

    def test_success_returns_json(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": "ok"}

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response

        with patch.object(zm, "get_access_token", return_value="tok"), \
             patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client):
            result = zm._zoom_request("GET", "/test")

        assert result == {"data": "ok"}

    def test_204_returns_empty_dict(self):
        mock_response = MagicMock()
        mock_response.status_code = 204

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response

        with patch.object(zm, "get_access_token", return_value="tok"), \
             patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client):
            result = zm._zoom_request("DELETE", "/test")

        assert result == {}

    def test_401_clears_cache_and_retries(self):
        zm._token_cache["access_token"] = "stale-token"
        zm._token_cache["expires_at"] = time.time() + 3600

        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_401.text = "Unauthorized"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {"ok": True}

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.side_effect = [resp_401, resp_200]

        with patch.object(zm, "get_access_token", return_value="fresh-tok"), \
             patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client), \
             patch("tools.integrations.zoom_manager.time.sleep"):
            result = zm._zoom_request("GET", "/test")

        assert result == {"ok": True}
        # Token cache should have been cleared
        assert "access_token" not in zm._token_cache

    def test_non_retryable_error_raises(self):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not Found"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_response

        with patch.object(zm, "get_access_token", return_value="tok"), \
             patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client), \
             patch("tools.integrations.zoom_manager.time.sleep"):
            with pytest.raises(zm.ZoomAPIError) as exc_info:
                zm._zoom_request("GET", "/meetings/bad-id")

        assert exc_info.value.status_code == 404


# ===========================================================================
# 4. create_meeting — payload structure
# ===========================================================================


class TestCreateMeeting:
    def test_meeting_payload_structure(self):
        fake_response = {
            "id": 12345,
            "join_url": "https://zoom.us/j/12345",
            "start_url": "https://zoom.us/s/12345",
            "topic": "AI კურსი — ჯგუფი #1, ლექცია #3",
            "start_time": "2026-03-17T16:00:00Z",
        }

        with patch.object(zm, "_zoom_request", return_value=fake_response) as mock_req:
            start = datetime(2026, 3, 17, 20, 0, 0, tzinfo=ZoneInfo("Asia/Tbilisi"))
            result = zm.create_meeting(1, 3, start)

        assert result["id"] == 12345
        assert result["join_url"] == "https://zoom.us/j/12345"

        # Verify the API call
        call_args = mock_req.call_args
        assert call_args[0][0] == "POST"
        assert "/users/me/meetings" in call_args[0][1]
        payload = call_args[1]["json"]
        assert payload["type"] == 2  # scheduled
        assert payload["settings"]["auto_recording"] == "cloud"

    def test_naive_datetime_treated_as_tbilisi(self):
        fake_response = {
            "id": 99, "join_url": "u", "start_url": "s",
            "topic": "t", "start_time": "2026-03-17T16:00:00Z",
        }

        with patch.object(zm, "_zoom_request", return_value=fake_response) as mock_req:
            # Naive datetime — should be treated as Tbilisi
            start = datetime(2026, 3, 17, 20, 0, 0)
            zm.create_meeting(1, 1, start)

        payload = mock_req.call_args[1]["json"]
        # 20:00 Tbilisi (UTC+4) = 16:00 UTC
        assert "16:00:00Z" in payload["start_time"]


# ===========================================================================
# 5. get_meeting_recordings
# ===========================================================================


class TestGetMeetingRecordings:
    def test_returns_recording_data(self):
        fake_data = {
            "recording_files": [
                {"file_type": "MP4", "status": "completed", "download_url": "https://zoom/dl"},
            ],
            "total_size": 1024,
        }

        with patch.object(zm, "_zoom_request", return_value=fake_data):
            result = zm.get_meeting_recordings("mtg-123")

        assert len(result["recording_files"]) == 1
        assert result["recording_files"][0]["file_type"] == "MP4"


# ===========================================================================
# 6. download_recording — streaming
# ===========================================================================


class TestDownloadRecording:
    def test_downloads_to_dest_path(self, tmp_path):
        dest = tmp_path / "recording.mp4"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-length": "100"}
        mock_response.iter_bytes.return_value = [b"\x00" * 100]
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_response

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client):
            result = zm.download_recording(
                "https://zoom.us/recording/dl", "token", dest
            )

        assert result == dest
        assert dest.exists()
        assert dest.stat().st_size == 100

    def test_raises_on_http_error(self, tmp_path):
        dest = tmp_path / "bad.mp4"

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_response

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client):
            with pytest.raises(zm.ZoomDownloadError, match="403"):
                zm.download_recording("https://zoom.us/dl", "tok", dest)

    def test_creates_parent_directories(self, tmp_path):
        dest = tmp_path / "sub" / "dir" / "rec.mp4"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-length": "10"}
        mock_response.iter_bytes.return_value = [b"\x00" * 10]
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_response

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client):
            result = zm.download_recording("https://zoom/dl", "tok", dest)

        assert result.exists()


# ===========================================================================
# 7. Constants
# ===========================================================================


class TestConstants:
    def test_tbilisi_tz_is_zoneinfo(self):
        assert zm.TBILISI_TZ.key == "Asia/Tbilisi"

    def test_meeting_duration(self):
        assert zm.MEETING_DURATION_MINUTES == 180

    def test_max_retries_is_positive(self):
        assert zm.MAX_RETRIES > 0


# ===========================================================================
# 8. start_recording_live (Zoom Live Meeting Events API)
# ===========================================================================


class TestStartRecordingLive:
    """Force-start cloud recording on a live Zoom meeting.

    The watchdog calls this 2 minutes after lecture start. It must be a
    total function — never raise — so the watchdog can route gracefully
    to operator alerts on every failure mode.
    """

    @staticmethod
    def _mock_response(status_code: int, text: str = ""):
        m = MagicMock()
        m.status_code = status_code
        m.text = text
        return m

    @staticmethod
    def _patch_client(response):
        client_mock = MagicMock()
        client_mock.__enter__ = MagicMock(return_value=client_mock)
        client_mock.__exit__ = MagicMock(return_value=False)
        client_mock.patch.return_value = response
        return patch.object(zm.httpx, "Client", return_value=client_mock)

    def test_success_204_returns_ok_true(self):
        resp = self._mock_response(204)
        with patch.object(zm, "get_access_token", return_value="t"), \
             self._patch_client(resp):
            r = zm.start_recording_live(123456789)
        assert r["ok"] is True
        assert r["status_code"] == 204
        assert r["reason"] is None

    def test_success_200_returns_ok_true(self):
        resp = self._mock_response(200, "")
        with patch.object(zm, "get_access_token", return_value="t"), \
             self._patch_client(resp):
            r = zm.start_recording_live("abc")
        assert r["ok"] is True

    def test_scope_missing_400_returns_scope_missing(self):
        resp = self._mock_response(
            400,
            '{"code":4711,"message":"Invalid access token, does not contain '
            'scopes:[meeting:update:in_meeting_controls]"}',
        )
        with patch.object(zm, "get_access_token", return_value="t"), \
             self._patch_client(resp):
            r = zm.start_recording_live(42)
        assert r["ok"] is False
        assert r["reason"] == "scope_missing"
        assert r["status_code"] == 400

    def test_meeting_not_live_404_returns_meeting_not_live(self):
        resp = self._mock_response(404, "Not Found")
        with patch.object(zm, "get_access_token", return_value="t"), \
             self._patch_client(resp):
            r = zm.start_recording_live("xyz")
        assert r["ok"] is False
        assert r["reason"] == "meeting_not_live"

    def test_unexpected_status_returns_http_code(self):
        resp = self._mock_response(500, "Internal Server Error")
        with patch.object(zm, "get_access_token", return_value="t"), \
             self._patch_client(resp):
            r = zm.start_recording_live(1)
        assert r["ok"] is False
        assert r["reason"] == "http_500"

    def test_request_error_returns_http_error(self):
        from httpx import RequestError
        client_mock = MagicMock()
        client_mock.__enter__ = MagicMock(return_value=client_mock)
        client_mock.__exit__ = MagicMock(return_value=False)
        client_mock.patch.side_effect = RequestError("boom")
        with patch.object(zm, "get_access_token", return_value="t"), \
             patch.object(zm.httpx, "Client", return_value=client_mock):
            r = zm.start_recording_live(7)
        assert r["ok"] is False
        assert r["reason"].startswith("http_error:")

    def test_auth_failure_returns_auth_error_not_raises(self):
        """get_access_token failure must return error dict, not propagate."""
        with patch.object(zm, "get_access_token",
                          side_effect=zm.ZoomAuthError("creds invalid")):
            r = zm.start_recording_live(99)
        assert r["ok"] is False
        assert r["reason"].startswith("auth_error:")

    def test_never_raises_on_unknown_exception(self):
        """Watchdog callers depend on this being a total function."""
        with patch.object(zm, "get_access_token", side_effect=Exception("kaboom")):
            r = zm.start_recording_live(0)
        assert isinstance(r, dict)
        assert r.get("ok") is False
