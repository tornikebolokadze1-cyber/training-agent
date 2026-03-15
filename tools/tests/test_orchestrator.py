"""Unit tests for tools/orchestrator.py.

Covers:
- validate_credentials: missing required, missing optional, all present
- _cleanup_stale_tmp_files: removes old files, keeps fresh ones
- _configure_logging: handler setup
- _CREDENTIALS structure validation

Run with:
    pytest tools/tests/test_orchestrator.py -v
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Pop fastapi/slowapi/pydantic stubs so orchestrator can import real ones.
# orchestrator.py imports from tools.server which needs real FastAPI.
# ---------------------------------------------------------------------------
for _mod_name in list(sys.modules):
    if _mod_name.startswith(("fastapi", "slowapi", "httpx", "pydantic", "uvicorn",
                             "tools.server", "tools.orchestrator")):
        sys.modules.pop(_mod_name, None)

import tools.orchestrator as orch


# ===========================================================================
# 1. validate_credentials — required vs optional
# ===========================================================================


class TestValidateCredentials:
    def test_all_present_succeeds(self):
        """No exception when all required credentials are set."""
        creds = [
            ("ZOOM_ACCOUNT_ID", "val", True),
            ("GEMINI_API_KEY", "val", True),
            ("OPT_VAR", "val", False),
        ]
        with patch.object(orch, "_CREDENTIALS", creds):
            # Should not raise
            orch.validate_credentials()

    def test_missing_required_raises(self):
        """EnvironmentError raised when a required credential is empty."""
        creds = [
            ("ZOOM_ACCOUNT_ID", "", True),  # missing!
            ("GEMINI_API_KEY", "val", True),
        ]
        with patch.object(orch, "_CREDENTIALS", creds):
            with pytest.raises(EnvironmentError, match="ZOOM_ACCOUNT_ID"):
                orch.validate_credentials()

    def test_missing_optional_does_not_raise(self):
        """Missing optional credentials only warn, don't raise."""
        creds = [
            ("ZOOM_ACCOUNT_ID", "val", True),
            ("N8N_CALLBACK_URL", "", False),  # optional, missing
        ]
        with patch.object(orch, "_CREDENTIALS", creds):
            # Should not raise
            orch.validate_credentials()

    def test_multiple_missing_required_listed(self):
        """All missing required vars are listed in the error message."""
        creds = [
            ("VAR_A", "", True),
            ("VAR_B", "", True),
            ("VAR_C", "ok", True),
        ]
        with patch.object(orch, "_CREDENTIALS", creds):
            with pytest.raises(EnvironmentError) as exc_info:
                orch.validate_credentials()
            assert "VAR_A" in str(exc_info.value)
            assert "VAR_B" in str(exc_info.value)


# ===========================================================================
# 2. _cleanup_stale_tmp_files
# ===========================================================================


class TestCleanupStaleTmpFiles:
    def test_removes_old_mp4_files(self, tmp_path):
        old_file = tmp_path / "old_recording.mp4"
        old_file.write_bytes(b"\x00" * 10)
        # Set mtime to 12 hours ago
        old_mtime = time.time() - 12 * 3600
        import os
        os.utime(old_file, (old_mtime, old_mtime))

        with patch("tools.config.TMP_DIR", tmp_path):
            orch._cleanup_stale_tmp_files()

        assert not old_file.exists()

    def test_keeps_fresh_mp4_files(self, tmp_path):
        fresh_file = tmp_path / "fresh_recording.mp4"
        fresh_file.write_bytes(b"\x00" * 10)
        # mtime is now — fresh

        with patch("tools.config.TMP_DIR", tmp_path):
            orch._cleanup_stale_tmp_files()

        assert fresh_file.exists()

    def test_handles_missing_directory_gracefully(self, tmp_path):
        nonexistent = tmp_path / "nonexistent_dir"
        with patch("tools.config.TMP_DIR", nonexistent):
            # Should not raise even if dir doesn't exist
            # (glob on nonexistent returns empty)
            orch._cleanup_stale_tmp_files()


# ===========================================================================
# 3. _CREDENTIALS structure
# ===========================================================================


class TestCredentialsStructure:
    def test_each_entry_is_tuple_of_three(self):
        for entry in orch._CREDENTIALS:
            assert len(entry) == 3, f"Entry {entry[0]} should be (name, value, required)"
            name, value, required = entry
            assert isinstance(name, str)
            assert isinstance(required, bool)

    def test_required_credentials_include_zoom(self):
        required_names = [name for name, _, req in orch._CREDENTIALS if req]
        assert "ZOOM_ACCOUNT_ID" in required_names
        assert "ZOOM_CLIENT_ID" in required_names

    def test_optional_credentials_exist(self):
        optional_names = [name for name, _, req in orch._CREDENTIALS if not req]
        assert len(optional_names) >= 1


