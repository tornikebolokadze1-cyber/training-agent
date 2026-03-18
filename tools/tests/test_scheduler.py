"""Unit tests for tools/scheduler.py.

Covers:
- TBILISI_TZ is ZoneInfo("Asia/Tbilisi"), not a pytz object
- Constants: REMINDER_HOUR, recording polling parameters
- _import_zoom_manager: success and ImportError
- check_recording_ready: auth errors, transient errors, timeout, success
- _run_post_meeting_pipeline: full pipeline, download failure, pipeline exception
- pre_meeting_job: normal flow, lecture_number==0, >TOTAL_LECTURES, Zoom errors
- post_meeting_job: dispatches to thread executor
- _get_running_scheduler: returns ref or raises
- _schedule_post_meeting: schedules job, handles past fire time
- start_scheduler: creates scheduler with correct jobs

All external I/O (Zoom API, time.sleep, WhatsApp) is mocked.

Run with:
    pytest tools/tests/test_scheduler.py -v
"""

from __future__ import annotations

import asyncio
import types
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.app.scheduler as sched

# ===========================================================================
# 1. ZoneInfo used instead of pytz
# ===========================================================================


class TestTbilisiTimezone:
    """TBILISI_TZ must be a ZoneInfo instance, not a pytz object."""

    def test_tbilisi_tz_is_zoneinfo(self):
        from zoneinfo import ZoneInfo
        assert isinstance(sched.TBILISI_TZ, ZoneInfo), (
            f"TBILISI_TZ should be ZoneInfo, got {type(sched.TBILISI_TZ)}"
        )

    def test_tbilisi_tz_key_is_asia_tbilisi(self):
        assert sched.TBILISI_TZ.key == "Asia/Tbilisi"

    def test_tbilisi_tz_is_not_pytz(self):
        # Confirm the object has no pytz-specific attributes
        assert not hasattr(sched.TBILISI_TZ, "localize"), (
            "TBILISI_TZ appears to be a pytz object (has .localize); "
            "expected ZoneInfo"
        )


# ===========================================================================
# Helper: build a fake zoom_manager module
# ===========================================================================

def _make_zoom_manager(get_meeting_recordings_side_effect=None,
                       get_meeting_recordings_return=None) -> types.ModuleType:
    """Return a fake zoom_manager module for injection into _import_zoom_manager."""
    zm = types.ModuleType("tools.integrations.zoom_manager")
    mock_fn = MagicMock()
    if get_meeting_recordings_side_effect is not None:
        mock_fn.side_effect = get_meeting_recordings_side_effect
    elif get_meeting_recordings_return is not None:
        mock_fn.return_value = get_meeting_recordings_return
    zm.get_meeting_recordings = mock_fn
    return zm


# ===========================================================================
# 2. check_recording_ready — auth error handling
# ===========================================================================


class TestCheckRecordingReadyAuthError:
    """401/403/unauthorized/forbidden errors abort without retrying."""

    def _run(self, error_message: str):
        """Call check_recording_ready with a zoom_manager that raises once."""
        zm = _make_zoom_manager(
            get_meeting_recordings_side_effect=Exception(error_message)
        )
        alert_mock = MagicMock()

        with patch.object(sched, "_import_zoom_manager", return_value=zm), \
             patch.object(sched, "alert_operator", alert_mock), \
             patch("tools.app.scheduler.time.sleep"):
            result = sched.check_recording_ready("mtg-123")

        return result, zm.get_meeting_recordings, alert_mock

    def test_401_error_returns_empty(self):
        result, _, _ = self._run("HTTP 401 Unauthorized")
        assert result == []

    def test_403_error_returns_empty(self):
        result, _, _ = self._run("HTTP 403 Forbidden")
        assert result == []

    def test_unauthorized_keyword_returns_empty(self):
        result, _, _ = self._run("unauthorized access denied")
        assert result == []

    def test_forbidden_keyword_returns_empty(self):
        result, _, _ = self._run("forbidden by policy")
        assert result == []

    def test_auth_error_calls_api_only_once(self):
        """Auth errors must not trigger a retry loop — exactly one API call."""
        _, api_mock, _ = self._run("HTTP 401 Unauthorized")
        # One call: the initial RECORDING_INITIAL_DELAY sleep happens first,
        # then the single API call that fails with auth error.
        assert api_mock.call_count == 1

    def test_auth_error_calls_alert_operator(self):
        """alert_operator must be called so the operator is notified."""
        _, _, alert_mock = self._run("HTTP 403 Forbidden")
        alert_mock.assert_called_once()

    def test_auth_error_message_contains_meeting_id(self):
        """The alert message should reference the meeting ID."""
        zm = _make_zoom_manager(
            get_meeting_recordings_side_effect=Exception("401 Unauthorized")
        )
        alert_mock = MagicMock()
        with patch.object(sched, "_import_zoom_manager", return_value=zm), \
             patch.object(sched, "alert_operator", alert_mock), \
             patch("tools.app.scheduler.time.sleep"):
            sched.check_recording_ready("meeting-XYZ-789")

        alert_call_args = alert_mock.call_args[0][0]
        assert "meeting-XYZ-789" in alert_call_args


