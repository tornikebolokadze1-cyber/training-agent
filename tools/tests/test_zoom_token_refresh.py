"""Regression tests for Zoom token-refresh behaviour during long downloads.

US-001 from the 2026-05-13 production-hardening ralph: 2-hour lecture
recordings on Railway egress can outlive Zoom's 1-hour S2S OAuth token.
The download retry loop must:

  1. Re-fetch the token on HTTP 401 from the CDN and retry with the new
     ``?access_token=`` URL parameter.
  2. Proactively refresh the token after 50 minutes (3000 s) elapsed
     since acquisition, before issuing the next request.
  3. Always rebuild the URL query string when the token changes — Zoom
     CDN does not accept the Authorization header.

These tests pin the three paths against regression.

Run with:
    pytest tools/tests/test_zoom_token_refresh.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import tools.integrations.zoom_manager as zm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream_response(status_code: int, content: bytes = b"") -> MagicMock:
    """Build a mock ``httpx`` streaming response usable as a context manager."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-length": str(len(content))}
    resp.iter_bytes.return_value = [content] if content else []
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_head_response(content_length: int = 0) -> MagicMock:
    """Build a mock HEAD response (used by the disk-space pre-flight check)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-length": str(content_length)}
    return resp


def _make_client_returning(stream_responses: list[MagicMock]) -> MagicMock:
    """Build an httpx.Client mock whose ``.stream()`` cycles through responses.

    ``download_recording`` opens a fresh ``httpx.Client`` per retry iteration
    (and another per HEAD probe). We return a single mock client that yields
    a queued response for each ``.stream()`` call, regardless of which
    ``with httpx.Client()`` produced it. The HEAD probe call also goes
    through this same client mock, so we wire ``.head()`` to a benign
    response that does not count against the queue.
    """
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.head.return_value = _make_head_response(0)
    client.stream.side_effect = stream_responses
    return client


def _reset_token_cache() -> None:
    zm._token_cache.clear()


# ---------------------------------------------------------------------------
# 1. 401 from CDN triggers an immediate token refresh + retry
# ---------------------------------------------------------------------------


class TestTokenRefreshOn401:
    """Mid-flight 401 must call get_access_token() again and retry."""

    def setup_method(self) -> None:
        _reset_token_cache()

    def test_token_refreshes_on_401(self, tmp_path, monkeypatch):
        dest = tmp_path / "recording.mp4"

        # First stream returns 401, second stream returns 200 with content
        resp_401 = _make_stream_response(401)
        resp_200 = _make_stream_response(200, content=b"\x00" * 64)
        mock_client = _make_client_returning([resp_401, resp_200])

        # Track get_access_token calls. The conftest auto-mocks it to return
        # "fake-test-token"; we override here with a counter that returns
        # increasing values so the second retry uses a fresh token string.
        call_count = {"n": 0}

        def fake_get_access_token() -> str:
            call_count["n"] += 1
            return f"refreshed-token-{call_count['n']}"

        monkeypatch.setattr(zm, "get_access_token", fake_get_access_token)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client), \
             patch("tools.integrations.zoom_manager.time.sleep"):
            result = zm.download_recording(
                "https://zoom.us/rec/download/abc",
                "initial-token",  # seed token, will be invalidated by 401
                dest,
            )

        assert result == dest
        assert dest.exists()
        # get_access_token must have been called at least once — specifically,
        # on the 401 path. (It is NOT called on the initial attempt because
        # the seed token is treated as fresh at function entry.)
        assert call_count["n"] >= 1, (
            "get_access_token should be invoked on 401 to refresh the token"
        )
        # And the stream must have been attempted twice (initial 401 + retry).
        assert mock_client.stream.call_count == 2


# ---------------------------------------------------------------------------
# 2. Token-age guard refreshes proactively after 50 min
# ---------------------------------------------------------------------------


class TestTokenRefreshWhenOld:
    """When the in-flight token ages past TOKEN_REFRESH_AGE_SECONDS, the
    next retry iteration must refresh before issuing the request — even
    when the previous attempt failed for an unrelated (network) reason.
    """

    def setup_method(self) -> None:
        _reset_token_cache()

    def test_token_refreshes_when_old(self, tmp_path, monkeypatch):
        dest = tmp_path / "recording.mp4"

        # First attempt: simulate a transient network error so the retry
        # loop progresses to a second iteration.
        # Second attempt: succeed.
        class _FakeTransportError(Exception):
            pass

        # Patch httpx.TransportError so the except clause catches our fake.
        monkeypatch.setattr(zm.httpx, "TransportError", _FakeTransportError)

        resp_200 = _make_stream_response(200, content=b"\x00" * 32)

        # First stream() raises; second returns success.
        def stream_side_effect(*args, **kwargs):
            if not getattr(stream_side_effect, "called", False):
                stream_side_effect.called = True
                raise _FakeTransportError("transient")
            return resp_200

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = _make_head_response(0)
        mock_client.stream.side_effect = stream_side_effect

        # Advance time.monotonic past TOKEN_REFRESH_AGE_SECONDS between
        # iterations. Iteration 1 reads t=0 (sets token_issued_at=0); the
        # exception bubbles to except; iteration 2 reads t=3001, triggering
        # the age guard.
        monotonic_values = iter([0.0, 3001.0, 3001.0, 3001.0, 3001.0])
        monkeypatch.setattr(
            zm.time, "monotonic", lambda: next(monotonic_values, 3001.0)
        )

        call_count = {"n": 0}

        def fake_get_access_token() -> str:
            call_count["n"] += 1
            return f"age-refreshed-{call_count['n']}"

        monkeypatch.setattr(zm, "get_access_token", fake_get_access_token)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client), \
             patch("tools.integrations.zoom_manager.time.sleep"):
            result = zm.download_recording(
                "https://zoom.us/rec/download/aged",
                "seed-token",
                dest,
            )

        assert result == dest
        # The age guard must have called get_access_token at least once.
        assert call_count["n"] >= 1, (
            "Token-age guard must call get_access_token() when token > 50 min old"
        )


# ---------------------------------------------------------------------------
# 3. URL ``?access_token=`` is rebuilt with the NEW token on retry
# ---------------------------------------------------------------------------


class TestTokenUrlParamRebuilt:
    """After refresh, the second stream call must use the NEW token in the
    URL query string, not the old one. Zoom's CDN keys auth off the URL
    parameter — a stale token in the URL is the bug we're fixing.
    """

    def setup_method(self) -> None:
        _reset_token_cache()

    def test_token_url_param_rebuilt(self, tmp_path, monkeypatch):
        dest = tmp_path / "recording.mp4"

        # First stream returns 401, second returns 200.
        resp_401 = _make_stream_response(401)
        resp_200 = _make_stream_response(200, content=b"\x00" * 16)
        mock_client = _make_client_returning([resp_401, resp_200])

        def fake_get_access_token() -> str:
            return "BRAND-NEW-TOKEN"

        monkeypatch.setattr(zm, "get_access_token", fake_get_access_token)

        with patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client), \
             patch("tools.integrations.zoom_manager.time.sleep"):
            zm.download_recording(
                "https://zoom.us/rec/download/xyz",
                "OLD-SEED-TOKEN",
                dest,
            )

        # Two stream() calls: first with old token, second with new token.
        assert mock_client.stream.call_count == 2
        first_call_url = mock_client.stream.call_args_list[0].args[1]
        second_call_url = mock_client.stream.call_args_list[1].args[1]

        assert "access_token=OLD-SEED-TOKEN" in first_call_url, (
            f"First attempt should use the seed token; got URL: {first_call_url}"
        )
        assert "access_token=BRAND-NEW-TOKEN" in second_call_url, (
            f"Retry after 401 must rebuild URL with refreshed token; "
            f"got URL: {second_call_url}"
        )
        assert "OLD-SEED-TOKEN" not in second_call_url, (
            "Stale token must NOT appear in retry URL — this is the bug we're "
            f"fixing. Got URL: {second_call_url}"
        )


# ---------------------------------------------------------------------------
# 4. Constant sanity check — guards against accidental tuning
# ---------------------------------------------------------------------------


class TestTokenRefreshConstants:
    def test_age_refresh_threshold_under_one_hour(self):
        """50-minute threshold must stay under the 60-minute token lifetime
        so we never race the cliff on Railway egress."""
        assert zm.TOKEN_REFRESH_AGE_SECONDS < 3600
        assert zm.TOKEN_REFRESH_AGE_SECONDS >= 2400  # don't refresh too aggressively either
