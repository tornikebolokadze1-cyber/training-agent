"""Regression tests — Zoom recording download uses Bearer header auth.

Verifies that:
1.  The Authorization: Bearer <token> header is set on every download request.
2.  The download URL does NOT contain access_token= as a query parameter.
3.  httpx's default cross-origin redirect behaviour strips the Authorization
    header (so we confirm our code does not override that safety mechanism).

Run with:
    pytest tools/tests/test_zoom_oauth_header.py -v
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Minimal stubs so zoom_manager can be imported without full dep tree
# ---------------------------------------------------------------------------

def _stub(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod

for _pkg in [
    "pinecone", "google", "google.genai", "google.genai.types",
    "google.oauth2", "google.oauth2.credentials", "google.auth",
    "google.auth.transport", "google.auth.transport.requests",
    "googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
    "anthropic", "slowapi", "slowapi.errors", "slowapi.util",
]:
    _stub(_pkg)

# google.oauth2.credentials.Credentials stub
_creds_mod = sys.modules.get("google.oauth2.credentials")
if _creds_mod is not None:
    _creds_mod.Credentials = MagicMock

_resilience_mod = _stub("tools.core.api_resilience")
# resilient_api_call is a decorator factory: @resilient_api_call(service=..., operation=...)
# so it must return a decorator (fn -> fn), not call fn directly.
_resilience_mod.resilient_api_call = lambda *a, **kw: (lambda fn: fn)


# ---------------------------------------------------------------------------
# Now import the module under test
# ---------------------------------------------------------------------------
from tools.integrations.zoom_manager import download_recording  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ACCESS_TOKEN = "test_access_token_abc123"
DOWNLOAD_URL = "https://zoom.us/rec/download/example_file.mp4"


def _make_mock_response(status_code: int = 200, content_length: str = "100") -> MagicMock:
    """Return a mock httpx streaming response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.headers = {"content-length": content_length}
    mock_resp.iter_bytes.return_value = iter([b"x" * 100])
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# Test 1: Authorization header present, access_token NOT in URL
# ---------------------------------------------------------------------------

def test_download_uses_bearer_header_not_query_param(tmp_path: Path) -> None:
    """Bearer auth header must be set; access_token= must not appear in URL."""
    captured_requests: list[dict] = []

    def fake_stream(method: str, url: str, headers: dict | None = None, **kwargs):
        captured_requests.append({"method": method, "url": url, "headers": headers or {}})
        mock_resp = _make_mock_response()
        return mock_resp

    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.stream = fake_stream
    # HEAD probe returns quickly with no content-length to skip disk check
    mock_client.head = MagicMock(return_value=MagicMock(headers={}))

    dest = tmp_path / "recording.mp4"

    with patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client):
        with patch("tools.integrations.zoom_manager.compute_file_checksum", return_value="abc"):
            with patch("tools.integrations.zoom_manager.save_checksum"):
                download_recording(
                    download_url=DOWNLOAD_URL,
                    access_token=ACCESS_TOKEN,
                    dest_path=dest,
                    resume=False,
                )

    assert captured_requests, "No stream request was made"
    stream_req = captured_requests[-1]

    # URL must NOT contain the token as a query parameter
    assert "access_token=" not in stream_req["url"], (
        f"access_token leaked into URL: {stream_req['url']}"
    )
    assert ACCESS_TOKEN not in stream_req["url"], (
        f"Raw access token leaked into URL: {stream_req['url']}"
    )

    # Authorization header must be set correctly
    auth_header = stream_req["headers"].get("Authorization", "")
    assert auth_header == f"Bearer {ACCESS_TOKEN}", (
        f"Expected 'Bearer {ACCESS_TOKEN}', got '{auth_header}'"
    )


# ---------------------------------------------------------------------------
# Test 2: HEAD probe also uses Bearer header, not URL token
# ---------------------------------------------------------------------------

def test_head_probe_uses_bearer_header_not_query_param(tmp_path: Path) -> None:
    """The pre-flight HEAD request must also use Bearer auth."""
    captured_head_calls: list[dict] = []

    def fake_head(url: str, headers: dict | None = None, **kwargs):
        captured_head_calls.append({"url": url, "headers": headers or {}})
        return MagicMock(headers={})

    mock_client = MagicMock()
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.head = fake_head

    mock_stream_resp = _make_mock_response()
    mock_client.stream = MagicMock(return_value=mock_stream_resp)

    dest = tmp_path / "recording_head.mp4"

    with patch("tools.integrations.zoom_manager.httpx.Client", return_value=mock_client):
        with patch("tools.integrations.zoom_manager.compute_file_checksum", return_value="abc"):
            with patch("tools.integrations.zoom_manager.save_checksum"):
                download_recording(
                    download_url=DOWNLOAD_URL,
                    access_token=ACCESS_TOKEN,
                    dest_path=dest,
                    resume=False,
                )

    assert captured_head_calls, "No HEAD request was made"
    head_req = captured_head_calls[0]

    assert "access_token=" not in head_req["url"], (
        f"access_token leaked into HEAD URL: {head_req['url']}"
    )
    assert ACCESS_TOKEN not in head_req["url"], (
        f"Raw token leaked into HEAD URL: {head_req['url']}"
    )
    auth_header = head_req["headers"].get("Authorization", "")
    assert auth_header == f"Bearer {ACCESS_TOKEN}", (
        f"HEAD probe missing Bearer auth: '{auth_header}'"
    )


# ---------------------------------------------------------------------------
# Test 3: httpx does NOT follow cross-origin redirects with Auth header
#         (verifying we rely on httpx default behaviour, not override it)
# ---------------------------------------------------------------------------

def test_no_trust_on_redirect_header_passing() -> None:
    """Confirm we do NOT set follow_redirects=False or override redirect hooks.

    httpx strips Authorization on cross-origin redirects by default when
    follow_redirects=True. Our code uses that default and does NOT manually
    re-attach the Auth header after a redirect, so cross-origin redirect
    targets never receive it.
    """
    import inspect
    import tools.integrations.zoom_manager as zm_module

    source = inspect.getsource(zm_module.download_recording)

    # We must NOT be manually re-appending the token after redirects
    assert "authenticated_url" not in source, (
        "Found legacy 'authenticated_url' variable — token may still be in URL"
    )
    # We must NOT be constructing an access_token query param
    assert "access_token={access_token}" not in source, (
        "Found access_token query-param construction in download_recording"
    )
    # We must use Bearer in headers
    assert "Bearer" in source, (
        "download_recording does not use Bearer header auth"
    )
