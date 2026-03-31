"""Tests for admin API endpoints (tools/app/admin_routes.py).

Covers:
- POST /admin/retry-lecture: auth, validation, FAILED/COMPLETE/active retry
- POST /admin/reset-pipeline: auth, no-pipeline, active reset, already-terminal
- GET /admin/lecture-status: auth, returns correct structure and summary
- POST /admin/force-refresh-token: auth, success, failure
- GET /admin/system-report: auth, report structure, uptime, error listing

Run with:
    pytest tools/tests/test_admin_routes.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# Pop stubs for packages that this test needs real implementations of.
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

import tools.app.server as srv  # noqa: E402
import tools.integrations.whatsapp_sender as _wa_sender_mod  # noqa: E402
from tools.app.server import (  # noqa: E402
    _processing_lock,
    _processing_tasks,
    _task_key,
    app,
)
from tools.core.config import TMP_DIR  # noqa: E402
from tools.core.pipeline_state import (  # noqa: E402
    ANALYZING,
    COMPLETE,
    FAILED,
    PipelineState,
    save_state,
    state_file_path,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_WEBHOOK_SECRET = "test-secret-abc"
_AUTH_HEADER = {"Authorization": f"Bearer {_TEST_WEBHOOK_SECRET}"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_state():
    """Reset task registry and pipeline state files before/after each test."""
    _processing_tasks.clear()
    for f in TMP_DIR.glob("pipeline_state_*.json"):
        f.unlink(missing_ok=True)
    yield
    _processing_tasks.clear()
    for f in TMP_DIR.glob("pipeline_state_*.json"):
        f.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def mock_alert_operator():
    with patch.object(_wa_sender_mod, "alert_operator", new_callable=MagicMock) as m:
        with patch.object(srv, "alert_operator", m):
            yield m


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    srv.limiter.reset()
    yield
    srv.limiter.reset()


@pytest.fixture
def patched_secrets():
    """Patch WEBHOOK_SECRET on the live server module in sys.modules.

    When running after test_server.py, the module may have been popped and
    reimported, so we patch whatever object is currently in sys.modules.
    """
    live_srv = sys.modules.get("tools.app.server", srv)
    with patch.object(live_srv, "WEBHOOK_SECRET", _TEST_WEBHOOK_SECRET):
        yield


async def _client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://localhost")


# ===========================================================================
# POST /admin/retry-lecture
# ===========================================================================


@pytest.mark.asyncio
class TestRetryLecture:
    async def test_rejects_missing_auth(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post(
                "/admin/retry-lecture",
                json={"group_number": 1, "lecture_number": 6},
            )
        assert resp.status_code == 401

    async def test_rejects_invalid_auth(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post(
                "/admin/retry-lecture",
                json={"group_number": 1, "lecture_number": 6},
                headers={"Authorization": "Bearer wrong"},
            )
        assert resp.status_code == 403

    async def test_validates_group_number(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post(
                "/admin/retry-lecture",
                json={"group_number": 3, "lecture_number": 1},
                headers=_AUTH_HEADER,
            )
        assert resp.status_code == 422

    async def test_validates_lecture_number(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post(
                "/admin/retry-lecture",
                json={"group_number": 1, "lecture_number": 0},
                headers=_AUTH_HEADER,
            )
        assert resp.status_code == 422

    async def test_validates_lecture_number_too_high(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post(
                "/admin/retry-lecture",
                json={"group_number": 1, "lecture_number": 16},
                headers=_AUTH_HEADER,
            )
        assert resp.status_code == 422

    @patch("tools.app.admin_routes._server_internals")
    async def test_retry_failed_lecture(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        # Create a FAILED pipeline
        ps = PipelineState(group=1, lecture=6, state=FAILED, error="test error")
        save_state(ps)

        with patch("tools.app.admin_routes.create_pipeline"):
            with patch("tools.app.scheduler._run_post_meeting_pipeline"):
                async with await _client() as c:
                    resp = await c.post(
                        "/admin/retry-lecture",
                        json={"group_number": 1, "lecture_number": 6},
                        headers=_AUTH_HEADER,
                    )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["group"] == 1
        assert data["lecture"] == 6
        assert data["previous_state"] == "FAILED"

    @patch("tools.app.admin_routes._server_internals")
    async def test_retry_complete_lecture(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        ps = PipelineState(group=1, lecture=3, state=COMPLETE)
        save_state(ps)

        with patch("tools.app.admin_routes.create_pipeline"):
            with patch("tools.app.scheduler._run_post_meeting_pipeline"):
                async with await _client() as c:
                    resp = await c.post(
                        "/admin/retry-lecture",
                        json={"group_number": 1, "lecture_number": 3},
                        headers=_AUTH_HEADER,
                    )

        assert resp.status_code == 200
        assert resp.json()["previous_state"] == "COMPLETE"

    @patch("tools.app.admin_routes._server_internals")
    async def test_retry_active_lecture_returns_409(
        self, mock_internals, patched_secrets
    ):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        ps = PipelineState(group=1, lecture=6, state=ANALYZING)
        save_state(ps)

        async with await _client() as c:
            resp = await c.post(
                "/admin/retry-lecture",
                json={"group_number": 1, "lecture_number": 6},
                headers=_AUTH_HEADER,
            )

        assert resp.status_code == 409
        assert "active" in resp.json()["detail"].lower()

    @patch("tools.app.admin_routes._server_internals")
    async def test_retry_nonexistent_lecture(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        with patch("tools.app.admin_routes.create_pipeline"):
            with patch("tools.app.scheduler._run_post_meeting_pipeline"):
                async with await _client() as c:
                    resp = await c.post(
                        "/admin/retry-lecture",
                        json={"group_number": 2, "lecture_number": 10},
                        headers=_AUTH_HEADER,
                    )

        assert resp.status_code == 200
        assert resp.json()["previous_state"] == "NONE"


# ===========================================================================
# POST /admin/reset-pipeline
# ===========================================================================


@pytest.mark.asyncio
class TestResetPipeline:
    async def test_rejects_missing_auth(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post(
                "/admin/reset-pipeline",
                json={"group_number": 1, "lecture_number": 1},
            )
        assert resp.status_code == 401

    @patch("tools.app.admin_routes._server_internals")
    async def test_reset_no_pipeline(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        async with await _client() as c:
            resp = await c.post(
                "/admin/reset-pipeline",
                json={"group_number": 1, "lecture_number": 15},
                headers=_AUTH_HEADER,
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "no_pipeline"

    @patch("tools.app.admin_routes._server_internals")
    async def test_reset_active_pipeline(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        ps = PipelineState(
            group=1,
            lecture=6,
            state=ANALYZING,
            started_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        save_state(ps)
        _processing_tasks[_task_key(1, 6)] = datetime.now()

        async with await _client() as c:
            resp = await c.post(
                "/admin/reset-pipeline",
                json={"group_number": 1, "lecture_number": 6},
                headers=_AUTH_HEADER,
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "reset"
        assert data["previous_state"] == ANALYZING

        # State file should be removed
        assert not state_file_path(1, 6).exists()
        # Dedup key should be cleared
        assert _task_key(1, 6) not in _processing_tasks

    @patch("tools.app.admin_routes._server_internals")
    async def test_reset_failed_pipeline(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        ps = PipelineState(group=2, lecture=3, state=FAILED, error="old error")
        save_state(ps)

        async with await _client() as c:
            resp = await c.post(
                "/admin/reset-pipeline",
                json={"group_number": 2, "lecture_number": 3},
                headers=_AUTH_HEADER,
            )

        assert resp.status_code == 200
        assert resp.json()["previous_state"] == FAILED
        assert not state_file_path(2, 3).exists()


# ===========================================================================
# GET /admin/lecture-status
# ===========================================================================


@pytest.mark.asyncio
class TestLectureStatus:
    async def test_rejects_missing_auth(self, patched_secrets):
        async with await _client() as c:
            resp = await c.get("/admin/lecture-status")
        assert resp.status_code == 401

    @patch("tools.app.admin_routes._server_internals")
    @patch("tools.app.admin_routes._get_pinecone_counts", return_value={})
    async def test_returns_all_lectures(self, mock_pc, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        async with await _client() as c:
            resp = await c.get("/admin/lecture-status", headers=_AUTH_HEADER)

        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert "groups" in data
        assert "group_1" in data["groups"]
        assert "group_2" in data["groups"]
        assert len(data["groups"]["group_1"]) == 15
        assert len(data["groups"]["group_2"]) == 15
        assert data["summary"]["total_lectures"] == 30

    @patch("tools.app.admin_routes._server_internals")
    @patch("tools.app.admin_routes._get_pinecone_counts", return_value={})
    async def test_includes_pipeline_state(
        self, mock_pc, mock_internals, patched_secrets
    ):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        # Create some pipeline states
        ps_complete = PipelineState(
            group=1,
            lecture=1,
            state=COMPLETE,
            drive_video_id="abc123",
            summary_doc_id="doc456",
        )
        save_state(ps_complete)

        ps_failed = PipelineState(
            group=1,
            lecture=2,
            state=FAILED,
            error="test error",
        )
        save_state(ps_failed)

        async with await _client() as c:
            resp = await c.get("/admin/lecture-status", headers=_AUTH_HEADER)

        data = resp.json()
        g1 = data["groups"]["group_1"]

        # Lecture 1 — complete
        l1 = g1[0]
        assert l1["pipeline_state"] == COMPLETE
        assert l1["drive_video_id"] == "abc123"

        # Lecture 2 — failed
        l2 = g1[1]
        assert l2["pipeline_state"] == FAILED
        assert l2["last_error"] == "test error"

        # Lecture 3 — unknown (no state file)
        l3 = g1[2]
        assert l3["pipeline_state"] == "UNKNOWN"

        # Summary counts
        assert data["summary"]["complete"] == 1
        assert data["summary"]["failed"] == 1

    @patch("tools.app.admin_routes._server_internals")
    @patch(
        "tools.app.admin_routes._get_pinecone_counts",
        side_effect=Exception("Pinecone down"),
    )
    async def test_handles_pinecone_failure(
        self, mock_pc, mock_internals, patched_secrets
    ):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        async with await _client() as c:
            resp = await c.get("/admin/lecture-status", headers=_AUTH_HEADER)

        # Should still succeed even if Pinecone is down
        assert resp.status_code == 200


# ===========================================================================
# POST /admin/force-refresh-token
# ===========================================================================


@pytest.mark.asyncio
class TestForceRefreshToken:
    async def test_rejects_missing_auth(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post("/admin/force-refresh-token")
        assert resp.status_code == 401

    @patch("tools.app.admin_routes._server_internals")
    async def test_successful_refresh(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        mock_service = MagicMock()
        mock_service.about.return_value.get.return_value.execute.return_value = {
            "user": {"emailAddress": "test@example.com"}
        }

        with patch(
            "tools.integrations.gdrive_manager.get_drive_service",
            return_value=mock_service,
        ):
            async with await _client() as c:
                resp = await c.post(
                    "/admin/force-refresh-token",
                    headers=_AUTH_HEADER,
                )

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "refreshed"
        assert data["google_user"] == "test@example.com"

    @patch("tools.app.admin_routes._server_internals")
    async def test_refresh_failure_returns_500(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        with patch(
            "tools.integrations.gdrive_manager.get_drive_service",
            side_effect=RuntimeError("Token expired"),
        ):
            async with await _client() as c:
                resp = await c.post(
                    "/admin/force-refresh-token",
                    headers=_AUTH_HEADER,
                )

        assert resp.status_code == 500
        assert "Token refresh failed" in resp.json()["detail"]


# ===========================================================================
# GET /admin/system-report
# ===========================================================================


@pytest.mark.asyncio
class TestSystemReport:
    async def test_rejects_missing_auth(self, patched_secrets):
        async with await _client() as c:
            resp = await c.get("/admin/system-report")
        assert resp.status_code == 401

    @patch("tools.app.admin_routes._server_internals")
    async def test_report_structure(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        # Set app startup time
        app.state.started_at = datetime.now(timezone.utc) - timedelta(hours=2)

        async with await _client() as c:
            resp = await c.get("/admin/system-report", headers=_AUTH_HEADER)

        assert resp.status_code == 200
        data = resp.json()
        assert "report" in data
        assert "timestamp" in data
        assert "active_pipelines" in data
        assert "recent_errors" in data

        report = data["report"]
        assert "Training Agent System Report" in report
        assert "Uptime:" in report
        assert "Group 1" in report
        assert "Group 2" in report

    @patch("tools.app.admin_routes._server_internals")
    async def test_report_includes_errors(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        app.state.started_at = datetime.now(timezone.utc)

        # Create a recent FAILED pipeline
        from tools.core.config import TBILISI_TZ

        ps = PipelineState(
            group=1,
            lecture=5,
            state=FAILED,
            error="Gemini API timeout",
            updated_at=datetime.now(tz=TBILISI_TZ).isoformat(),
        )
        save_state(ps)

        async with await _client() as c:
            resp = await c.get("/admin/system-report", headers=_AUTH_HEADER)

        data = resp.json()
        assert data["recent_errors"] == 1
        assert "Gemini API timeout" in data["report"]

    @patch("tools.app.admin_routes._server_internals")
    async def test_report_no_uptime_when_not_started(
        self, mock_internals, patched_secrets
    ):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        # Remove started_at if it exists
        if hasattr(app.state, "started_at"):
            delattr(app.state, "started_at")

        async with await _client() as c:
            resp = await c.get("/admin/system-report", headers=_AUTH_HEADER)

        assert resp.status_code == 200
        assert "Uptime: unknown" in resp.json()["report"]

    @patch("tools.app.admin_routes._server_internals")
    async def test_report_shows_active_pipelines(self, mock_internals, patched_secrets):
        mock_internals.return_value = (
            srv.verify_webhook_secret,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )

        app.state.started_at = datetime.now(timezone.utc)

        ps = PipelineState(
            group=2,
            lecture=4,
            state=ANALYZING,
            updated_at=datetime.now().isoformat(),
        )
        save_state(ps)

        async with await _client() as c:
            resp = await c.get("/admin/system-report", headers=_AUTH_HEADER)

        data = resp.json()
        assert data["active_pipelines"] == 1
        assert "G2 L4: ANALYZING" in data["report"]


# ===========================================================================
# Validation edge cases
# ===========================================================================


@pytest.mark.asyncio
class TestLectureRequestValidation:
    async def test_missing_group_number(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post(
                "/admin/retry-lecture",
                json={"lecture_number": 1},
                headers=_AUTH_HEADER,
            )
        assert resp.status_code == 422

    async def test_missing_lecture_number(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post(
                "/admin/retry-lecture",
                json={"group_number": 1},
                headers=_AUTH_HEADER,
            )
        assert resp.status_code == 422

    async def test_string_group_number(self, patched_secrets):
        async with await _client() as c:
            resp = await c.post(
                "/admin/retry-lecture",
                json={"group_number": "abc", "lecture_number": 1},
                headers=_AUTH_HEADER,
            )
        assert resp.status_code == 422