# ===========================================================================
# 3. check_recording_ready — transient error handling
# ===========================================================================


class TestCheckRecordingReadyTransientError:
    """Network / transient errors are retried until the recording appears."""

    def test_transient_error_then_success_returns_recording(self):
        """After one transient failure, a successful response is returned."""
        recording_file = {
            "file_type": "MP4",
            "status": "COMPLETED",
            "download_url": "https://zoom.example.com/recording.mp4",
        }
        success_response = {"recording_files": [recording_file]}

        call_count = [0]

        def side_effect(meeting_id):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("Network unreachable")
            return success_response

        zm = _make_zoom_manager(get_meeting_recordings_side_effect=side_effect)
        alert_mock = MagicMock()

        with patch.object(sched, "_import_zoom_manager", return_value=zm), \
             patch.object(sched, "alert_operator", alert_mock), \
             patch("tools.app.scheduler.time.sleep"):
            result = sched.check_recording_ready("mtg-retry")

        assert len(result) == 1
        assert result[0]["download_url"] == "https://zoom.example.com/recording.mp4"

    def test_transient_error_does_not_call_alert_operator(self):
        """A single transient error followed by success must not alert the operator."""
        recording_file = {"file_type": "MP4", "status": "COMPLETED", "download_url": "u"}
        calls = [0]

        def side_effect(_):
            calls[0] += 1
            if calls[0] == 1:
                raise ConnectionError("Temporary failure")
            return {"recording_files": [recording_file]}

        zm = _make_zoom_manager(get_meeting_recordings_side_effect=side_effect)
        alert_mock = MagicMock()

        with patch.object(sched, "_import_zoom_manager", return_value=zm), \
             patch.object(sched, "alert_operator", alert_mock), \
             patch("tools.app.scheduler.time.sleep"):
            sched.check_recording_ready("mtg-no-alert")

        alert_mock.assert_not_called()

    def test_transient_error_api_called_more_than_once(self):
        """After a transient error the polling loop calls the API again."""
        recording_file = {"file_type": "MP4", "status": "COMPLETED", "download_url": "u"}
        calls = [0]

        def side_effect(_):
            calls[0] += 1
            if calls[0] < 3:
                raise TimeoutError("Connection timed out")
            return {"recording_files": [recording_file]}

        zm = _make_zoom_manager(get_meeting_recordings_side_effect=side_effect)

        with patch.object(sched, "_import_zoom_manager", return_value=zm), \
             patch.object(sched, "alert_operator", MagicMock()), \
             patch("tools.app.scheduler.time.sleep"):
            result = sched.check_recording_ready("mtg-multi-retry")

        assert calls[0] >= 2
        assert result is not None

    def test_no_recording_files_keeps_polling(self):
        """Empty recording_files list is not treated as an error — just retries."""
        responses = [
            {"recording_files": []},
            {"recording_files": []},
            {"recording_files": [
                {"file_type": "MP4", "status": "COMPLETED", "download_url": "u"}
            ]},
        ]
        call_count = [0]

        def side_effect(_):
            resp = responses[min(call_count[0], len(responses) - 1)]
            call_count[0] += 1
            return resp

        zm = _make_zoom_manager(get_meeting_recordings_side_effect=side_effect)

        with patch.object(sched, "_import_zoom_manager", return_value=zm), \
             patch.object(sched, "alert_operator", MagicMock()), \
             patch("tools.app.scheduler.time.sleep"):
            result = sched.check_recording_ready("mtg-empty")

        assert result is not None
        assert call_count[0] >= 3