# ===========================================================================
# 4. status_endpoint
# ===========================================================================


class TestStatusEndpoint:
    """Tests for the /status FastAPI route handler."""

    def _run(self, coro):
        """Helper to run async functions."""
        return asyncio.get_event_loop().run_until_complete(coro)

    def _get_content(self, response):
        """Extract JSON content from a real JSONResponse object."""
        import json
        return json.loads(response.body)

    def test_uptime_none_when_started_at_not_set(self):
        """When app.state.started_at is None, uptime should be None."""
        mock_state = MagicMock()
        mock_state.started_at = None
        mock_state.last_execution_results = []

        with patch.object(orch, "verify_webhook_secret"), \
             patch.object(orch, "app") as mock_app, \
             patch("tools.scheduler._scheduler_ref", None):
            mock_app.state = mock_state
            mock_app.title = "test"
            mock_app.version = "0.1"
            result = self._run(orch.status_endpoint(authorization="Bearer test"))

        content = self._get_content(result)
        assert content["uptime_seconds"] is None
        assert content["started_at"] is None

    def test_uptime_computed_when_started_at_set(self):
        """When app.state.started_at is set, uptime should be computed."""
        from datetime import datetime, timezone, timedelta

        started = datetime.now(timezone.utc) - timedelta(seconds=120)
        mock_state = MagicMock()
        mock_state.started_at = started
        mock_state.last_execution_results = []

        with patch.object(orch, "verify_webhook_secret"), \
             patch.object(orch, "app") as mock_app, \
             patch("tools.scheduler._scheduler_ref", None):
            mock_app.state = mock_state
            mock_app.title = "test"
            mock_app.version = "0.1"
            result = self._run(orch.status_endpoint(authorization="Bearer test"))

        content = self._get_content(result)
        assert content["uptime_seconds"] is not None
        assert content["uptime_seconds"] >= 119  # at least ~120s
        assert content["started_at"] == started.isoformat()

    def test_scheduler_unavailable_when_none(self):
        """When scheduler ref is None, state should be 'unavailable'."""
        mock_state = MagicMock()
        mock_state.started_at = None
        mock_state.last_execution_results = []

        with patch.object(orch, "verify_webhook_secret"), \
             patch.object(orch, "app") as mock_app, \
             patch("tools.scheduler._scheduler_ref", None):
            mock_app.state = mock_state
            mock_app.title = "test"
            mock_app.version = "0.1"
            result = self._run(orch.status_endpoint(authorization=None))

        content = self._get_content(result)
        assert content["scheduler_state"] == "unavailable"
        assert content["scheduled_jobs"] == []

    def test_scheduler_running_with_jobs(self):
        """When scheduler is running with jobs, they should be listed and sorted."""
        from datetime import datetime, timezone

        mock_state = MagicMock()
        mock_state.started_at = None
        mock_state.last_execution_results = ["result1"]

        mock_job1 = MagicMock()
        mock_job1.id = "job1"
        mock_job1.name = "reminder"
        mock_job1.next_run_time = datetime(2026, 3, 17, 18, 0, tzinfo=timezone.utc)
        mock_job1.trigger = "cron[hour=18]"

        mock_job2 = MagicMock()
        mock_job2.id = "job2"
        mock_job2.name = "cleanup"
        mock_job2.next_run_time = datetime(2026, 3, 16, 12, 0, tzinfo=timezone.utc)
        mock_job2.trigger = "cron[hour=12]"

        mock_scheduler = MagicMock()
        mock_scheduler.running = True
        mock_scheduler.get_jobs.return_value = [mock_job1, mock_job2]

        with patch.object(orch, "verify_webhook_secret"), \
             patch.object(orch, "app") as mock_app, \
             patch("tools.scheduler._scheduler_ref", mock_scheduler):
            mock_app.state = mock_state
            mock_app.title = "test"
            mock_app.version = "0.1"
            result = self._run(orch.status_endpoint(authorization="Bearer x"))

        content = self._get_content(result)
        assert content["scheduler_state"] == "running"
        assert len(content["scheduled_jobs"]) == 2
        # job2 (12:00) should come before job1 (18:00) after sorting
        assert content["scheduled_jobs"][0]["id"] == "job2"
        assert content["scheduled_jobs"][1]["id"] == "job1"
        assert content["last_execution_results"] == ["result1"]

    def test_scheduler_stopped(self):
        """When scheduler exists but not running, state should be 'stopped'."""
        mock_state = MagicMock()
        mock_state.started_at = None
        mock_state.last_execution_results = []

        mock_scheduler = MagicMock()
        mock_scheduler.running = False
        mock_scheduler.get_jobs.return_value = []

        with patch.object(orch, "verify_webhook_secret"), \
             patch.object(orch, "app") as mock_app, \
             patch("tools.scheduler._scheduler_ref", mock_scheduler):
            mock_app.state = mock_state
            mock_app.title = "test"
            mock_app.version = "0.1"
            result = self._run(orch.status_endpoint(authorization="Bearer x"))

        content = self._get_content(result)
        assert content["scheduler_state"] == "stopped"

    def test_verify_webhook_secret_is_called(self):
        """verify_webhook_secret should be called with the authorization header."""
        mock_state = MagicMock()
        mock_state.started_at = None
        mock_state.last_execution_results = []

        with patch.object(orch, "verify_webhook_secret") as mock_verify, \
             patch.object(orch, "app") as mock_app, \
             patch("tools.scheduler._scheduler_ref", None):
            mock_app.state = mock_state
            mock_app.title = "test"
            mock_app.version = "0.1"
            self._run(orch.status_endpoint(authorization="Bearer secret123"))

        mock_verify.assert_called_once_with("Bearer secret123")

    def test_job_with_none_next_run_time_sorted_last(self):
        """Jobs with None next_run_time should sort after those with times."""
        from datetime import datetime, timezone

        mock_state = MagicMock()
        mock_state.started_at = None
        mock_state.last_execution_results = []

        mock_job_fired = MagicMock()
        mock_job_fired.id = "fired"
        mock_job_fired.name = "already_fired"
        mock_job_fired.next_run_time = None
        mock_job_fired.trigger = "date"

        mock_job_future = MagicMock()
        mock_job_future.id = "future"
        mock_job_future.name = "upcoming"
        mock_job_future.next_run_time = datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc)
        mock_job_future.trigger = "cron"

        mock_scheduler = MagicMock()
        mock_scheduler.running = True
        mock_scheduler.get_jobs.return_value = [mock_job_fired, mock_job_future]

        with patch.object(orch, "verify_webhook_secret"), \
             patch.object(orch, "app") as mock_app, \
             patch("tools.scheduler._scheduler_ref", mock_scheduler):
            mock_app.state = mock_state
            mock_app.title = "test"
            mock_app.version = "0.1"
            result = self._run(orch.status_endpoint(authorization="Bearer x"))

        content = self._get_content(result)
        # future job should be first, fired job (None) should be last
        assert content["scheduled_jobs"][0]["id"] == "future"
        assert content["scheduled_jobs"][1]["id"] == "fired"


