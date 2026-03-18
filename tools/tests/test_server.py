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
    """Reset the in-flight task registry before every test."""
    _processing_tasks.clear()
    yield
    _processing_tasks.clear()


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
    async def test_healthy_returns_200(self, patched_secrets, tmp_path):
        """GET /health returns 200 and status=healthy when WEBHOOK_SECRET is set."""
        with patch.object(srv, "TMP_DIR", tmp_path):
            async with await _async_client() as client:
                resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["service"] == "training-agent"

    async def test_healthy_response_has_timestamp(self, patched_secrets, tmp_path):
        """Health response includes an ISO timestamp."""
        with patch.object(srv, "TMP_DIR", tmp_path):
            async with await _async_client() as client:
                resp = await client.get("/health")
        data = resp.json()
        assert "timestamp" in data
        # Verify it is at least parseable as a datetime string
        datetime.fromisoformat(data["timestamp"])

    async def test_healthy_includes_checks_dict(self, patched_secrets, tmp_path):
        """Health response includes a checks dict with expected keys."""
        with patch.object(srv, "TMP_DIR", tmp_path):
            async with await _async_client() as client:
                resp = await client.get("/health")
        data = resp.json()
        assert "checks" in data
        checks = data["checks"]
        assert "webhook_secret" in checks
        assert "tmp_dir" in checks
        assert "tasks_in_progress" in checks

    async def test_degraded_when_webhook_secret_missing(self, tmp_path):
        """GET /health returns 503 when WEBHOOK_SECRET is not configured."""
        with (
            patch.object(srv, "WEBHOOK_SECRET", ""),
            patch.object(srv, "TMP_DIR", tmp_path),
        ):
            async with await _async_client() as client:
                resp = await client.get("/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"

    async def test_webhook_secret_check_shows_missing(self, tmp_path):
        """Health checks dict shows MISSING when WEBHOOK_SECRET is unset."""
        with (
            patch.object(srv, "WEBHOOK_SECRET", ""),
            patch.object(srv, "TMP_DIR", tmp_path),
        ):
            async with await _async_client() as client:
                resp = await client.get("/health")
        data = resp.json()
        assert data["checks"]["webhook_secret"] == "MISSING"

    async def test_in_flight_task_count_reflected(self, patched_secrets, tmp_path):
        """Health endpoint reports the current number of in-progress tasks."""
        _processing_tasks["g1_l3"] = datetime.now()
        _processing_tasks["g2_l5"] = datetime.now()
        with patch.object(srv, "TMP_DIR", tmp_path):
            async with await _async_client() as client:
                resp = await client.get("/health")
        data = resp.json()
        assert data["checks"]["tasks_in_progress"] == "2"


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
        """The limiter must be an instance of slowapi.Limiter."""
        from slowapi import Limiter
        assert isinstance(app.state.limiter, Limiter)


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
        # alert_operator called
        mock_alert_operator.assert_called_once()

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