# ===========================================================================
# 4. pre_meeting_job — WhatsApp failure triggers alert_operator
# ===========================================================================


class TestPreMeetingJobWhatsAppAlert:
    """When send_group_reminder raises, alert_operator must be called."""

    def _run_pre_meeting_job(self, group_number: int, whatsapp_error: Exception):
        """Run pre_meeting_job in an event loop with mocked dependencies."""
        from tools.core.config import TBILISI_TZ

        # A fixed datetime on a Tuesday (group 1 meeting day) so lecture_number > 0
        # weekday=1 (Tuesday), date 2026-03-17
        now_tbilisi = datetime(2026, 3, 17, 19, 0, 0, tzinfo=TBILISI_TZ)

        fake_meeting_info = {"join_url": "https://zoom.us/j/123", "id": "99999"}

        mock_zm = MagicMock()
        mock_zm.create_meeting.return_value = fake_meeting_info

        alert_mock = MagicMock()

        # Fake scheduler returned by _get_running_scheduler
        fake_scheduler = MagicMock()

        async def fake_run_in_executor(executor, fn, *args):
            # Intercept run_in_executor calls:
            # first call is create_meeting (lambda), second is send_group_reminder
            if args:
                # send_group_reminder call: fn(*args) would be the actual call
                fn(*args)
            else:
                # lambda call for create_meeting
                return fn()

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_run_in_executor)

        with patch("tools.app.scheduler.datetime") as mock_dt, \
             patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
             patch.object(sched, "alert_operator", alert_mock), \
             patch.object(sched, "_get_running_scheduler", return_value=fake_scheduler), \
             patch.object(sched, "_schedule_post_meeting"), \
             patch("tools.app.scheduler.asyncio.get_running_loop", return_value=mock_loop):

            # Make datetime.now() return our fixed time
            mock_dt.now.return_value = now_tbilisi
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            # Patch send_group_reminder to raise the given error
            with patch("tools.integrations.whatsapp_sender.send_group_reminder",
                       side_effect=whatsapp_error, create=True):

                # Also patch the import inside pre_meeting_job
                import tools.integrations.whatsapp_sender as ws
                original = getattr(ws, "send_group_reminder", None)
                ws.send_group_reminder = MagicMock(side_effect=whatsapp_error)

                try:
                    asyncio.run(sched.pre_meeting_job(group_number))
                finally:
                    if original is not None:
                        ws.send_group_reminder = original
                    elif hasattr(ws, "send_group_reminder"):
                        del ws.send_group_reminder

        return alert_mock

    def test_whatsapp_failure_calls_alert_operator(self):
        """Any exception from send_group_reminder must trigger alert_operator."""
        import tools.integrations.whatsapp_sender as ws
        from tools.app.scheduler import TBILISI_TZ

        now_tbilisi = datetime(2026, 3, 17, 19, 0, 0, tzinfo=TBILISI_TZ)
        fake_meeting_info = {"join_url": "https://zoom.us/j/123", "id": "99999"}
        mock_zm = MagicMock()
        mock_zm.create_meeting.return_value = fake_meeting_info
        alert_mock = MagicMock()
        fake_scheduler = MagicMock()

        # Patch send_group_reminder at module level before the async job imports it
        ws.send_group_reminder = MagicMock(side_effect=ConnectionError("WhatsApp down"))

        async def fake_executor(executor, fn, *args):
            if args:
                fn(*args)
            else:
                return fn()

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_executor)

        try:
            with patch("tools.app.scheduler.datetime") as mock_dt, \
                 patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
                 patch.object(sched, "alert_operator", alert_mock), \
                 patch.object(sched, "_get_running_scheduler", return_value=fake_scheduler), \
                 patch.object(sched, "_schedule_post_meeting"), \
                 patch("tools.app.scheduler.asyncio.get_running_loop", return_value=mock_loop):

                mock_dt.now.return_value = now_tbilisi
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                asyncio.run(sched.pre_meeting_job(1))
        finally:
            # Restore original attribute state
            if hasattr(ws, "send_group_reminder"):
                del ws.send_group_reminder

        alert_mock.assert_called()

    def test_whatsapp_failure_alert_mentions_group_number(self):
        """The alert message should mention the failing group for operator triage."""
        import tools.integrations.whatsapp_sender as ws
        from tools.app.scheduler import TBILISI_TZ

        now_tbilisi = datetime(2026, 3, 17, 19, 0, 0, tzinfo=TBILISI_TZ)
        fake_meeting_info = {"join_url": "https://zoom.us/j/456", "id": "77777"}
        mock_zm = MagicMock()
        mock_zm.create_meeting.return_value = fake_meeting_info
        alert_mock = MagicMock()
        fake_scheduler = MagicMock()

        ws.send_group_reminder = MagicMock(side_effect=RuntimeError("API timeout"))

        async def fake_executor(executor, fn, *args):
            if args:
                fn(*args)
            else:
                return fn()

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_executor)

        try:
            with patch("tools.app.scheduler.datetime") as mock_dt, \
                 patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
                 patch.object(sched, "alert_operator", alert_mock), \
                 patch.object(sched, "_get_running_scheduler", return_value=fake_scheduler), \
                 patch.object(sched, "_schedule_post_meeting"), \
                 patch("tools.app.scheduler.asyncio.get_running_loop", return_value=mock_loop):

                mock_dt.now.return_value = now_tbilisi
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                asyncio.run(sched.pre_meeting_job(1))
        finally:
            if hasattr(ws, "send_group_reminder"):
                del ws.send_group_reminder

        alert_text = alert_mock.call_args[0][0]
        assert "1" in alert_text  # group number referenced

    def test_whatsapp_success_does_not_call_alert_operator(self):
        """A successful WhatsApp reminder must not trigger any operator alert."""
        import tools.integrations.whatsapp_sender as ws
        from tools.app.scheduler import TBILISI_TZ

        now_tbilisi = datetime(2026, 3, 17, 19, 0, 0, tzinfo=TBILISI_TZ)
        fake_meeting_info = {"join_url": "https://zoom.us/j/789", "id": "55555"}
        mock_zm = MagicMock()
        mock_zm.create_meeting.return_value = fake_meeting_info
        alert_mock = MagicMock()
        fake_scheduler = MagicMock()

        ws.send_group_reminder = MagicMock(return_value=None)

        async def fake_executor(executor, fn, *args):
            if args:
                return fn(*args)
            else:
                return fn()

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_executor)

        try:
            with patch("tools.app.scheduler.datetime") as mock_dt, \
                 patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
                 patch.object(sched, "alert_operator", alert_mock), \
                 patch.object(sched, "_get_running_scheduler", return_value=fake_scheduler), \
                 patch.object(sched, "_schedule_post_meeting"), \
                 patch("tools.app.scheduler.asyncio.get_running_loop", return_value=mock_loop):

                mock_dt.now.return_value = now_tbilisi
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

                asyncio.run(sched.pre_meeting_job(1))
        finally:
            if hasattr(ws, "send_group_reminder"):
                del ws.send_group_reminder

        alert_mock.assert_not_called()