# ===========================================================================
# 5. _on_startup
# ===========================================================================


class TestOnStartup:
    """Tests for the FastAPI startup lifecycle hook."""

    def _run(self, coro):
        import asyncio
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_sets_started_at(self):
        """_on_startup should set app.state.started_at to a UTC datetime."""
        from datetime import datetime, timezone

        mock_state = MagicMock()
        with patch.object(orch, "app") as mock_app, \
             patch.object(orch, "_cleanup_stale_tmp_files"):
            mock_app.state = mock_state
            self._run(orch._on_startup())

        set_value = mock_state.started_at
        assert isinstance(set_value, datetime)
        assert set_value.tzinfo is not None  # UTC-aware

    def test_sets_last_execution_results_empty(self):
        """_on_startup should set app.state.last_execution_results to []."""
        mock_state = MagicMock()
        with patch.object(orch, "app") as mock_app, \
             patch.object(orch, "_cleanup_stale_tmp_files"):
            mock_app.state = mock_state
            self._run(orch._on_startup())

        assert mock_state.last_execution_results == []

    def test_calls_cleanup_stale_tmp_files(self):
        """_on_startup should call _cleanup_stale_tmp_files."""
        mock_state = MagicMock()
        with patch.object(orch, "app") as mock_app, \
             patch.object(orch, "_cleanup_stale_tmp_files") as mock_cleanup:
            mock_app.state = mock_state
            self._run(orch._on_startup())

        mock_cleanup.assert_called_once()


# ===========================================================================
# 6. _configure_logging
# ===========================================================================


