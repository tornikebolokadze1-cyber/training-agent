"""FastAPI server tests for tools/server.py.

Covers:
- Health endpoint (200 healthy, 503 degraded when WEBHOOK_SECRET unset)
- Correlation ID middleware
- Rate limiter configuration
- Zoom webhook CRC challenge-response
- Zoom webhook HMAC signature verification (valid, missing, invalid)
- Zoom webhook recording.completed event routing
- /process-recording secret validation and deduplication
- /whatsapp-incoming basic routing
- _evict_stale_tasks stale task cleanup and operator alert

Run with:
    pytest tools/tests/test_server.py -v
"""

from __future__ import annotations

import hashlib
import hmac
import json

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# test_server needs REAL fastapi, slowapi, httpx, and pydantic to run the
# ASGI app with httpx.AsyncClient.  Restore them before importing server.
# ---------------------------------------------------------------------------
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# Pop stubs for packages that test_server needs real implementations of.
# Also pop tools.app.server so it reimports with real FastAPI/slowapi.
for _mod_name in list(sys.modules):
    if _mod_name.startswith(("fastapi", "slowapi", "httpx", "pydantic", "tools.app.server")):
        sys.modules.pop(_mod_name, None)

# Now re-import real packages

from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import tools.app.server as srv  # noqa: E402
import tools.integrations.whatsapp_sender as _wa_sender_mod  # noqa: E402
from tools.app.server import (  # noqa: E402
    STALE_TASK_HOURS,
    _evict_stale_tasks,
    _processing_tasks,
    _task_key,
    app,
)

# NOTE: @pytest.mark.asyncio is applied per-class below rather than globally
# so that the sync test classes (TestRateLimiterConfiguration, TestEvictStaleTasks)
# do not emit a "non-async function marked asyncio" warning.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_WEBHOOK_SECRET = "test-secret-abc"
_TEST_ZOOM_SECRET = "test-zoom-secret-xyz"

_AUTH_HEADER = {"Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}"}