# ===========================================================================
# 5. Constants
# ===========================================================================


class TestSchedulerConstants:
    def test_reminder_hour_derived_from_lecture_start(self):
        assert sched.REMINDER_HOUR == sched.LECTURE_START_HOUR - (sched.REMINDER_OFFSET_MINUTES // 60)

    def test_reminder_hour_is_18(self):
        assert sched.REMINDER_HOUR == 18

    def test_recording_initial_delay_is_15_min(self):
        assert sched.RECORDING_INITIAL_DELAY == 15 * 60

    def test_recording_poll_interval_is_5_min(self):
        assert sched.RECORDING_POLL_INTERVAL == 5 * 60

    def test_recording_poll_timeout_is_3_hours(self):
        assert sched.RECORDING_POLL_TIMEOUT == 3 * 60 * 60


# ===========================================================================
# 6. _import_zoom_manager
# ===========================================================================


class TestImportZoomManager:
    def test_success_returns_module(self):
        """_import_zoom_manager returns the zoom_manager module successfully."""
        import sys
        # If the real module exists, just verify it imports
        if "tools.integrations.zoom_manager" in sys.modules:
            result = sched._import_zoom_manager()
            assert hasattr(result, "get_meeting_recordings") or hasattr(result, "__name__")
        else:
            fake_zm = types.ModuleType("tools.integrations.zoom_manager")
            fake_zm.get_meeting_recordings = MagicMock()
            sys.modules["tools.integrations.zoom_manager"] = fake_zm
            try:
                result = sched._import_zoom_manager()
                assert result is fake_zm
            finally:
                del sys.modules["tools.integrations.zoom_manager"]

    def test_import_error_raises_with_clear_message(self):
        import sys
        # Temporarily replace tools.integrations.zoom_manager with a module that raises on import
        saved = sys.modules.pop("tools.integrations.zoom_manager", None)
        # Also block re-import by making importlib fail
        original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def blocking_import(name, *args, **kwargs):
            if name == "tools.integrations.zoom_manager":
                raise ImportError("No module named 'tools.integrations.zoom_manager'")
            return original_import(name, *args, **kwargs)

        try:
            sys.modules.pop("tools.integrations.zoom_manager", None)
            with patch("builtins.__import__", side_effect=blocking_import):
                with pytest.raises(ImportError, match="tools/zoom_manager.py is not yet created"):
                    sched._import_zoom_manager()
        finally:
            if saved is not None:
                sys.modules["tools.integrations.zoom_manager"] = saved


# ===========================================================================
# 7. check_recording_ready — timeout path
# ===========================================================================


class TestCheckRecordingReadyTimeout:
    def test_timeout_returns_empty_list(self):
        """When polling exceeds RECORDING_POLL_TIMEOUT, returns empty list."""
        zm = _make_zoom_manager(get_meeting_recordings_return={"recording_files": []})
        alert_mock = MagicMock()

        with patch.object(sched, "_import_zoom_manager", return_value=zm), \
             patch.object(sched, "alert_operator", alert_mock), \
             patch("tools.app.scheduler.time.sleep"), \
             patch.object(sched, "RECORDING_INITIAL_DELAY", 0), \
             patch.object(sched, "RECORDING_POLL_INTERVAL", 1), \
             patch.object(sched, "RECORDING_POLL_TIMEOUT", 0):
            result = sched.check_recording_ready("mtg-timeout")

        assert result == []

    def test_timeout_calls_alert_operator(self):
        """Timeout triggers an alert to the operator."""
        zm = _make_zoom_manager(get_meeting_recordings_return={"recording_files": []})
        alert_mock = MagicMock()

        with patch.object(sched, "_import_zoom_manager", return_value=zm), \
             patch.object(sched, "alert_operator", alert_mock), \
             patch("tools.app.scheduler.time.sleep"), \
             patch.object(sched, "RECORDING_INITIAL_DELAY", 0), \
             patch.object(sched, "RECORDING_POLL_INTERVAL", 1), \
             patch.object(sched, "RECORDING_POLL_TIMEOUT", 0):
            sched.check_recording_ready("mtg-timeout-alert")

        alert_mock.assert_called_once()
        assert "mtg-timeout-alert" in alert_mock.call_args[0][0]


# ===========================================================================
# 8. _run_post_meeting_pipeline
# ===========================================================================


class TestRunPostMeetingPipeline:
    def test_no_recording_aborts_early(self):
        """If check_recording_ready returns empty list, pipeline aborts."""
        with patch.object(sched, "check_recording_ready", return_value=[]):
            sched._run_post_meeting_pipeline(1, 3, "mtg-no-rec")

    def test_download_failure_alerts_operator(self, tmp_path):
        """If recording download fails, operator is alerted."""
        recordings = [{"download_url": "https://zoom/rec.mp4", "file_type": "MP4"}]
        mock_zm = MagicMock()
        mock_zm.get_access_token.return_value = "token-123"
        mock_zm.download_recording.side_effect = ConnectionError("Network down")
        alert_mock = MagicMock()

        with patch.object(sched, "check_recording_ready", return_value=recordings), \
             patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
             patch.object(sched, "alert_operator", alert_mock), \
             patch.object(sched, "TMP_DIR", tmp_path):
            sched._run_post_meeting_pipeline(1, 5, "mtg-dl-fail")

        alert_mock.assert_called_once()
        assert "download FAILED" in alert_mock.call_args[0][0]

    def test_successful_pipeline_calls_transcribe(self, tmp_path):
        """Full success path: download -> Drive upload -> transcribe_and_index."""
        recordings = [{"download_url": "https://zoom/rec.mp4", "file_type": "MP4"}]
        mock_zm = MagicMock()
        mock_zm.get_access_token.return_value = "token"
        def fake_download(url, token, path):
            path.write_bytes(b"\x00" * 100)
        mock_zm.download_recording.side_effect = fake_download

        mock_groups = {1: {"name": "g1", "drive_folder_id": "folder-1"}}
        mock_tai = MagicMock(return_value={"summary": 3})
        mock_upload = MagicMock()

        # Patch at source modules since _run_post_meeting_pipeline uses local imports
        with patch.object(sched, "check_recording_ready", return_value=recordings), \
             patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
             patch.object(sched, "GROUPS", mock_groups), \
             patch.object(sched, "TMP_DIR", tmp_path), \
             patch("tools.integrations.gdrive_manager.get_drive_service", return_value=MagicMock()), \
             patch("tools.integrations.gdrive_manager.ensure_folder", return_value="lec-folder"), \
             patch("tools.integrations.gdrive_manager.upload_file", mock_upload), \
             patch("tools.services.transcribe_lecture.transcribe_and_index", mock_tai):
            sched._run_post_meeting_pipeline(1, 3, "mtg-ok")

        mock_tai.assert_called_once()
        mock_upload.assert_called_once()

    def test_pipeline_exception_alerts_operator(self, tmp_path):
        """If transcribe_and_index raises, operator is alerted."""
        recordings = [{"download_url": "https://zoom/rec.mp4", "file_type": "MP4"}]
        mock_zm = MagicMock()
        mock_zm.get_access_token.return_value = "token"
        def fake_download(url, token, path):
            path.write_bytes(b"\x00" * 100)
        mock_zm.download_recording.side_effect = fake_download

        mock_groups = {1: {"name": "g1", "drive_folder_id": "folder-1"}}
        alert_mock = MagicMock()

        with patch.object(sched, "check_recording_ready", return_value=recordings), \
             patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
             patch.object(sched, "GROUPS", mock_groups), \
             patch.object(sched, "TMP_DIR", tmp_path), \
             patch.object(sched, "alert_operator", alert_mock), \
             patch("tools.integrations.gdrive_manager.get_drive_service", return_value=MagicMock()), \
             patch("tools.integrations.gdrive_manager.ensure_folder", return_value="lec-folder"), \
             patch("tools.integrations.gdrive_manager.upload_file", MagicMock()), \
             patch("tools.services.transcribe_lecture.transcribe_and_index", side_effect=RuntimeError("Gemini down")):
            sched._run_post_meeting_pipeline(1, 3, "mtg-fail")

        alert_mock.assert_called_once()
        assert "Pipeline FAILED" in alert_mock.call_args[0][0]


# ===========================================================================
# 9. pre_meeting_job — lecture_number edge cases
# ===========================================================================


class TestPreMeetingJobEdgeCases:
    def test_lecture_number_zero_skips(self):
        """If get_lecture_number returns 0, job exits without creating meeting."""
        mock_zm = MagicMock()

        with patch.object(sched, "get_lecture_number", return_value=0), \
             patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
             patch("tools.app.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 17, 19, 0, 0, tzinfo=sched.TBILISI_TZ)
            asyncio.run(sched.pre_meeting_job(1))

        mock_zm.create_meeting.assert_not_called()

    def test_lecture_number_exceeds_total_skips(self):
        """If lecture_number > TOTAL_LECTURES, job exits early."""
        mock_zm = MagicMock()

        with patch.object(sched, "get_lecture_number", return_value=999), \
             patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
             patch.object(sched, "TOTAL_LECTURES", 15), \
             patch("tools.app.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 17, 19, 0, 0, tzinfo=sched.TBILISI_TZ)
            asyncio.run(sched.pre_meeting_job(2))

        mock_zm.create_meeting.assert_not_called()

    def test_zoom_import_error_continues_with_placeholder(self):
        """If zoom_manager is not available, job continues with placeholder."""
        import tools.integrations.whatsapp_sender as ws
        ws.send_group_reminder = MagicMock(return_value=None)

        async def fake_executor(executor, fn, *args):
            if args:
                return fn(*args)
            return fn()

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_executor)

        try:
            with patch.object(sched, "get_lecture_number", return_value=3), \
                 patch.object(sched, "_import_zoom_manager", side_effect=ImportError("no zoom")), \
                 patch.object(sched, "alert_operator", MagicMock()), \
                 patch("tools.app.scheduler.datetime") as mock_dt, \
                 patch("tools.app.scheduler.asyncio.get_running_loop", return_value=mock_loop):
                mock_dt.now.return_value = datetime(2026, 3, 17, 19, 0, 0, tzinfo=sched.TBILISI_TZ)
                asyncio.run(sched.pre_meeting_job(1))
        finally:
            if hasattr(ws, "send_group_reminder"):
                del ws.send_group_reminder

    def test_zoom_creation_error_alerts_and_continues(self):
        """If create_meeting raises non-ImportError, operator is alerted."""
        import tools.integrations.whatsapp_sender as ws
        ws.send_group_reminder = MagicMock(return_value=None)
        alert_mock = MagicMock()

        mock_zm = MagicMock()
        mock_zm.create_meeting.side_effect = RuntimeError("Zoom API down")

        async def fake_executor(executor, fn, *args):
            if args:
                return fn(*args)
            return fn()

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_executor)

        try:
            with patch.object(sched, "get_lecture_number", return_value=3), \
                 patch.object(sched, "_import_zoom_manager", return_value=mock_zm), \
                 patch.object(sched, "alert_operator", alert_mock), \
                 patch("tools.app.scheduler.datetime") as mock_dt, \
                 patch("tools.app.scheduler.asyncio.get_running_loop", return_value=mock_loop):
                mock_dt.now.return_value = datetime(2026, 3, 17, 19, 0, 0, tzinfo=sched.TBILISI_TZ)
                asyncio.run(sched.pre_meeting_job(1))
        finally:
            if hasattr(ws, "send_group_reminder"):
                del ws.send_group_reminder

        alert_mock.assert_called()
        assert "Zoom meeting creation FAILED" in alert_mock.call_args[0][0]

    def test_no_meeting_id_skips_post_meeting_scheduling(self):
        """If Zoom fails (no ID), post-meeting job is NOT scheduled."""
        import tools.integrations.whatsapp_sender as ws
        ws.send_group_reminder = MagicMock(return_value=None)

        async def fake_executor(executor, fn, *args):
            if args:
                return fn(*args)
            return fn()

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=fake_executor)

        try:
            with patch.object(sched, "get_lecture_number", return_value=3), \
                 patch.object(sched, "_import_zoom_manager", side_effect=ImportError("nope")), \
                 patch.object(sched, "alert_operator", MagicMock()), \
                 patch.object(sched, "_schedule_post_meeting") as mock_spm, \
                 patch("tools.app.scheduler.datetime") as mock_dt, \
                 patch("tools.app.scheduler.asyncio.get_running_loop", return_value=mock_loop):
                mock_dt.now.return_value = datetime(2026, 3, 17, 19, 0, 0, tzinfo=sched.TBILISI_TZ)
                asyncio.run(sched.pre_meeting_job(1))
        finally:
            if hasattr(ws, "send_group_reminder"):
                del ws.send_group_reminder

        mock_spm.assert_not_called()


