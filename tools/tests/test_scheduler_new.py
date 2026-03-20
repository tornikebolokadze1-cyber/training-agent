"""Tests for NEW features added to tools/app/scheduler.py.

Covers:
- _save_pending_job — writes JSON, replaces duplicates
- _remove_pending_job — removes by group+lecture, no-op when missing
- _restore_pending_jobs — re-schedules valid jobs, skips stale ones, returns count
- Adaptive backoff in check_recording_ready — 404+3301 triggers exponential wait
- Auth-error short-circuit in check_recording_ready — 401/403 aborts immediately

All file I/O uses tmp_path fixture. All scheduler and network calls are mocked.

Run with:
    pytest tools/tests/test_scheduler_new.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# conftest.py stubs apscheduler; we need the real scheduler module
# ---------------------------------------------------------------------------
import tools.app.scheduler as sched
from tools.app.scheduler import (
    _remove_pending_job,
    _restore_pending_jobs,
    _save_pending_job,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _write_jobs_file(path: Path, jobs: list[dict]) -> None:
    path.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


def _read_jobs_file(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


# ===========================================================================
# 1. _save_pending_job
# ===========================================================================


class TestSavePendingJob:
    """_save_pending_job must persist job entries as JSON."""

    def test_creates_file_if_missing(self, tmp_path):
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 3, "mtg-abc", "2026-03-13T22:15:00+04:00")

        assert jobs_file.exists()

    def test_saved_job_has_correct_fields(self, tmp_path):
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(2, 5, "mtg-xyz", "2026-03-17T22:15:00+04:00")

        jobs = _read_jobs_file(jobs_file)
        assert len(jobs) == 1
        job = jobs[0]
        assert job["group"] == 2
        assert job["lecture"] == 5
        assert job["meeting_id"] == "mtg-xyz"
        assert job["fire_time"] == "2026-03-17T22:15:00+04:00"

    def test_multiple_jobs_accumulate(self, tmp_path):
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 1, "mtg-1", "2026-03-13T22:15:00+04:00")
            _save_pending_job(2, 2, "mtg-2", "2026-03-16T22:15:00+04:00")

        jobs = _read_jobs_file(jobs_file)
        assert len(jobs) == 2

    def test_duplicate_group_lecture_replaces_existing(self, tmp_path):
        """Saving the same group+lecture again must replace the old entry, not append."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 3, "old-mtg", "2026-03-13T22:00:00+04:00")
            _save_pending_job(1, 3, "new-mtg", "2026-03-13T22:15:00+04:00")

        jobs = _read_jobs_file(jobs_file)
        assert len(jobs) == 1
        assert jobs[0]["meeting_id"] == "new-mtg"

    def test_does_not_replace_different_lecture(self, tmp_path):
        """Different lectures under the same group must coexist."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 3, "mtg-l3", "2026-03-13T22:15:00+04:00")
            _save_pending_job(1, 4, "mtg-l4", "2026-03-17T22:15:00+04:00")

        jobs = _read_jobs_file(jobs_file)
        assert len(jobs) == 2

    def test_handles_corrupt_file_gracefully(self, tmp_path):
        """If the existing file is corrupt JSON, save should still succeed."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        jobs_file.write_text("not valid json{{{{", encoding="utf-8")

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 1, "mtg-recovery", "2026-03-13T22:15:00+04:00")

        jobs = _read_jobs_file(jobs_file)
        assert any(j["meeting_id"] == "mtg-recovery" for j in jobs)


# ===========================================================================
# 2. _remove_pending_job
# ===========================================================================


class TestRemovePendingJob:
    """_remove_pending_job must remove matching entries and leave others intact."""

    def test_removes_matching_entry(self, tmp_path):
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 3, "meeting_id": "mtg-1", "fire_time": "2026-03-13T22:15:00+04:00"},
            {"group": 2, "lecture": 5, "meeting_id": "mtg-2", "fire_time": "2026-03-17T22:15:00+04:00"},
        ])

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 3)

        remaining = _read_jobs_file(jobs_file)
        assert len(remaining) == 1
        assert remaining[0]["group"] == 2

    def test_noop_when_file_missing(self, tmp_path):
        """Must not raise when the file does not exist."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 1)  # should not raise

    def test_noop_when_entry_not_present(self, tmp_path):
        """Must not raise or corrupt file when the entry is absent."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        _write_jobs_file(jobs_file, [
            {"group": 2, "lecture": 5, "meeting_id": "mtg-2", "fire_time": "2026-03-17T22:15:00+04:00"},
        ])

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 99)  # non-existent

        remaining = _read_jobs_file(jobs_file)
        assert len(remaining) == 1  # original entry intact

    def test_removes_only_exact_match(self, tmp_path):
        """Remove group=1 lecture=3 — do not remove group=1 lecture=13."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 3, "meeting_id": "mtg-l3", "fire_time": "2026-03-13T22:15:00+04:00"},
            {"group": 1, "lecture": 13, "meeting_id": "mtg-l13", "fire_time": "2026-06-06T22:15:00+04:00"},
        ])

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 3)

        remaining = _read_jobs_file(jobs_file)
        assert len(remaining) == 1
        assert remaining[0]["lecture"] == 13

    def test_handles_corrupt_file_gracefully(self, tmp_path):
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        jobs_file.write_text("{invalid}", encoding="utf-8")

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 1)  # must not raise


# ===========================================================================
# 3. _restore_pending_jobs
# ===========================================================================


class TestRestorePendingJobs:
    """_restore_pending_jobs must re-schedule valid jobs and return a count."""

    def _make_scheduler_mock(self) -> MagicMock:
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = []
        return scheduler

    def test_returns_zero_when_file_missing(self, tmp_path):
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        scheduler = self._make_scheduler_mock()

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            count = _restore_pending_jobs(scheduler)

        assert count == 0

    def test_returns_zero_for_empty_file(self, tmp_path):
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        _write_jobs_file(jobs_file, [])
        scheduler = self._make_scheduler_mock()

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            count = _restore_pending_jobs(scheduler)

        assert count == 0

    def test_returns_count_of_restored_jobs(self, tmp_path):
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        # Future fire_times — within the 2-hour stale window
        future = (datetime.now(sched.TBILISI_TZ) + timedelta(minutes=30)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 3, "meeting_id": "mtg-1", "fire_time": future},
            {"group": 2, "lecture": 5, "meeting_id": "mtg-2", "fire_time": future},
        ])
        scheduler = self._make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting") as mock_schedule,
        ):
            count = _restore_pending_jobs(scheduler)

        assert count == 2
        assert mock_schedule.call_count == 2

    def test_stale_jobs_skipped(self, tmp_path):
        """Jobs with fire_time older than 2 hours must be skipped (not restored)."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        stale = (datetime.now(sched.TBILISI_TZ) - timedelta(hours=3)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 3, "meeting_id": "mtg-stale", "fire_time": stale},
        ])
        scheduler = self._make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting") as mock_schedule,
        ):
            count = _restore_pending_jobs(scheduler)

        assert count == 0
        mock_schedule.assert_not_called()

    def test_recent_past_job_still_restored(self, tmp_path):
        """Jobs within 2 hours of now (even in past) must be restored."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        # 1 hour ago — within the 2-hour window
        recent_past = (datetime.now(sched.TBILISI_TZ) - timedelta(hours=1)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 2, "meeting_id": "mtg-recent", "fire_time": recent_past},
        ])
        scheduler = self._make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting") as mock_schedule,
        ):
            count = _restore_pending_jobs(scheduler)

        assert count == 1
        mock_schedule.assert_called_once()

    def test_invalid_fire_time_entry_skipped(self, tmp_path):
        """Entries with unparseable fire_time must be skipped silently."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        future = (datetime.now(sched.TBILISI_TZ) + timedelta(minutes=30)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 3, "meeting_id": "mtg-bad", "fire_time": "not-a-date"},
            {"group": 2, "lecture": 1, "meeting_id": "mtg-good", "fire_time": future},
        ])
        scheduler = self._make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting"),
        ):
            count = _restore_pending_jobs(scheduler)

        assert count == 1  # only the valid entry is restored

    def test_corrupt_json_returns_zero(self, tmp_path):
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        jobs_file.write_text("{[invalid json", encoding="utf-8")
        scheduler = self._make_scheduler_mock()

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            count = _restore_pending_jobs(scheduler)

        assert count == 0

    def test_schedule_post_meeting_receives_correct_args(self, tmp_path):
        """_schedule_post_meeting must be called with matching group/lecture/meeting_id."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        future = (datetime.now(sched.TBILISI_TZ) + timedelta(minutes=45)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 7, "meeting_id": "mtg-abc", "fire_time": future},
        ])
        scheduler = self._make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting") as mock_schedule,
        ):
            _restore_pending_jobs(scheduler)

        call_kwargs = mock_schedule.call_args
        assert call_kwargs is not None
        # Check key arguments were forwarded
        kwargs = call_kwargs[1] if call_kwargs[1] else {}
        args = call_kwargs[0] if call_kwargs[0] else ()
        # _schedule_post_meeting is called with keyword args
        combined = {**dict(zip(
            ["scheduler", "group_number", "lecture_number", "meeting_id",
             "fire_at_hour", "fire_at_minute"],
            args,
        )), **kwargs}
        assert combined.get("group_number") == 1
        assert combined.get("lecture_number") == 7
        assert combined.get("meeting_id") == "mtg-abc"


# ===========================================================================
# 4. check_recording_ready — adaptive backoff for 404s
# ===========================================================================


class TestCheckRecordingReadyAdaptiveBackoff:
    """check_recording_ready must use exponential backoff for "still processing"
    404 errors and abort immediately on auth errors."""

    def _make_zm_mock(self) -> MagicMock:
        zm_mock = MagicMock()
        zm_mock.ZoomAPIError = Exception
        return zm_mock

    def _patch_sleep(self):
        return patch("tools.app.scheduler.time.sleep")

    def _patch_zm(self, zm_mock):
        return patch("tools.app.scheduler._import_zoom_manager", return_value=zm_mock)

    def test_auth_error_aborts_immediately(self):
        """A 401/Forbidden error must return [] without waiting for timeout."""
        zm_mock = self._make_zm_mock()
        zm_mock.get_meeting_recordings.side_effect = Exception("401 Unauthorized")

        sleep_calls: list[float] = []

        with (
            self._patch_zm(zm_mock),
            patch("tools.app.scheduler.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
            patch("tools.app.scheduler.alert_operator"),
        ):
            result = sched.check_recording_ready("mtg-auth-test")

        assert result == []
        # After the initial RECORDING_INITIAL_DELAY sleep, there should be no
        # further poll-cycle sleeps (the function aborted on first attempt)
        poll_sleeps = [s for s in sleep_calls if s != sched.RECORDING_INITIAL_DELAY]
        assert len(poll_sleeps) == 0

    def test_forbidden_error_aborts_immediately(self):
        """A 403 Forbidden error must also short-circuit."""
        zm_mock = self._make_zm_mock()
        zm_mock.get_meeting_recordings.side_effect = Exception("403 forbidden access denied")

        with (
            self._patch_zm(zm_mock),
            patch("tools.app.scheduler.time.sleep"),
            patch("tools.app.scheduler.alert_operator"),
        ):
            result = sched.check_recording_ready("mtg-forbidden")

        assert result == []

    def test_still_processing_404_triggers_backoff(self):
        """A 404+3301 error (recording still processing) must use adaptive backoff."""
        zm_mock = self._make_zm_mock()

        # First call: 404/3301 "still processing"
        # Second call: success with MP4
        zm_mock.get_meeting_recordings.side_effect = [
            Exception("404 3301 recording still being processed"),
            {"recording_files": [
                {"file_type": "MP4", "status": "COMPLETED",
                 "download_url": "https://zoom.us/rec/test.mp4"},
            ]},
            # Third call (extra poll)
            {"recording_files": [
                {"file_type": "MP4", "status": "COMPLETED",
                 "download_url": "https://zoom.us/rec/test.mp4"},
            ]},
        ]

        sleep_calls: list[float] = []
        with (
            self._patch_zm(zm_mock),
            patch("tools.app.scheduler.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            result = sched.check_recording_ready("mtg-backoff")

        assert len(result) >= 1
        # After the initial delay, the backoff sleep for the 404 must be >=
        # RECORDING_POLL_INTERVAL (exponential backoff starts at 1x, then 2x…)
        non_initial = [s for s in sleep_calls if s != sched.RECORDING_INITIAL_DELAY]
        assert any(s >= sched.RECORDING_POLL_INTERVAL for s in non_initial)

    def test_successful_mp4_returned(self):
        """On success, a list of completed MP4 dicts must be returned."""
        zm_mock = self._make_zm_mock()
        mp4_file = {
            "file_type": "MP4",
            "status": "COMPLETED",
            "download_url": "https://zoom.us/rec/lecture.mp4",
        }
        zm_mock.get_meeting_recordings.return_value = {
            "recording_files": [mp4_file]
        }

        with (
            self._patch_zm(zm_mock),
            patch("tools.app.scheduler.time.sleep"),
        ):
            result = sched.check_recording_ready("mtg-success")

        assert len(result) >= 1
        assert result[0]["file_type"] == "MP4"
        assert result[0]["status"] == "COMPLETED"

    def test_transient_network_error_retries(self):
        """Non-auth transient errors must trigger a standard retry, not abort."""
        zm_mock = self._make_zm_mock()
        mp4_file = {
            "file_type": "MP4",
            "status": "COMPLETED",
            "download_url": "https://zoom.us/rec/test.mp4",
        }
        # First call fails with a transient network error, second succeeds
        zm_mock.get_meeting_recordings.side_effect = [
            Exception("Connection reset by peer"),
            {"recording_files": [mp4_file]},
            {"recording_files": [mp4_file]},  # extra poll
        ]

        with (
            self._patch_zm(zm_mock),
            patch("tools.app.scheduler.time.sleep"),
        ):
            result = sched.check_recording_ready("mtg-transient")

        assert len(result) >= 1

    def test_exponential_backoff_increases_with_consecutive_404s(self):
        """Each consecutive 404 must produce a longer backoff than the last.

        The implementation doubles the interval per consecutive 404:
            attempt 1 → POLL_INTERVAL * 2^0 = 5 min
            attempt 2 → POLL_INTERVAL * 2^1 = 10 min
            attempt 3 → POLL_INTERVAL * 2^2 = 20 min
        After success the extra-poll sleep uses POLL_INTERVAL (5 min) — we
        must collect only the three backoff sleeps, not the extra-poll one.
        """
        zm_mock = self._make_zm_mock()

        # Three consecutive 404/3301 errors, then success
        mp4_file = {"file_type": "MP4", "status": "COMPLETED", "download_url": "https://zoom.us/r"}
        zm_mock.get_meeting_recordings.side_effect = [
            Exception("404 3301 still processing"),
            Exception("404 3301 still processing"),
            Exception("404 3301 still processing"),
            {"recording_files": [mp4_file]},
            {"recording_files": [mp4_file]},
        ]

        sleep_calls: list[float] = []
        with (
            self._patch_zm(zm_mock),
            patch("tools.app.scheduler.time.sleep", side_effect=lambda s: sleep_calls.append(s)),
        ):
            with patch.object(sched, "RECORDING_POLL_TIMEOUT", 10 * 60 * 60):
                sched.check_recording_ready("mtg-expo")

        # Remove the initial delay; the remaining sleeps come from:
        #   backoff_1, backoff_2, backoff_3  (the three 404 sleeps), then extra_poll sleep
        # The backoff values are POLL_INTERVAL * 2^(k-1): 5, 10, 20 min (in seconds)
        non_initial = [s for s in sleep_calls if s != sched.RECORDING_INITIAL_DELAY]
        # We expect at least 3 backoff values before the optional extra-poll sleep
        assert len(non_initial) >= 3, f"Expected >=3 backoff sleeps, got {non_initial}"
        # The first three are strictly increasing (5 < 10 < 20 min)
        backoff_trio = non_initial[:3]
        assert backoff_trio[0] < backoff_trio[1], (
            f"Backoff not increasing: {backoff_trio[0]} >= {backoff_trio[1]}"
        )
        assert backoff_trio[1] < backoff_trio[2], (
            f"Backoff not increasing: {backoff_trio[1]} >= {backoff_trio[2]}"
        )