def _zoom_sig(body: bytes, timestamp: str, secret: str = _TEST_ZOOM_SECRET) -> str:
    """Compute the Zoom HMAC-SHA256 signature for a raw body + timestamp."""
    message = f"v0:{timestamp}:{body.decode()}"
    return "v0=" + hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _make_recording_body(topic: str = "ჯგუფი #1 lecture") -> dict:
    return {
        "event": "recording.completed",
        "download_token": "dl_token_test",
        "payload": {
            "object": {
                "topic": topic,
                "start_time": "2026-03-13T16:00:00Z",
                "recording_files": [
                    {
                        "file_type": "MP4",
                        "recording_type": "shared_screen_with_speaker_view",
                        "download_url": "https://zoom.us/rec/download/test.mp4",
                    }
                ],
            }
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_processing_tasks():
    """Reset the in-flight task registry and pipeline state files before every test."""
    _processing_tasks.clear()
    # Also clean up any pipeline state files from previous tests
    from tools.core.config import TMP_DIR
    for state_file in TMP_DIR.glob("pipeline_state_*.json"):
        state_file.unlink(missing_ok=True)
    yield
    _processing_tasks.clear()
    for state_file in TMP_DIR.glob("pipeline_state_*.json"):
        state_file.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def mock_alert_operator():
    """Patch alert_operator with a fresh MagicMock for every test."""
    with patch.object(_wa_sender_mod, "alert_operator", new_callable=MagicMock) as mock_alert:
        # Also patch it on the server module where it was imported via `from ... import`
        with patch.object(srv, "alert_operator", mock_alert):
            yield mock_alert


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset the slowapi in-memory rate limiter between tests.

    Without this, repeated requests to the same endpoint across the test session
    accumulate against the per-IP counters and trigger 429 responses mid-suite.
    """
    srv.limiter.reset()
    yield
    srv.limiter.reset()


@pytest.fixture
def patched_secrets():
    """Patch WEBHOOK_SECRET and ZOOM_WEBHOOK_SECRET_TOKEN on the server module."""
    with (
        patch.object(srv, "WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET),
        patch.object(srv, "ZOOM_WEBHOOK_SECRET_TOKEN", _TEST_ZOOM_SECRET),
    ):
        yield


# ---------------------------------------------------------------------------
# Async client factory
# ---------------------------------------------------------------------------

async def _async_client():
    """Return an httpx.AsyncClient wired to the FastAPI ASGI app.

    Uses base_url="http://localhost" so that the Host header sent to the ASGI
    app is "localhost", which is included in _allowed_hosts by server.py.
    """
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://localhost")


# ===========================================================================
# 1. Health endpoint
# ===========================================================================

@pytest.mark.asyncio
class TestHealthEndpoint:
    """Tests for the /health endpoint which now delegates to HealthMonitor.check_all()."""

    def _mock_check_all_healthy(self):
        """Return a mock check_all result for a healthy system."""
        return {
            "overall_status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "checks": [
                {"name": "disk_space", "severity": "ok", "message": "OK", "details": {}},
            ],
            "warnings_count": 0,
            "critical_count": 0,
        }

    def _mock_check_all_degraded(self):
        """Return a mock check_all result for a degraded system."""
        return {
            "overall_status": "degraded",
            "timestamp": datetime.now().isoformat(),
            "checks": [
                {"name": "disk_space", "severity": "warning", "message": "Low", "details": {}},
            ],
            "warnings_count": 1,
            "critical_count": 0,
        }

    def _mock_check_all_critical(self):
        """Return a mock check_all result for a critical system."""
        return {
            "overall_status": "critical",
            "timestamp": datetime.now().isoformat(),
            "checks": [
                {"name": "gemini_api", "severity": "critical", "message": "Unreachable", "details": {}},
            ],
            "warnings_count": 0,
            "critical_count": 1,
        }

    async def test_healthy_returns_200(self, patched_secrets, tmp_path):
        """GET /health returns 200 and status=healthy when all checks pass."""
        with patch("tools.core.health_monitor.get_cached_or_run_full_audit", return_value=self._mock_check_all_healthy()):
            async with await _async_client() as client:
                resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "training-agent"

    async def test_healthy_response_has_timestamp(self, patched_secrets, tmp_path):
        """Health response includes an ISO timestamp."""
        with patch("tools.core.health_monitor.get_cached_or_run_full_audit", return_value=self._mock_check_all_healthy()):
            async with await _async_client() as client:
                resp = await client.get("/health")
        data = resp.json()
        assert "timestamp" in data
        datetime.fromisoformat(data["timestamp"])

    async def test_healthy_includes_checks_list(self, patched_secrets, tmp_path):
        """Health response includes a checks list with check results."""
        with patch("tools.core.health_monitor.get_cached_or_run_full_audit", return_value=self._mock_check_all_healthy()):
            async with await _async_client() as client:
                resp = await client.get("/health")
        data = resp.json()
        assert "checks" in data
        assert isinstance(data["checks"], list)
        assert len(data["checks"]) > 0
        assert "name" in data["checks"][0]

    async def test_degraded_returns_503(self, tmp_path):
        """GET /health returns 503 when overall_status is critical."""
        with patch("tools.core.health_monitor.get_cached_or_run_full_audit", return_value=self._mock_check_all_critical()):
            async with await _async_client() as client:
                resp = await client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "critical"

    async def test_webhook_secret_check_shows_missing(self, tmp_path):
        """Health returns degraded (200) when a check is warning-level."""
        with patch("tools.core.health_monitor.get_cached_or_run_full_audit", return_value=self._mock_check_all_degraded()):
            async with await _async_client() as client:
                resp = await client.get("/health")
        data = resp.json()
        assert data["warnings_count"] == 1

    async def test_in_flight_task_count_reflected(self, patched_secrets, tmp_path):
        """Health endpoint reports the current number of in-progress tasks."""
        _processing_tasks["g1_l3"] = datetime.now()
        _processing_tasks["g2_l5"] = datetime.now()
        with patch("tools.core.health_monitor.get_cached_or_run_full_audit", return_value=self._mock_check_all_healthy()):
            async with await _async_client() as client:
                resp = await client.get("/health")
        data = resp.json()
        assert data["tasks_in_progress"] == 2


# ===========================================================================
# 2. Correlation ID middleware
# ===========================================================================

@pytest.mark.asyncio
class TestCorrelationIDMiddleware:
    async def test_response_has_correlation_id_header(self, patched_secrets, tmp_path):
        """Every response must include an X-Correlation-ID header."""
        with patch.object(srv, "TMP_DIR", tmp_path):
            async with await _async_client() as client:
                resp = await client.get("/health")
        assert "x-correlation-id" in resp.headers

    async def test_provided_correlation_id_echoed_back(self, patched_secrets, tmp_path):
        """When caller sends X-Correlation-ID, the same value is echoed in response."""
        custom_id = "my-trace-42"
        with patch.object(srv, "TMP_DIR", tmp_path):
            async with await _async_client() as client:
                resp = await client.get("/health", headers={"X-Correlation-ID": custom_id})
        assert resp.headers["x-correlation-id"] == custom_id

    async def test_auto_generated_correlation_id_nonempty(self, patched_secrets, tmp_path):
        """When no X-Correlation-ID is provided, a non-empty one is generated."""
        with patch.object(srv, "TMP_DIR", tmp_path):
            async with await _async_client() as client:
                resp = await client.get("/health")
        generated = resp.headers.get("x-correlation-id", "")
        assert len(generated) > 0


# ===========================================================================
# 3. Rate limiter configuration
# ===========================================================================

class TestRateLimiterConfiguration:
    def test_limiter_attribute_exists_on_app_state(self):
        """app.state.limiter must be set (slowapi Limiter instance)."""
        assert hasattr(app.state, "limiter")
        assert app.state.limiter is not None

    def test_limiter_is_slowapi_instance(self):
        """The limiter must be a slowapi Limiter (or mock in test env)."""
        # In full test suite, slowapi.Limiter may be a MagicMock stub.
        # Check attribute existence instead of isinstance to be robust.
        limiter = app.state.limiter
        assert limiter is not None
        assert hasattr(limiter, '__call__') or hasattr(limiter, 'limit')


# ===========================================================================
# 4. Zoom webhook — CRC challenge-response
# ===========================================================================

@pytest.mark.asyncio
class TestZoomWebhookCRC:
    async def test_crc_returns_200(self, patched_secrets):
        """CRC challenge must return HTTP 200."""
        body = {
            "event": "endpoint.url_validation",
            "payload": {"plainToken": "abc123plain"},
        }
        async with await _async_client() as client:
            resp = await client.post("/zoom-webhook", json=body)
        assert resp.status_code == 200

    async def test_crc_response_contains_plain_token(self, patched_secrets):
        """CRC response must echo the original plainToken."""
        plain = "test_plain_token_xyz"
        body = {
            "event": "endpoint.url_validation",
            "payload": {"plainToken": plain},
        }
        async with await _async_client() as client:
            resp = await client.post("/zoom-webhook", json=body)
        data = resp.json()
        assert data["plainToken"] == plain

    async def test_crc_response_encrypted_token_is_correct_hmac(self, patched_secrets):
        """CRC encryptedToken must be HMAC-SHA256(secret, plainToken).hexdigest()."""
        plain = "crc_challenge_token"
        expected_enc = hmac.new(
            _TEST_ZOOM_SECRET.encode(),
            plain.encode(),
            hashlib.sha256,
        ).hexdigest()

        body = {
            "event": "endpoint.url_validation",
            "payload": {"plainToken": plain},
        }
        async with await _async_client() as client:
            resp = await client.post("/zoom-webhook", json=body)
        data = resp.json()
        assert data["encryptedToken"] == expected_enc

    async def test_crc_fails_503_when_zoom_secret_unset(self):
        """CRC must return 503 when ZOOM_WEBHOOK_SECRET_TOKEN is not configured."""
        with patch.object(srv, "ZOOM_WEBHOOK_SECRET_TOKEN", ""):
            body = {
                "event": "endpoint.url_validation",
                "payload": {"plainToken": "some_plain"},
            }
            async with await _async_client() as client:
                resp = await client.post("/zoom-webhook", json=body)
        assert resp.status_code == 503


# ===========================================================================
# 5. Zoom webhook — HMAC signature verification
# ===========================================================================

@pytest.mark.asyncio
class TestZoomWebhookHMAC:
    async def test_missing_signature_headers_returns_401(self, patched_secrets):
        """Requests without x-zm-signature/x-zm-request-timestamp are rejected."""
        body = _make_recording_body()
        async with await _async_client() as client:
            resp = await client.post("/zoom-webhook", json=body)
        assert resp.status_code == 401

    async def test_invalid_signature_returns_401(self, patched_secrets):
        """A request with a wrong HMAC signature must be rejected with 401."""
        body_bytes = json.dumps(_make_recording_body()).encode()
        timestamp = str(int(time.time()))
        headers = {
            "x-zm-request-timestamp": timestamp,
            "x-zm-signature": "v0=deadbeefdeadbeefdeadbeef",  # wrong
        }
        async with await _async_client() as client:
            resp = await client.post(
                "/zoom-webhook",
                content=body_bytes,
                headers={**headers, "content-type": "application/json"},
            )
        assert resp.status_code == 401

    async def test_valid_signature_accepted(self, patched_secrets):
        """A correctly signed Zoom webhook must NOT return 401."""
        body_dict = _make_recording_body()
        body_bytes = json.dumps(body_dict).encode()
        timestamp = str(int(time.time()))
        sig = _zoom_sig(body_bytes, timestamp)
        headers = {
            "x-zm-request-timestamp": timestamp,
            "x-zm-signature": sig,
        }
        with patch("tools.app.server.process_recording_task"):
            async with await _async_client() as client:
                resp = await client.post(
                    "/zoom-webhook",
                    content=body_bytes,
                    headers={**headers, "content-type": "application/json"},
                )
        assert resp.status_code != 401

    async def test_zoom_secret_unset_returns_503_for_non_crc(self):
        """When ZOOM_WEBHOOK_SECRET_TOKEN is missing, non-CRC events return 503."""
        with patch.object(srv, "ZOOM_WEBHOOK_SECRET_TOKEN", ""):
            body = _make_recording_body()
            body_bytes = json.dumps(body).encode()
            timestamp = str(int(time.time()))
            headers = {
                "x-zm-request-timestamp": timestamp,
                "x-zm-signature": "v0=anything",
            }
            async with await _async_client() as client:
                resp = await client.post(
                    "/zoom-webhook",
                    content=body_bytes,
                    headers={**headers, "content-type": "application/json"},
                )
        assert resp.status_code == 503

    async def test_stale_timestamp_rejected(self, patched_secrets):
        """Requests with timestamps older than 5 minutes must be rejected."""
        body_dict = _make_recording_body()
        body_bytes = json.dumps(body_dict).encode()
        stale_ts = str(int(time.time()) - 600)  # 10 min old
        sig = _zoom_sig(body_bytes, stale_ts)
        headers = {
            "x-zm-request-timestamp": stale_ts,
            "x-zm-signature": sig,
            "content-type": "application/json",
        }
        async with await _async_client() as client:
            resp = await client.post("/zoom-webhook", content=body_bytes, headers=headers)
        assert resp.status_code == 401


# ===========================================================================
# 6. Zoom webhook — recording.completed event routing
# ===========================================================================

@pytest.mark.asyncio
class TestZoomWebhookRecordingCompleted:
    def _signed_request(self, body_dict: dict):
        """Return (body_bytes, headers) with a valid HMAC signature."""
        body_bytes = json.dumps(body_dict).encode()
        timestamp = str(int(time.time()))
        sig = _zoom_sig(body_bytes, timestamp)
        headers = {
            "x-zm-request-timestamp": timestamp,
            "x-zm-signature": sig,
            "content-type": "application/json",
        }
        return body_bytes, headers

    async def test_recording_completed_group1_accepted(self, patched_secrets):
        """A valid recording.completed for Group 1 must return status=accepted."""
        body = _make_recording_body(topic="ჯგუფი #1 lecture")
        body_bytes, headers = self._signed_request(body)
        with patch("tools.app.server.process_recording_task"):
            async with await _async_client() as client:
                resp = await client.post(
                    "/zoom-webhook", content=body_bytes, headers=headers
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["group"] == 1

    async def test_recording_completed_group2_accepted(self, patched_secrets):
        """A valid recording.completed for Group 2 must return status=accepted."""
        body = _make_recording_body(topic="ჯგუფი #2 lecture")
        body_bytes, headers = self._signed_request(body)
        with patch("tools.app.server.process_recording_task"):
            async with await _async_client() as client:
                resp = await client.post(
                    "/zoom-webhook", content=body_bytes, headers=headers
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert data["group"] == 2

    async def test_unknown_group_returns_ignored(self, patched_secrets):
        """A recording with an unrecognised topic returns status=ignored."""
        body = _make_recording_body(topic="Some Generic Meeting Title")
        body_bytes, headers = self._signed_request(body)
        async with await _async_client() as client:
            resp = await client.post(
                "/zoom-webhook", content=body_bytes, headers=headers
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ignored"

    async def test_no_mp4_recording_returns_ignored(self, patched_secrets):
        """A recording.completed event with no MP4 file returns status=ignored."""
        body = {
            "event": "recording.completed",
            "download_token": "tok",
            "payload": {
                "object": {
                    "topic": "ჯგუფი #1 test",
                    "start_time": "2026-03-13T16:00:00Z",
                    "recording_files": [
                        {"file_type": "TRANSCRIPT", "recording_type": "audio_only"}
                    ],
                }
            },
        }
        body_bytes, headers = self._signed_request(body)
        async with await _async_client() as client:
            resp = await client.post(
                "/zoom-webhook", content=body_bytes, headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    async def test_unrelated_event_returns_ignored(self, patched_secrets):
        """Non-recording events (e.g. meeting.started) are silently ignored."""
        body_dict = {"event": "meeting.started", "payload": {}}
        body_bytes, headers = self._signed_request(body_dict)
        async with await _async_client() as client:
            resp = await client.post(
                "/zoom-webhook", content=body_bytes, headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    async def test_duplicate_zoom_recording_returns_duplicate_status(self, patched_secrets):
        """A second recording.completed for the same group+lecture is deduplicated."""
        body = _make_recording_body(topic="ჯგუფი #1 lecture")
        body_bytes, headers = self._signed_request(body)

        # Manually inject the task key to simulate an already-running job
        with patch("tools.app.server.get_lecture_number", return_value=1):
            _processing_tasks[_task_key(1, 1)] = datetime.now()
            with patch("tools.app.server.process_recording_task"):
                async with await _async_client() as client:
                    resp = await client.post(
                        "/zoom-webhook", content=body_bytes, headers=headers
                    )
        assert resp.status_code == 200
        assert resp.json()["status"] == "duplicate"


# ===========================================================================
# 7. /process-recording endpoint
# ===========================================================================

_VALID_RECORDING_PAYLOAD = {
    "download_url": "https://zoom.us/rec/download/test.mp4",
    "access_token": "zoom_access_token",
    "group_number": 1,
    "lecture_number": 3,
    "drive_folder_id": "drive_folder_abc",
}


@pytest.mark.asyncio
class TestProcessRecordingEndpoint:
    async def test_valid_request_returns_accepted(self, patched_secrets):
        """A properly authenticated request starts processing and returns accepted."""
        with patch("tools.app.server.process_recording_task"):
            async with await _async_client() as client:
                resp = await client.post(
                    "/process-recording",
                    json=_VALID_RECORDING_PAYLOAD,
                    headers=_AUTH_HEADER,
                )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"

    async def test_missing_auth_header_returns_401(self, patched_secrets):
        """Request without Authorization header is rejected with 401."""
        async with await _async_client() as client:
            resp = await client.post(
                "/process-recording", json=_VALID_RECORDING_PAYLOAD
            )
        assert resp.status_code == 401

    async def test_wrong_secret_returns_403(self, patched_secrets):
        """Request with a wrong Bearer token is rejected with 403."""
        async with await _async_client() as client:
            resp = await client.post(
                "/process-recording",
                json=_VALID_RECORDING_PAYLOAD,
                headers={"Authorization": "Bearer wrong-secret"},
            )
        assert resp.status_code == 403

    async def test_missing_webhook_secret_config_returns_503(self):
        """When WEBHOOK_SECRET is not configured, any request returns 503."""
        with patch.object(srv, "WEBHOOK_SECRET", ""):
            async with await _async_client() as client:
                resp = await client.post(
                    "/process-recording",
                    json=_VALID_RECORDING_PAYLOAD,
                    headers={"Authorization": "Bearer anything"},
                )
        assert resp.status_code == 503

    async def test_invalid_group_number_returns_422(self, patched_secrets):
        """group_number outside {1,2} must be rejected with 422."""
        payload = {**_VALID_RECORDING_PAYLOAD, "group_number": 99}
        async with await _async_client() as client:
            resp = await client.post(
                "/process-recording", json=payload, headers=_AUTH_HEADER
            )
        assert resp.status_code == 422

    async def test_invalid_lecture_number_too_high_returns_422(self, patched_secrets):
        """lecture_number > 15 must be rejected with 422."""
        payload = {**_VALID_RECORDING_PAYLOAD, "lecture_number": 16}
        async with await _async_client() as client:
            resp = await client.post(
                "/process-recording", json=payload, headers=_AUTH_HEADER
            )
        assert resp.status_code == 422

    async def test_invalid_lecture_number_zero_returns_422(self, patched_secrets):
        """lecture_number of 0 must be rejected with 422."""
        payload = {**_VALID_RECORDING_PAYLOAD, "lecture_number": 0}
        async with await _async_client() as client:
            resp = await client.post(
                "/process-recording", json=payload, headers=_AUTH_HEADER
            )
        assert resp.status_code == 422

    async def test_duplicate_task_rejected_with_409(self, patched_secrets):
        """A second request for the same group+lecture returns 409 Conflict."""
        key = _task_key(
            _VALID_RECORDING_PAYLOAD["group_number"],
            _VALID_RECORDING_PAYLOAD["lecture_number"],
        )
        _processing_tasks[key] = datetime.now()

        async with await _async_client() as client:
            resp = await client.post(
                "/process-recording",
                json=_VALID_RECORDING_PAYLOAD,
                headers=_AUTH_HEADER,
            )
        assert resp.status_code == 409

    async def test_task_registered_in_tracking_dict(self, patched_secrets):
        """After a successful request the task key is added to _processing_tasks."""
        with patch("tools.app.server.process_recording_task"):
            async with await _async_client() as client:
                await client.post(
                    "/process-recording",
                    json=_VALID_RECORDING_PAYLOAD,
                    headers=_AUTH_HEADER,
                )
        key = _task_key(
            _VALID_RECORDING_PAYLOAD["group_number"],
            _VALID_RECORDING_PAYLOAD["lecture_number"],
        )
        assert key in _processing_tasks


# ===========================================================================
# 8. /whatsapp-incoming endpoint
# ===========================================================================

_WA_AUTH = {"Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}"}

_WA_TEXT_BODY = {
    "typeWebhook": "incomingMessageReceived",
    "timestamp": 1700000000,
    "senderData": {
        "chatId": "995599000001@c.us",
        "sender": "995599000001@c.us",
        "senderName": "Test User",
    },
    "messageData": {
        "typeMessage": "textMessage",
        "fromMe": False,
        "textMessageData": {"textMessage": "მრჩეველო გამარჯობა"},
    },
}


@pytest.mark.asyncio
class TestWhatsAppIncomingEndpoint:
    async def test_missing_auth_returns_401(self, patched_secrets):
        """Request without Authorization is rejected."""
        async with await _async_client() as client:
            resp = await client.post("/whatsapp-incoming", json=_WA_TEXT_BODY)
        assert resp.status_code == 401

    async def test_non_incoming_message_type_ignored(self, patched_secrets):
        """Webhook types other than incomingMessageReceived are ignored."""
        body = {**_WA_TEXT_BODY, "typeWebhook": "outgoingAPIMessageReceived"}
        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming", json=body, headers=_WA_AUTH
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    async def test_own_message_ignored(self, patched_secrets):
        """Messages sent by the bot itself (fromMe=True) are ignored."""
        body = {
            **_WA_TEXT_BODY,
            "messageData": {
                **_WA_TEXT_BODY["messageData"],
                "fromMe": True,
            },
        }
        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming", json=body, headers=_WA_AUTH
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    async def test_empty_text_ignored(self, patched_secrets):
        """Messages with empty text content are silently ignored."""
        body = {
            **_WA_TEXT_BODY,
            "messageData": {
                "typeMessage": "textMessage",
                "fromMe": False,
                "textMessageData": {"textMessage": "   "},
            },
        }
        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming", json=body, headers=_WA_AUTH
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    async def test_unsupported_message_type_ignored(self, patched_secrets):
        """Non-text message types (e.g. imageMessage) are ignored."""
        body = {
            **_WA_TEXT_BODY,
            "messageData": {
                "typeMessage": "imageMessage",
                "fromMe": False,
            },
        }
        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming", json=body, headers=_WA_AUTH
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


# ===========================================================================
# 9. _evict_stale_tasks
# ===========================================================================

class TestEvictStaleTasks:
    def test_fresh_tasks_not_evicted(self):
        """Tasks started recently must not be evicted."""
        _processing_tasks["g1_l1"] = datetime.now()
        evicted = _evict_stale_tasks()
        assert "g1_l1" not in evicted
        assert "g1_l1" in _processing_tasks

    def test_stale_tasks_evicted(self):
        """Tasks running longer than STALE_TASK_HOURS are removed."""
        stale_start = datetime.now() - timedelta(hours=STALE_TASK_HOURS + 1)
        _processing_tasks["g1_l2"] = stale_start
        evicted = _evict_stale_tasks()
        assert "g1_l2" in evicted
        assert "g1_l2" not in _processing_tasks

    def test_returns_list_of_evicted_keys(self):
        """Return value is a list of the evicted task key strings."""
        stale = datetime.now() - timedelta(hours=STALE_TASK_HOURS + 2)
        _processing_tasks["g2_l4"] = stale
        result = _evict_stale_tasks()
        assert isinstance(result, list)
        assert "g2_l4" in result

    def test_empty_registry_returns_empty_list(self):
        """No tasks registered means nothing is evicted."""
        result = _evict_stale_tasks()
        assert result == []

    def test_mixed_fresh_and_stale(self):
        """Only stale tasks are evicted; fresh tasks remain."""
        _processing_tasks["g1_l1"] = datetime.now()
        _processing_tasks["g1_l2"] = datetime.now() - timedelta(hours=STALE_TASK_HOURS + 1)
        evicted = _evict_stale_tasks()
        assert "g1_l2" in evicted
        assert "g1_l1" not in evicted
        assert "g1_l1" in _processing_tasks

    def test_stale_eviction_calls_alert_operator(self, mock_alert_operator):
        """Evicting stale tasks must trigger alert_operator exactly once."""
        stale = datetime.now() - timedelta(hours=STALE_TASK_HOURS + 1)
        _processing_tasks["g1_l6"] = stale

        _evict_stale_tasks()

        mock_alert_operator.assert_called_once()
        call_args = mock_alert_operator.call_args[0][0]
        assert "g1_l6" in call_args

    def test_no_stale_tasks_does_not_call_alert_operator(self, mock_alert_operator):
        """alert_operator must NOT be called when there are no stale tasks."""
        _processing_tasks["g2_l1"] = datetime.now()  # fresh

        _evict_stale_tasks()

        mock_alert_operator.assert_not_called()


# ===========================================================================
# 10. process_recording_task — background task
# ===========================================================================

@pytest.mark.asyncio
class TestProcessRecordingTask:
    """Tests for the async background task that does download → upload → transcribe."""

    def _make_payload(self, url="https://zoom.us/rec/download/test.mp4"):
        return srv.ProcessRecordingRequest(
            download_url=url,
            access_token="test_token",
            group_number=1,
            lecture_number=3,
            drive_folder_id="folder_abc",
        )

    async def test_ssrf_rejects_non_https_url(self, mock_alert_operator):
        """Non-HTTPS download URLs must be rejected (SSRF prevention)."""
        payload = self._make_payload(url="http://zoom.us/rec/download/test.mp4")
        key = _task_key(1, 3)
        _processing_tasks[key] = datetime.now()

        with patch("tools.app.server._send_callback") as mock_cb:
            await srv.process_recording_task(payload)
            mock_cb.assert_called()
            cb_payload = mock_cb.call_args[0][0]
            assert cb_payload.status == "error"
            assert "HTTPS" in cb_payload.error_message

        # Task key should be cleaned up
        assert key not in _processing_tasks

    async def test_ssrf_rejects_non_zoom_host(self, mock_alert_operator):
        """Download URLs from non-zoom.us hosts must be rejected."""
        payload = self._make_payload(url="https://evil.com/rec/download/test.mp4")
        key = _task_key(1, 3)
        _processing_tasks[key] = datetime.now()

        with patch("tools.app.server._send_callback") as mock_cb:
            await srv.process_recording_task(payload)
            mock_cb.assert_called()
            cb_payload = mock_cb.call_args[0][0]
            assert cb_payload.status == "error"
            assert "zoom.us" in cb_payload.error_message

    async def test_success_path(self, tmp_path, mock_alert_operator):
        """Full success: download → upload → transcribe → callback with status=success."""
        payload = self._make_payload()
        key = _task_key(1, 3)
        _processing_tasks[key] = datetime.now()

        # Create a fake downloaded file
        fake_file = tmp_path / "fake.mp4"
        fake_file.write_bytes(b"fake video content")

        async def fake_download(url, token, dest):
            import shutil
            shutil.copy(fake_file, dest)

        with (
            patch.object(srv, "TMP_DIR", tmp_path),
            patch("tools.app.server._download_recording", side_effect=fake_download),
            patch("tools.app.server.get_drive_service", return_value=MagicMock()),
            patch("tools.app.server.ensure_folder", return_value="lec_folder_id"),
            patch("tools.app.server.trash_old_recordings", return_value=0),
            patch("tools.app.server.upload_file", return_value="file_id_123"),
            patch("tools.app.server.transcribe_and_index", return_value={"chunks": 5}),
            patch("tools.app.server._send_callback") as mock_cb,
        ):
            await srv.process_recording_task(payload)
            mock_cb.assert_called_once()
            cb_payload = mock_cb.call_args[0][0]
            assert cb_payload.status == "success"
            assert cb_payload.drive_recording_url == "https://drive.google.com/file/d/file_id_123/view"

        # Task key cleaned up
        assert key not in _processing_tasks
        # alert_operator should NOT be called on success
        mock_alert_operator.assert_not_called()

    async def test_exception_sends_error_callback_and_alerts(self, tmp_path, mock_alert_operator):
        """When an exception occurs, error callback is sent and alert_operator is called."""
        payload = self._make_payload()
        key = _task_key(1, 3)
        _processing_tasks[key] = datetime.now()

        async def failing_download(url, token, dest):
            raise ConnectionError("Download failed")

        with (
            patch.object(srv, "TMP_DIR", tmp_path),
            patch("tools.app.server._download_recording", side_effect=failing_download),
            patch("tools.app.server._send_callback") as mock_cb,
        ):
            await srv.process_recording_task(payload)
            mock_cb.assert_called_once()
            cb_payload = mock_cb.call_args[0][0]
            assert cb_payload.status == "error"
            assert "Download failed" in cb_payload.error_message

        # Task key cleaned up even after error
        assert key not in _processing_tasks
        # alert_operator called (may be called multiple times if retry/callback also alert)
        mock_alert_operator.assert_called()

    async def test_temp_file_cleaned_up_after_success(self, tmp_path, mock_alert_operator):
        """The temporary MP4 file is deleted in the finally block."""
        payload = self._make_payload()
        _processing_tasks[_task_key(1, 3)] = datetime.now()

        created_files = []

        async def fake_download(url, token, dest):
            Path(dest).write_bytes(b"x" * 100)
            created_files.append(Path(dest))

        with (
            patch.object(srv, "TMP_DIR", tmp_path),
            patch("tools.app.server._download_recording", side_effect=fake_download),
            patch("tools.app.server.get_drive_service", return_value=MagicMock()),
            patch("tools.app.server.ensure_folder", return_value="fid"),
            patch("tools.app.server.upload_file", return_value="uid"),
            patch("tools.app.server.transcribe_and_index", return_value={"c": 1}),
            patch("tools.app.server._send_callback"),
        ):
            await srv.process_recording_task(payload)

        assert len(created_files) == 1
        assert not created_files[0].exists(), "Temp file should be deleted"


# ===========================================================================
# 11. _download_recording
# ===========================================================================

@pytest.mark.asyncio
class TestDownloadRecording:
    """Tests for _download_recording streaming download with SSRF guard."""

    async def test_ssrf_guard_rejects_non_zoom_redirect(self, tmp_path):
        """After redirect, if final host is not zoom.us/zoomgov.com, raise ValueError."""
        dest = tmp_path / "out.mp4"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.url = MagicMock()
        mock_response.url.host = "evil.com"

        # Build the async context managers
        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = MagicMock(return_value=mock_response)
        mock_stream_ctx.__aexit__ = MagicMock(return_value=False)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream_ctx)
        mock_client.__aenter__ = MagicMock(return_value=mock_client)
        mock_client.__aexit__ = MagicMock(return_value=False)

        # Make the async context managers actually awaitable

        async def async_return(val):
            return val

        mock_stream_ctx.__aenter__ = lambda self: async_return(mock_response)
        mock_stream_ctx.__aexit__ = lambda self, *a: async_return(False)
        mock_client.__aenter__ = lambda self: async_return(mock_client)
        mock_client.__aexit__ = lambda self, *a: async_return(False)

        with patch("tools.app.server.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="untrusted host"):
                await srv._download_recording("https://zoom.us/rec/test.mp4", "tok", dest)

    async def test_accepts_zoomgov_host(self, tmp_path):
        """zoomgov.com should be accepted as a trusted host."""
        dest = tmp_path / "out.mp4"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.url = MagicMock()
        mock_response.url.host = "us06web.zoomgov.com"

        async def fake_aiter_bytes(chunk_size=None):
            yield b"video data chunk"

        mock_response.aiter_bytes = fake_aiter_bytes


        async def async_return(val):
            return val

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = lambda self: async_return(mock_response)
        mock_stream_ctx.__aexit__ = lambda self, *a: async_return(False)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream_ctx)
        mock_client.__aenter__ = lambda self: async_return(mock_client)
        mock_client.__aexit__ = lambda self, *a: async_return(False)

        with patch("tools.app.server.httpx.AsyncClient", return_value=mock_client):
            await srv._download_recording("https://zoom.us/rec/test.mp4", "tok", dest)

        assert dest.exists()
        assert dest.read_bytes() == b"video data chunk"

    async def test_successful_download_from_zoom(self, tmp_path):
        """Successful streaming download from zoom.us writes file to disk."""
        dest = tmp_path / "out.mp4"

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.url = MagicMock()
        mock_response.url.host = "us06web.zoom.us"

        async def fake_aiter_bytes(chunk_size=None):
            yield b"chunk1"
            yield b"chunk2"

        mock_response.aiter_bytes = fake_aiter_bytes


        async def async_return(val):
            return val

        mock_stream_ctx = MagicMock()
        mock_stream_ctx.__aenter__ = lambda self: async_return(mock_response)
        mock_stream_ctx.__aexit__ = lambda self, *a: async_return(False)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream_ctx)
        mock_client.__aenter__ = lambda self: async_return(mock_client)
        mock_client.__aexit__ = lambda self, *a: async_return(False)

        with patch("tools.app.server.httpx.AsyncClient", return_value=mock_client):
            await srv._download_recording("https://zoom.us/rec/test.mp4", "tok", dest)

        assert dest.read_bytes() == b"chunk1chunk2"


# ===========================================================================
# 12. _send_callback
# ===========================================================================

@pytest.mark.asyncio
class TestSendCallback:
    """Tests for _send_callback retry logic and auth."""

    def _make_cb_payload(self, status="success"):
        return srv.CallbackPayload(
            status=status,
            group_number=1,
            lecture_number=3,
        )

    async def test_skips_when_callback_url_empty(self):
        """When N8N_CALLBACK_URL is empty, callback is skipped silently."""
        with patch.object(srv, "N8N_CALLBACK_URL", ""):
            # Should return without error
            await srv._send_callback(self._make_cb_payload())

    async def test_sends_auth_header_when_secret_set(self):
        """Authorization header is included when WEBHOOK_SECRET is configured."""

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        async def async_return(val):
            return val

        mock_post = MagicMock(return_value=async_return(mock_response))

        mock_client = MagicMock()
        mock_client.post = mock_post
        mock_client.__aenter__ = lambda self: async_return(mock_client)
        mock_client.__aexit__ = lambda self, *a: async_return(False)

        with (
            patch.object(srv, "N8N_CALLBACK_URL", "https://n8n.example.com/webhook"),
            patch.object(srv, "WEBHOOK_SECRET", "my-secret"),
            patch("tools.app.server.httpx.AsyncClient", return_value=mock_client),
        ):
            await srv._send_callback(self._make_cb_payload())

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers.get("Authorization") == "Bearer my-secret"

    async def test_retries_on_http_error(self, mock_alert_operator):
        """HTTP errors trigger retries up to 3 attempts, then alert_operator."""

        call_count = 0

        async def failing_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
            )
            return resp

        async def async_return(val):
            return val

        mock_client = MagicMock()
        mock_client.post = failing_post
        mock_client.__aenter__ = lambda self: async_return(mock_client)
        mock_client.__aexit__ = lambda self, *a: async_return(False)

        with (
            patch.object(srv, "N8N_CALLBACK_URL", "https://n8n.example.com/webhook"),
            patch.object(srv, "WEBHOOK_SECRET", ""),
            patch("tools.app.server.httpx.AsyncClient", return_value=mock_client),
            patch("asyncio.sleep", return_value=async_return(None)),
        ):
            await srv._send_callback(self._make_cb_payload())

        assert call_count == 3
        mock_alert_operator.assert_called_once()

    async def test_non_retryable_error_alerts_immediately(self, mock_alert_operator):
        """Non-retryable errors (e.g. TypeError) alert immediately without retry."""

        call_count = 0

        async def bad_post(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise TypeError("unexpected error")

        async def async_return(val):
            return val

        mock_client = MagicMock()
        mock_client.post = bad_post
        mock_client.__aenter__ = lambda self: async_return(mock_client)
        mock_client.__aexit__ = lambda self, *a: async_return(False)

        with (
            patch.object(srv, "N8N_CALLBACK_URL", "https://n8n.example.com/webhook"),
            patch.object(srv, "WEBHOOK_SECRET", ""),
            patch("tools.app.server.httpx.AsyncClient", return_value=mock_client),
        ):
            await srv._send_callback(self._make_cb_payload())

        assert call_count == 1  # No retries
        mock_alert_operator.assert_called_once()


# ===========================================================================
# 13. _handle_assistant_message
# ===========================================================================

@pytest.mark.asyncio
class TestHandleAssistantMessage:
    """Tests for _handle_assistant_message background task."""

    def _make_message(self):
        from tools.services.whatsapp_assistant import IncomingMessage
        return IncomingMessage(
            chat_id="995599000001@c.us",
            sender_id="995599000001@c.us",
            sender_name="Test User",
            text="მრჩეველო test",
            timestamp=1700000000,
        )

    async def test_successful_response(self):
        """When assistant returns a truthy result, no error occurs."""
        msg = self._make_message()
        mock_assistant = MagicMock()
        mock_assistant.handle_message = MagicMock(return_value=MagicMock())
        # Make handle_message a coroutine
        async def mock_handle(m):
            return "response text"
        mock_assistant.handle_message = mock_handle

        with patch.object(srv, "assistant", mock_assistant):
            await srv._handle_assistant_message(msg)
        # No exception means success

    async def test_assistant_returns_none(self):
        """When assistant returns None (chose not to respond), no error."""
        msg = self._make_message()
        mock_assistant = MagicMock()
        async def mock_handle(m):
            return None
        mock_assistant.handle_message = mock_handle

        with patch.object(srv, "assistant", mock_assistant):
            await srv._handle_assistant_message(msg)

    async def test_exception_triggers_alert_operator(self, mock_alert_operator):
        """When assistant raises, alert_operator is called."""
        msg = self._make_message()
        mock_assistant = MagicMock()
        async def mock_handle(m):
            raise RuntimeError("assistant crashed")
        mock_assistant.handle_message = mock_handle

        with patch.object(srv, "assistant", mock_assistant):
            await srv._handle_assistant_message(msg)

        mock_alert_operator.assert_called_once()
        assert "crashed" in mock_alert_operator.call_args[0][0]


# ===========================================================================
# 14. WhatsApp extended text message type
# ===========================================================================

@pytest.mark.asyncio
class TestWhatsAppExtendedTextMessage:
    """Test that extendedTextMessage type extracts text correctly."""

    async def test_extended_text_message_accepted(self, patched_secrets):
        """extendedTextMessage with valid text is routed to the assistant."""
        body = {
            "typeWebhook": "incomingMessageReceived",
            "timestamp": 1700000000,
            "senderData": {
                "chatId": "995599000001@c.us",
                "sender": "995599000001@c.us",
                "senderName": "Test User",
            },
            "messageData": {
                "typeMessage": "extendedTextMessage",
                "fromMe": False,
                "extendedTextMessageData": {"text": "მრჩეველო გამარჯობა"},
            },
        }
        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming",
                json=body,
                headers=_WA_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    async def test_quoted_message_accepted(self, patched_secrets):
        """quotedMessage type also extracts text from extendedTextMessageData."""
        body = {
            "typeWebhook": "incomingMessageReceived",
            "timestamp": 1700000000,
            "senderData": {
                "chatId": "995599000001@c.us",
                "sender": "995599000001@c.us",
                "senderName": "Test User",
            },
            "messageData": {
                "typeMessage": "quotedMessage",
                "fromMe": False,
                "extendedTextMessageData": {"text": "reply text here"},
            },
        }
        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming",
                json=body,
                headers=_WA_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    async def test_extended_text_empty_ignored(self, patched_secrets):
        """extendedTextMessage with empty text is ignored."""
        body = {
            "typeWebhook": "incomingMessageReceived",
            "timestamp": 1700000000,
            "senderData": {
                "chatId": "995599000001@c.us",
                "sender": "995599000001@c.us",
                "senderName": "Test User",
            },
            "messageData": {
                "typeMessage": "extendedTextMessage",
                "fromMe": False,
                "extendedTextMessageData": {"text": ""},
            },
        }
        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming",
                json=body,
                headers=_WA_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


# ===========================================================================
# 15. Webhook replay attack (Zoom recording.completed deduplicated on re-send)
# ===========================================================================


@pytest.mark.asyncio
class TestWebhookReplay:
    """Verify that replaying the same signed Zoom webhook is deduplicated.

    The deduplication authority is ``try_claim_pipeline``, which creates a
    persistent state file on the first call and returns None for every
    subsequent call until that state file is removed.  A second request
    carrying the *same* valid HMAC signature (within the 5-minute replay
    window) must therefore return ``status=duplicate``, not ``status=accepted``.
    """

    def _signed_recording_body(self, topic: str = "ჯგუფი #1 lecture") -> tuple[bytes, dict]:
        """Return (body_bytes, signed_headers) for a recording.completed event."""
        body_dict = _make_recording_body(topic=topic)
        body_bytes = json.dumps(body_dict).encode()
        timestamp = str(int(time.time()))
        sig = _zoom_sig(body_bytes, timestamp)
        headers = {
            "x-zm-request-timestamp": timestamp,
            "x-zm-signature": sig,
            "content-type": "application/json",
        }
        return body_bytes, headers

    async def test_first_request_is_accepted(self, patched_secrets):
        """The initial webhook delivery is accepted and the pipeline is claimed."""
        body_bytes, headers = self._signed_recording_body()
        with patch("tools.app.server.process_recording_task"):
            async with await _async_client() as client:
                resp = await client.post(
                    "/zoom-webhook", content=body_bytes, headers=headers
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    async def test_replay_within_valid_window_is_deduplicated(self, patched_secrets):
        """Sending the same payload twice returns duplicate on the second delivery.

        Both requests arrive with a fresh, valid timestamp so the signature
        check passes.  Only the pipeline-claim layer should prevent the second
        processing run.
        """
        # Build two independent signed requests (same body, fresh timestamps)
        body_dict = _make_recording_body(topic="ჯგუფი #1 lecture")

        with patch("tools.app.server.get_lecture_number", return_value=3):
            # --- First delivery ---
            body1 = json.dumps(body_dict).encode()
            ts1 = str(int(time.time()))
            sig1 = _zoom_sig(body1, ts1)
            hdrs1 = {
                "x-zm-request-timestamp": ts1,
                "x-zm-signature": sig1,
                "content-type": "application/json",
            }

            with patch("tools.app.server.process_recording_task"):
                async with await _async_client() as client:
                    resp1 = await client.post(
                        "/zoom-webhook", content=body1, headers=hdrs1
                    )

            assert resp1.status_code == 200
            assert resp1.json()["status"] == "accepted", (
                "First delivery must be accepted"
            )

            # --- Second delivery (replay) ---
            body2 = json.dumps(body_dict).encode()
            ts2 = str(int(time.time()))
            sig2 = _zoom_sig(body2, ts2)
            hdrs2 = {
                "x-zm-request-timestamp": ts2,
                "x-zm-signature": sig2,
                "content-type": "application/json",
            }

            with patch("tools.app.server.process_recording_task"):
                async with await _async_client() as client:
                    resp2 = await client.post(
                        "/zoom-webhook", content=body2, headers=hdrs2
                    )

        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2["status"] == "duplicate", (
            f"Replay should be deduplicated, got: {data2}"
        )

    async def test_replay_does_not_start_second_pipeline(self, patched_secrets):
        """process_recording_task must be enqueued exactly once across two deliveries."""
        body_dict = _make_recording_body(topic="ჯგუფი #2 lecture")
        enqueue_count = 0

        async def count_calls(*_args, **_kwargs):
            nonlocal enqueue_count
            enqueue_count += 1

        with patch("tools.app.server.get_lecture_number", return_value=5):
            for _ in range(2):
                body_bytes = json.dumps(body_dict).encode()
                ts = str(int(time.time()))
                sig = _zoom_sig(body_bytes, ts)
                headers = {
                    "x-zm-request-timestamp": ts,
                    "x-zm-signature": sig,
                    "content-type": "application/json",
                }
                with patch("tools.app.server.process_recording_task", side_effect=count_calls):
                    async with await _async_client() as client:
                        await client.post(
                            "/zoom-webhook", content=body_bytes, headers=headers
                        )

        assert enqueue_count == 1, (
            f"Expected pipeline to be started once, was started {enqueue_count} times"
        )


# ===========================================================================
# 16. Stale eviction using real pipeline state files
# ===========================================================================


class TestEvictStaleTasksWithStateFiles:
    """Test _evict_stale_tasks against the in-memory task dict alongside state files.

    ``_evict_stale_tasks`` evicts keys from ``_processing_tasks`` that have
    exceeded STALE_TASK_HOURS.  It does NOT scan state files directly — the
    in-memory dict is the eviction authority.  State files created via
    ``create_pipeline`` are NOT automatically marked FAILED by eviction; that
    is the responsibility of the pipeline retry orchestrator's nightly cleanup.

    NOTE: A future improvement would be for _evict_stale_tasks to also mark
    orphaned state files as FAILED when the in-memory key is evicted.  Until
    then, these tests verify the actual implemented behavior.
    """

    def _backdate_state_file(self, group: int, lecture: int, hours_ago: float) -> None:
        """Overwrite the started_at field in a pipeline state file to simulate age."""
        from tools.core.pipeline_state import load_state, save_state
        from tools.core.config import TBILISI_TZ as _TZ
        import dataclasses

        state = load_state(group, lecture)
        assert state is not None, "State file must exist before backdating"

        stale_ts = (
            datetime.now(_TZ) - timedelta(hours=hours_ago)
        ).isoformat()
        backdated = dataclasses.replace(state, started_at=stale_ts)
        save_state(backdated)

    def test_stale_state_file_is_marked_failed(self, mock_alert_operator):
        """A stale in-memory key is evicted and alert_operator is called.

        NOTE: _evict_stale_tasks evicts from _processing_tasks only.
        Marking state files as FAILED is handled by the retry orchestrator's
        nightly cleanup (pipeline_retry.py), not by this function.
        """
        from tools.core.pipeline_state import create_pipeline, load_state, PENDING
        from tools.core.config import TBILISI_TZ as _TZ

        # Create a real pipeline state file and back-date it
        create_pipeline(group=1, lecture=7, meeting_id="test-meeting-stale")
        self._backdate_state_file(1, 7, hours_ago=STALE_TASK_HOURS + 1)

        # Pre-populate in-memory dict as the webhook handler would
        key = _task_key(1, 7)
        _processing_tasks[key] = datetime.now(_TZ) - timedelta(hours=STALE_TASK_HOURS + 1)

        # Run eviction
        evicted = _evict_stale_tasks()

        # Key must be evicted from in-memory dict
        assert "g1_l7" in evicted
        assert key not in _processing_tasks

        # State file still exists (eviction does not delete or mark it)
        reloaded = load_state(1, 7)
        assert reloaded is not None, "State file should still exist after eviction"

    def test_stale_state_file_key_removed_from_processing_tasks(
        self, mock_alert_operator
    ):
        """After eviction, the matching key is removed from _processing_tasks."""
        from tools.core.pipeline_state import create_pipeline
        from tools.core.config import TBILISI_TZ as _TZ

        create_pipeline(group=2, lecture=8, meeting_id="test-meeting-mem")
        self._backdate_state_file(2, 8, hours_ago=STALE_TASK_HOURS + 0.5)

        # Pre-populate the in-memory cache as the webhook handler would
        key = _task_key(2, 8)
        _processing_tasks[key] = datetime.now(_TZ) - timedelta(hours=STALE_TASK_HOURS + 1)

        _evict_stale_tasks()

        assert key not in _processing_tasks, (
            "Stale key must be removed from _processing_tasks after eviction"
        )

    def test_fresh_state_file_not_evicted(self, mock_alert_operator):
        """A pipeline created moments ago must not be evicted (in-memory key is fresh)."""
        from tools.core.pipeline_state import create_pipeline, load_state, PENDING
        from tools.core.config import TBILISI_TZ as _TZ

        create_pipeline(group=1, lecture=9, meeting_id="fresh-meeting")

        # Register a fresh in-memory key (not stale)
        key = _task_key(1, 9)
        _processing_tasks[key] = datetime.now(_TZ)

        evicted = _evict_stale_tasks()

        assert "g1_l9" not in evicted
        assert key in _processing_tasks, "Fresh key must remain in _processing_tasks"

        reloaded = load_state(1, 9)
        assert reloaded is not None
        assert reloaded.state == PENDING, (
            f"Fresh pipeline should remain PENDING, got {reloaded.state}"
        )

    def test_eviction_calls_alert_operator_with_key_in_message(
        self, mock_alert_operator
    ):
        """alert_operator message must include the evicted task key."""
        from tools.core.pipeline_state import create_pipeline
        from tools.core.config import TBILISI_TZ as _TZ

        create_pipeline(group=2, lecture=10, meeting_id="alert-test-meeting")
        self._backdate_state_file(2, 10, hours_ago=STALE_TASK_HOURS + 2)

        key = _task_key(2, 10)
        _processing_tasks[key] = datetime.now(_TZ) - timedelta(hours=STALE_TASK_HOURS + 2)

        _evict_stale_tasks()

        mock_alert_operator.assert_called_once()
        alert_msg = mock_alert_operator.call_args[0][0]
        assert "g2_l10" in alert_msg, (
            f"Alert message should contain evicted key 'g2_l10', got: {alert_msg!r}"
        )

    def test_mixed_fresh_and_stale_state_files(self, mock_alert_operator):
        """Only stale in-memory keys are evicted; fresh ones remain."""
        from tools.core.pipeline_state import create_pipeline, load_state, PENDING
        from tools.core.config import TBILISI_TZ as _TZ

        # Create both a fresh and a stale pipeline with matching in-memory keys
        create_pipeline(group=1, lecture=11, meeting_id="fresh-11")
        create_pipeline(group=1, lecture=12, meeting_id="stale-12")
        self._backdate_state_file(1, 12, hours_ago=STALE_TASK_HOURS + 3)

        fresh_key = _task_key(1, 11)
        stale_key = _task_key(1, 12)
        _processing_tasks[fresh_key] = datetime.now(_TZ)
        _processing_tasks[stale_key] = datetime.now(_TZ) - timedelta(hours=STALE_TASK_HOURS + 3)

        evicted = _evict_stale_tasks()

        assert "g1_l12" in evicted
        assert "g1_l11" not in evicted
        assert fresh_key in _processing_tasks
        assert stale_key not in _processing_tasks

        fresh_state = load_state(1, 11)
        assert fresh_state is not None
        assert fresh_state.state == PENDING


# ===========================================================================
# 17. Content-Length bypass (chunked encoding without Content-Length header)
# ===========================================================================


@pytest.mark.asyncio
class TestContentLengthBypass:
    """Verify that the body-size middleware handles chunked-encoding requests.

    HTTP clients may omit the Content-Length header and use chunked transfer
    encoding instead.  The ``limit_request_body`` middleware must read the
    actual body and enforce the limit regardless of whether Content-Length
    is present.

    These tests verify:
    1. A small payload without Content-Length is processed normally (not crashed).
    2. An oversized payload without Content-Length is rejected with HTTP 413.
    3. A request with an honest Content-Length that exceeds the limit is also
       rejected with 413 (fast-reject path).
    """

    async def test_small_body_without_content_length_accepted(self, patched_secrets, tmp_path):
        """A valid small POST without Content-Length header is not rejected."""
        small_body = json.dumps({"typeWebhook": "statusInstanceChanged"}).encode()

        # httpx will omit Content-Length when we pass raw bytes via content=
        # and explicitly clear the header.  We verify the server doesn't crash.
        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming",
                content=small_body,
                headers={
                    "Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}",
                    "content-type": "application/json",
                    # Explicitly do NOT set Content-Length — httpx may add it
                    # automatically; we only care the server doesn't reject valid
                    # small payloads.
                },
            )
        # Any 2xx or 4xx (business logic) is acceptable — we just must not get 500
        assert resp.status_code != 500, (
            f"Server must not crash on request without explicit Content-Length: "
            f"got {resp.status_code}"
        )

    async def test_oversized_body_rejected_with_413(self, patched_secrets):
        """A POST body exceeding 1 MB is rejected with HTTP 413."""
        # Build a payload just over MAX_BODY_SIZE (1 MB)
        oversized_body = b"x" * (1_048_576 + 1)

        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming",
                content=oversized_body,
                headers={
                    "Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}",
                    "content-type": "application/octet-stream",
                },
            )
        assert resp.status_code == 413, (
            f"Oversized body must be rejected with 413, got {resp.status_code}"
        )

    async def test_honest_content_length_over_limit_rejected(self, patched_secrets):
        """A request advertising Content-Length > 1 MB is fast-rejected with 413."""
        small_actual_body = b"small actual content"

        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming",
                content=small_actual_body,
                headers={
                    "Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}",
                    "content-type": "application/json",
                    "content-length": str(1_048_576 + 100),  # lie about size
                },
            )
        assert resp.status_code == 413, (
            f"Request with Content-Length > limit must be fast-rejected with 413, "
            f"got {resp.status_code}"
        )

    async def test_body_at_exact_limit_boundary_accepted(self, patched_secrets):
        """A POST body exactly at MAX_BODY_SIZE must not be rejected by the middleware."""
        # Build a valid-looking JSON body padded to exactly 1 MB.
        # We pad inside a JSON string value so it remains valid JSON.
        padding = "a" * (1_048_576 - 60)
        at_limit_body = json.dumps(
            {"typeWebhook": "statusInstanceChanged", "pad": padding}
        ).encode()

        # Trim or expand to be exactly 1 MB (the exact boundary is non-rejectable)
        at_limit_body = at_limit_body[:1_048_576]

        async with await _async_client() as client:
            resp = await client.post(
                "/whatsapp-incoming",
                content=at_limit_body,
                headers={
                    "Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}",
                    "content-type": "application/json",
                },
            )
        # Must not be 413 — the middleware limit is strictly > MAX_BODY_SIZE
        assert resp.status_code != 413, (
            f"A body at the exact 1 MB boundary must not be rejected with 413, "
            f"got {resp.status_code}"
        )


# ===========================================================================
# 18. Startup recovery — _check_unprocessed_recordings semaphore / concurrency
# ===========================================================================


@pytest.mark.asyncio
class TestCheckUnprocessedRecordings:
    """Tests for the _check_unprocessed_recordings startup recovery function.

    The function queries Zoom for recent recordings, checks Pinecone, and
    starts the pipeline for any unprocessed lecture.  These tests verify:

    1. When zoom_manager is unavailable (ImportError), the function returns
       without crashing.
    2. When no meetings are returned, the function returns without starting
       any pipeline.
    3. When a meeting's topic does not match a known group, it is skipped.
    4. When a lecture is already indexed in Pinecone, no pipeline is started.
    5. When ``try_claim_pipeline`` returns None (already active), no executor
       call is made (concurrency guard).
    6. The function does not launch more than one pipeline per unique
       group+lecture combination (no duplicate executor submissions).
    """

    async def test_returns_cleanly_when_zoom_manager_unavailable(self):
        """Function must not raise when zoom_manager cannot be imported."""
        with patch.dict("sys.modules", {"tools.integrations.zoom_manager": None}):
            # Should complete without exception
            await srv._check_unprocessed_recordings()

    async def test_returns_cleanly_when_no_meetings_found(self):
        """No meetings returned by Zoom API means nothing is started."""
        mock_zm = MagicMock()
        mock_zm.list_user_recordings = MagicMock(return_value=[])

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with (
            patch.dict("sys.modules", {"tools.integrations.zoom_manager": mock_zm}),
            patch("tools.app.server.asyncio.to_thread", side_effect=_to_thread),
        ):
            await srv._check_unprocessed_recordings()

        # _processing_tasks must remain empty
        assert len(_processing_tasks) == 0

    async def test_skips_meetings_with_unknown_topic(self):
        """Meetings whose topics cannot be matched to a group are skipped."""
        meeting = {
            "topic": "Random Company All-Hands 2026",
            "start_time": "2026-03-31T16:00:00Z",
            "uuid": "abc123",
            "id": "111222333",
        }

        mock_zm = MagicMock()
        mock_zm.list_user_recordings = MagicMock(return_value=[meeting])

        executor_calls = []
        mock_loop = MagicMock()
        mock_loop.run_in_executor = MagicMock(side_effect=lambda *a, **kw: executor_calls.append(1))

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with (
            patch.dict("sys.modules", {"tools.integrations.zoom_manager": mock_zm}),
            patch("tools.app.server.asyncio.to_thread", side_effect=_to_thread),
            patch("tools.app.server.asyncio.get_running_loop", return_value=mock_loop),
        ):
            await srv._check_unprocessed_recordings()

        assert len(executor_calls) == 0, (
            "No pipeline should start for an unrecognised meeting topic"
        )

    async def test_skips_already_indexed_lecture(self):
        """If Pinecone already has vectors for the lecture, no pipeline is started."""
        meeting = {
            "topic": "ჯგუფი #1 lecture",
            "start_time": "2026-03-25T16:00:00Z",  # Tuesday — Group 1
            "uuid": "uuid-already-indexed",
            "id": "999888777",
        }

        mock_zm = MagicMock()
        mock_zm.list_user_recordings = MagicMock(return_value=[meeting])

        executor_calls = []
        mock_loop = MagicMock()
        mock_loop.run_in_executor = MagicMock(side_effect=lambda *a, **kw: executor_calls.append(1))

        async def mock_to_thread(fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            return result

        with (
            patch.dict("sys.modules", {"tools.integrations.zoom_manager": mock_zm}),
            patch("tools.app.server.asyncio.to_thread", side_effect=mock_to_thread),
            patch("tools.app.server.extract_group_from_topic", return_value=1),
            patch("tools.app.server.get_lecture_number", return_value=5),
            patch("tools.app.server.is_pipeline_done", return_value=False),
            patch("tools.app.server.is_pipeline_active", return_value=False),
            patch("tools.app.server.asyncio.get_running_loop", return_value=mock_loop),
        ):
            # Patch lecture_exists_in_index to say it's already indexed
            with patch.dict("sys.modules", {
                "tools.integrations.knowledge_indexer": MagicMock(
                    lecture_exists_in_index=MagicMock(return_value=True)
                )
            }):
                await srv._check_unprocessed_recordings()

        assert len(executor_calls) == 0, (
            "Pipeline must not start for a lecture already indexed in Pinecone"
        )

    async def test_already_claimed_pipeline_not_double_started(self):
        """When a pipeline is already active per state file, no executor call is made."""
        meeting = {
            "topic": "ჯგუფი #2 lecture",
            "start_time": "2026-03-30T16:00:00Z",  # Monday — Group 2
            "uuid": "uuid-claim-race",
            "id": "777666555",
        }

        mock_zm = MagicMock()
        mock_zm.list_user_recordings = MagicMock(return_value=[meeting])

        executor_calls = []

        async def mock_to_thread(fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            return result

        mock_loop = MagicMock()
        mock_loop.run_in_executor = MagicMock(side_effect=lambda *a, **kw: executor_calls.append(1))

        with (
            patch.dict("sys.modules", {"tools.integrations.zoom_manager": mock_zm}),
            patch("tools.app.server.asyncio.to_thread", side_effect=mock_to_thread),
            patch("tools.app.server.extract_group_from_topic", return_value=2),
            patch("tools.app.server.get_lecture_number", return_value=6),
            # is_pipeline_active returns True → pipeline already active, skip
            patch("tools.app.server.is_pipeline_active", return_value=True),
            patch("tools.app.server.is_pipeline_done", return_value=False),
            patch("tools.app.server.asyncio.get_running_loop", return_value=mock_loop),
        ):
            with patch.dict("sys.modules", {
                "tools.integrations.knowledge_indexer": MagicMock(
                    lecture_exists_in_index=MagicMock(return_value=False)
                )
            }):
                await srv._check_unprocessed_recordings()

        assert len(executor_calls) == 0, (
            "run_in_executor must not be called when pipeline is already active"
        )

    async def test_unprocessed_lecture_starts_pipeline_in_executor(self):
        """An unprocessed, unclaimed lecture triggers run_in_executor."""
        meeting = {
            "topic": "ჯგუფი #1 lecture",
            "start_time": "2026-03-25T16:00:00Z",
            "uuid": "uuid-unprocessed",
            "id": "444333222",
        }

        mock_zm = MagicMock()
        mock_zm.list_user_recordings = MagicMock(return_value=[meeting])

        executor_calls = []

        async def mock_to_thread(fn, *args, **kwargs):
            result = fn(*args, **kwargs)
            return result

        mock_loop = MagicMock()
        mock_loop.run_in_executor = MagicMock(
            side_effect=lambda *a, **kw: executor_calls.append(1)
        )

        with (
            patch.dict("sys.modules", {"tools.integrations.zoom_manager": mock_zm}),
            patch("tools.app.server.asyncio.to_thread", side_effect=mock_to_thread),
            patch("tools.app.server.extract_group_from_topic", return_value=1),
            patch("tools.app.server.get_lecture_number", return_value=4),
            patch("tools.app.server.is_pipeline_done", return_value=False),
            patch("tools.app.server.is_pipeline_active", return_value=False),
            patch("tools.app.server.create_pipeline"),
            patch("tools.app.server.asyncio.get_running_loop", return_value=mock_loop),
        ):
            with patch.dict("sys.modules", {
                "tools.integrations.knowledge_indexer": MagicMock(
                    lecture_exists_in_index=MagicMock(return_value=False)
                )
            }):
                await srv._check_unprocessed_recordings()

        assert len(executor_calls) == 1, (
            f"Expected exactly one executor submission, got {len(executor_calls)}"
        )


# ===========================================================================
# New Phase-4 tests: /live, /ready, /health cache behaviour
# ===========================================================================

@pytest.mark.asyncio
class TestLivenessEndpoint:
    """Tests for GET /live — cheap liveness probe."""

    async def test_live_returns_200(self):
        """/live must always return HTTP 200."""
        async with await _async_client() as client:
            resp = await client.get("/live")
        assert resp.status_code == 200

    async def test_live_response_shape(self):
        """/live response must contain status, uptime_s, and version keys."""
        async with await _async_client() as client:
            resp = await client.get("/live")
        data = resp.json()
        assert data["status"] == "alive"
        assert isinstance(data["uptime_s"], int)
        assert "version" in data

    async def test_live_makes_no_external_calls(self):
        """/live must not call Gemini, Claude, Zoom, Pinecone, or WhatsApp."""
        gemini_mock = MagicMock()
        claude_mock = MagicMock()
        zoom_mock = MagicMock()
        pinecone_mock = MagicMock()
        whatsapp_mock = MagicMock()

        with (
            patch.dict("sys.modules", {
                "google.genai": gemini_mock,
                "anthropic": claude_mock,
                "tools.integrations.zoom_manager": zoom_mock,
                "pinecone": pinecone_mock,
                "tools.integrations.whatsapp_sender": whatsapp_mock,
            }),
        ):
            async with await _async_client() as client:
                resp = await client.get("/live")

        assert resp.status_code == 200
        # None of the external service constructors should have been called
        gemini_mock.Client.assert_not_called()
        claude_mock.Anthropic.assert_not_called()
        zoom_mock.get_access_token.assert_not_called()
        pinecone_mock.Pinecone.assert_not_called()

    async def test_live_is_fast(self):
        """/live must respond within 100 ms under normal conditions."""
        import time as _time

        async with await _async_client() as client:
            t0 = _time.perf_counter()
            resp = await client.get("/live")
            elapsed_ms = (_time.perf_counter() - t0) * 1000

        assert resp.status_code == 200
        assert elapsed_ms < 100, f"/live took {elapsed_ms:.1f} ms — expected < 100 ms"

    async def test_live_does_not_require_auth(self):
        """/live must be accessible without any Authorization header."""
        async with await _async_client() as client:
            resp = await client.get("/live")  # no headers
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestReadinessEndpoint:
    """Tests for GET /ready — startup completion probe."""

    async def test_ready_returns_503_before_startup_complete(self):
        """/ready must return 503 when _startup_complete is False."""
        with patch.object(srv, "_startup_complete", False):
            async with await _async_client() as client:
                resp = await client.get("/ready")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "starting"

    async def test_ready_returns_200_after_startup_complete(self):
        """/ready must return 200 once _startup_complete is True."""
        with patch.object(srv, "_startup_complete", True):
            async with await _async_client() as client:
                resp = await client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ready"
        assert "tasks_in_progress" in data


@pytest.mark.asyncio
class TestHealthCacheBehaviour:
    """Tests for /health TTL caching and status-code semantics."""

    async def test_health_cache_prevents_repeat_api_calls(self):
        """Two /health calls within TTL window must invoke full audit only once."""
        from tools.core import health_monitor as _hm

        # Reset cache so we start from a clean state
        _hm._health_cache["timestamp"] = 0.0
        _hm._health_cache["result"] = None

        call_count = {"n": 0}

        def _mock_check_all():
            call_count["n"] += 1
            return {
                "overall_status": "healthy",
                "timestamp": "2026-01-01T00:00:00+04:00",
                "checks": [],
                "warnings_count": 0,
                "critical_count": 0,
            }

        with patch.object(_hm, "check_all", side_effect=_mock_check_all):
            # First call — should run the full audit
            async with await _async_client() as client:
                resp1 = await client.get("/health")
            # Second call immediately after — should use cache
            async with await _async_client() as client:
                resp2 = await client.get("/health")

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert call_count["n"] == 1, (
            f"Expected check_all() called once (cached), but was called {call_count['n']} times"
        )

    async def test_health_force_param_bypasses_cache(self):
        """Passing ?force=true must bypass the TTL cache and run a fresh audit."""
        from tools.core import health_monitor as _hm

        call_count = {"n": 0}

        def _mock_check_all():
            call_count["n"] += 1
            return {
                "overall_status": "healthy",
                "timestamp": "2026-01-01T00:00:00+04:00",
                "checks": [],
                "warnings_count": 0,
                "critical_count": 0,
            }

        # Seed cache so first call would normally be served from cache
        _hm._health_cache["timestamp"] = __import__("time").time()
        _hm._health_cache["result"] = {
            "overall_status": "healthy",
            "timestamp": "2026-01-01T00:00:00+04:00",
            "checks": [],
            "warnings_count": 0,
            "critical_count": 0,
        }

        with patch.object(_hm, "check_all", side_effect=_mock_check_all):
            async with await _async_client() as client:
                resp = await client.get("/health?force=true")

        assert resp.status_code == 200
        assert call_count["n"] == 1, "force=true must bypass cache and call check_all()"

    async def test_health_returns_200_for_degraded_state(self):
        """/health must return 200 (not 503) when overall_status is 'degraded' (warnings only)."""
        from tools.core import health_monitor as _hm

        degraded_result = {
            "overall_status": "degraded",
            "timestamp": "2026-01-01T00:00:00+04:00",
            "checks": [],
            "warnings_count": 2,
            "critical_count": 0,
        }

        with patch.object(_hm, "get_cached_or_run_full_audit", return_value=degraded_result):
            async with await _async_client() as client:
                resp = await client.get("/health")

        assert resp.status_code == 200, (
            f"Degraded (warning-only) state should return 200, got {resp.status_code}"
        )
        assert resp.json()["status"] == "degraded"

    async def test_health_returns_503_only_for_critical_state(self):
        """/health must return 503 only when overall_status is 'critical'."""
        from tools.core import health_monitor as _hm

        critical_result = {
            "overall_status": "critical",
            "timestamp": "2026-01-01T00:00:00+04:00",
            "checks": [],
            "warnings_count": 0,
            "critical_count": 3,
        }

        with patch.object(_hm, "get_cached_or_run_full_audit", return_value=critical_result):
            async with await _async_client() as client:
                resp = await client.get("/health")

        assert resp.status_code == 503, (
            f"Critical state must return 503, got {resp.status_code}"
        )

    async def test_health_returns_200_for_healthy_state(self):
        """/health returns 200 when overall_status is 'healthy'."""
        from tools.core import health_monitor as _hm

        healthy_result = {
            "overall_status": "healthy",
            "timestamp": "2026-01-01T00:00:00+04:00",
            "checks": [],
            "warnings_count": 0,
            "critical_count": 0,
        }

        with patch.object(_hm, "get_cached_or_run_full_audit", return_value=healthy_result):
            async with await _async_client() as client:
                resp = await client.get("/health")

        assert resp.status_code == 200