# ===========================================================================
# 10. post_meeting_job
# ===========================================================================


class TestPostMeetingJob:
    def test_dispatches_to_thread_executor(self):
        """post_meeting_job runs _run_post_meeting_pipeline in executor."""
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=None)

        with patch("tools.app.scheduler.asyncio.get_running_loop", return_value=mock_loop):
            asyncio.run(sched.post_meeting_job(1, 5, "mtg-123"))

        mock_loop.run_in_executor.assert_called_once()
        call_args = mock_loop.run_in_executor.call_args[0]
        assert call_args[0] is None
        assert call_args[1] is sched._run_post_meeting_pipeline
        assert call_args[2:] == (1, 5, "mtg-123")


# ===========================================================================
# 11. _get_running_scheduler
# ===========================================================================


class TestGetRunningScheduler:
    def test_returns_scheduler_when_set(self):
        fake_sched = MagicMock()
        original = sched._scheduler_ref
        try:
            sched._scheduler_ref = fake_sched
            assert sched._get_running_scheduler() is fake_sched
        finally:
            sched._scheduler_ref = original

    def test_raises_when_not_started(self):
        original = sched._scheduler_ref
        try:
            sched._scheduler_ref = None
            with pytest.raises(RuntimeError, match="Scheduler has not been started"):
                sched._get_running_scheduler()
        finally:
            sched._scheduler_ref = original