class TestConfigureLogging:
    """Tests for _configure_logging."""

    def _reset_root_logger(self):
        """Remove all handlers from root logger before each test."""
        root = logging.getLogger()
        root.handlers.clear()

    def test_adds_console_handler(self):
        self._reset_root_logger()
        with patch.dict("os.environ", {}, clear=False), \
             patch.dict("os.environ", {"RAILWAY_ENVIRONMENT": "production"}):
            orch._configure_logging()

        root = logging.getLogger()
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)
                          and not isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(stream_handlers) >= 1
        self._reset_root_logger()

    def test_skips_file_handler_on_railway(self):
        self._reset_root_logger()
        with patch.dict("os.environ", {"RAILWAY_ENVIRONMENT": "production"}):
            orch._configure_logging()

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers
                        if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(file_handlers) == 0
        self._reset_root_logger()

    def test_adds_file_handler_when_not_railway(self, tmp_path):
        self._reset_root_logger()
        import os
        env = os.environ.copy()
        env.pop("RAILWAY_ENVIRONMENT", None)
        with patch.dict("os.environ", env, clear=True), \
             patch("tools.orchestrator.PROJECT_ROOT", tmp_path):
            orch._configure_logging()

        root = logging.getLogger()
        file_handlers = [h for h in root.handlers
                        if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(file_handlers) >= 1
        # Clean up file handler to release file
        for h in file_handlers:
            h.close()
        self._reset_root_logger()

    def test_sets_third_party_loggers_to_warning(self):
        self._reset_root_logger()
        with patch.dict("os.environ", {"RAILWAY_ENVIRONMENT": "production"}):
            orch._configure_logging()

        assert logging.getLogger("apscheduler.scheduler").level == logging.WARNING
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert logging.getLogger("uvicorn.access").level == logging.WARNING
        self._reset_root_logger()


# ===========================================================================
# 7. start()
# ===========================================================================


class TestStart:
    """Tests for the start() entry point."""

    def test_calls_configure_logging_then_validate(self):
        """start() should call _configure_logging, then validate_credentials."""
        call_order = []
        with patch.object(orch, "_configure_logging", side_effect=lambda: call_order.append("logging")), \
             patch.object(orch, "validate_credentials", side_effect=lambda: call_order.append("validate")), \
             patch("asyncio.run"):
            orch.start()

        assert call_order == ["logging", "validate"]

    def test_exits_on_credential_failure(self):
        """start() should call sys.exit(1) if validate_credentials raises."""
        with patch.object(orch, "_configure_logging"), \
             patch.object(orch, "validate_credentials", side_effect=EnvironmentError("missing")), \
             pytest.raises(SystemExit) as exc_info:
            orch.start()

        assert exc_info.value.code == 1

    def test_calls_asyncio_run_on_success(self):
        """start() should call asyncio.run(_async_start) when credentials pass."""
        with patch.object(orch, "_configure_logging"), \
             patch.object(orch, "validate_credentials"), \
             patch("asyncio.run") as mock_run:
            orch.start()

        mock_run.assert_called_once()
        # The argument should be the coroutine from _async_start()
        assert mock_run.call_args[0][0] is not None

    def test_handles_keyboard_interrupt_gracefully(self):
        """start() should catch KeyboardInterrupt without crashing."""
        with patch.object(orch, "_configure_logging"), \
             patch.object(orch, "validate_credentials"), \
             patch("asyncio.run", side_effect=KeyboardInterrupt):
            # Should not raise
            orch.start()


# ===========================================================================
# 8. _async_start
# ===========================================================================


class TestAsyncStart:
    """Tests for _async_start coroutine."""

    def _run(self, coro):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_calls_start_scheduler(self):
        """_async_start should call start_scheduler."""
        mock_scheduler = MagicMock()

        async def _noop_serve():
            pass

        mock_server_instance = MagicMock()
        mock_server_instance.serve = _noop_serve

        with patch("tools.scheduler.start_scheduler", return_value=mock_scheduler) as mock_start, \
             patch("uvicorn.Config"), \
             patch("uvicorn.Server", return_value=mock_server_instance):
            self._run(orch._async_start())

        mock_start.assert_called_once()
        mock_scheduler.shutdown.assert_called_once_with(wait=False)

    def test_shuts_down_scheduler_in_finally(self):
        """Scheduler should be shut down even if server.serve() raises."""
        mock_scheduler = MagicMock()

        async def _raise_serve():
            raise RuntimeError("server error")

        mock_server_instance = MagicMock()
        mock_server_instance.serve = _raise_serve

        with patch("tools.scheduler.start_scheduler", return_value=mock_scheduler), \
             patch("uvicorn.Config"), \
             patch("uvicorn.Server", return_value=mock_server_instance), \
             pytest.raises(RuntimeError, match="server error"):
            self._run(orch._async_start())

        mock_scheduler.shutdown.assert_called_once_with(wait=False)