# ===========================================================================
# 12. _schedule_post_meeting
# ===========================================================================


class TestSchedulePostMeeting:
    def test_adds_job_to_scheduler(self):
        fake_scheduler = MagicMock()

        with patch("tools.app.scheduler.datetime") as mock_dt:
            now = datetime(2026, 3, 17, 19, 0, 0, tzinfo=sched.TBILISI_TZ)
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            sched._schedule_post_meeting(
                scheduler=fake_scheduler,
                group_number=1,
                lecture_number=3,
                meeting_id="mtg-sched",
                fire_at_hour=22,
            )

        fake_scheduler.add_job.assert_called_once()
        kwargs = fake_scheduler.add_job.call_args[1]
        assert kwargs["trigger"] == "date"
        assert kwargs["id"] == "post_g1_l3_mtg-sched"

    def test_reschedules_when_fire_time_in_past(self):
        """If fire_at_hour is already past, reschedules to future."""
        fake_scheduler = MagicMock()

        with patch("tools.app.scheduler.datetime") as mock_dt:
            now = datetime(2026, 3, 17, 23, 0, 0, tzinfo=sched.TBILISI_TZ)
            mock_dt.now.return_value = now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            sched._schedule_post_meeting(
                scheduler=fake_scheduler,
                group_number=2,
                lecture_number=7,
                meeting_id="mtg-past",
                fire_at_hour=22,
            )

        fake_scheduler.add_job.assert_called_once()
        run_date = fake_scheduler.add_job.call_args[1]["run_date"]
        assert run_date > now


# ===========================================================================
# 13. start_scheduler
# ===========================================================================


class TestStartScheduler:
    def test_returns_started_scheduler(self):
        mock_scheduler_instance = MagicMock()
        mock_scheduler_instance.get_jobs.return_value = []

        with patch("tools.app.scheduler.AsyncIOScheduler", return_value=mock_scheduler_instance):
            result = sched.start_scheduler()

        assert result is mock_scheduler_instance
        mock_scheduler_instance.start.assert_called_once()

    def test_registers_four_cron_jobs(self):
        mock_scheduler_instance = MagicMock()
        mock_scheduler_instance.get_jobs.return_value = []

        with patch("tools.app.scheduler.AsyncIOScheduler", return_value=mock_scheduler_instance):
            sched.start_scheduler()

        assert mock_scheduler_instance.add_job.call_count == 4

    def test_sets_module_level_scheduler_ref(self):
        mock_scheduler_instance = MagicMock()
        mock_scheduler_instance.get_jobs.return_value = []
        original = sched._scheduler_ref

        try:
            with patch("tools.app.scheduler.AsyncIOScheduler", return_value=mock_scheduler_instance):
                sched.start_scheduler()
            assert sched._scheduler_ref is mock_scheduler_instance
        finally:
            sched._scheduler_ref = original

    def test_job_ids_match_expected_pattern(self):
        mock_scheduler_instance = MagicMock()
        mock_scheduler_instance.get_jobs.return_value = []

        with patch("tools.app.scheduler.AsyncIOScheduler", return_value=mock_scheduler_instance):
            sched.start_scheduler()

        job_ids = [call[1]["id"] for call in mock_scheduler_instance.add_job.call_args_list]
        expected = {"pre_group1_tuesday", "pre_group1_friday", "pre_group2_monday", "pre_group2_thursday"}
        assert set(job_ids) == expected
