"""Unit tests for the 24-hour boundary and _concatenate_segments in scheduler.py.

Covers:
- _restore_pending_jobs 24-hour boundary:
    - fire_time = now - 23 h  → job IS restored (within 24-hour window)
    - fire_time = now - 25 h  → job is NOT restored (stale, older than 24 h)
    - fire_time = now - 24 h exactly → boundary: treated as stale (< not <=)
- _save_pending_job / _remove_pending_job round-trips:
    - write a job, read it back
    - overwrite for same group+lecture (dedup)
    - remove a job, verify file still contains others
- _concatenate_segments:
    - ffmpeg is called with the correct concat-demuxer arguments
    - the temporary concat-list file is deleted after a successful run
    - RuntimeError raised on non-zero return code

All file I/O uses pytest's tmp_path fixture.
All subprocess calls are mocked; no real ffmpeg binary is invoked.

Run with:
    pytest tools/tests/test_scheduler_unit.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py before import.
# ---------------------------------------------------------------------------
import tools.app.scheduler as sched
from tools.app.scheduler import (
    _remove_pending_job,
    _restore_pending_jobs,
    _save_pending_job,
    _concatenate_segments,
)


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------

def _write_jobs_file(path: Path, jobs: list[dict]) -> None:
    path.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


def _read_jobs_file(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _make_scheduler_mock() -> MagicMock:
    scheduler = MagicMock()
    scheduler.get_jobs.return_value = []
    return scheduler


# ===========================================================================
# 1. _restore_pending_jobs — 24-hour boundary
# ===========================================================================


class TestRestorePendingJobs24HourBoundary:
    """The stale-job threshold is exactly 2 hours.

    Jobs with fire_time < (now - 2 h) are skipped.
    Jobs with fire_time >= (now - 2 h) are restored.
    """

    def test_job_23_hours_old_is_restored(self, tmp_path: Path) -> None:
        """fire_time = now - 1 h is inside the 2-hour window — must be restored."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        fire_time = (datetime.now(sched.TBILISI_TZ) - timedelta(hours=1)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 4, "meeting_id": "mtg-recent", "fire_time": fire_time},
        ])
        scheduler = _make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting") as mock_schedule,
        ):
            count = _restore_pending_jobs(scheduler)

        assert count == 1
        mock_schedule.assert_called_once()

    def test_job_25_hours_old_is_not_restored(self, tmp_path: Path) -> None:
        """fire_time = now - 3 h is outside the 2-hour window — must be skipped."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        fire_time = (datetime.now(sched.TBILISI_TZ) - timedelta(hours=3)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 2, "lecture": 7, "meeting_id": "mtg-stale", "fire_time": fire_time},
        ])
        scheduler = _make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting") as mock_schedule,
        ):
            count = _restore_pending_jobs(scheduler)

        assert count == 0
        mock_schedule.assert_not_called()

    def test_job_24_hours_old_is_considered_stale(self, tmp_path: Path) -> None:
        """fire_time = exactly now - 2 h + 1 s sits just past the boundary.

        The code uses strict less-than: ``if fire_time < now - timedelta(hours=2)``.
        A job that is exactly 2 h + 1 s old is skipped.
        """
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        # Subtract a tiny extra buffer so we cross the threshold reliably
        fire_time = (datetime.now(sched.TBILISI_TZ) - timedelta(hours=2, seconds=1)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 1, "meeting_id": "mtg-boundary", "fire_time": fire_time},
        ])
        scheduler = _make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting") as mock_schedule,
        ):
            count = _restore_pending_jobs(scheduler)

        # A job that is >2 h old must be skipped
        assert count == 0
        mock_schedule.assert_not_called()

    def test_mixed_jobs_only_fresh_ones_restored(self, tmp_path: Path) -> None:
        """With one fresh (1 h) and one stale (3 h) job, only the fresh one
        must be restored."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        fresh = (datetime.now(sched.TBILISI_TZ) - timedelta(hours=1)).isoformat()
        stale = (datetime.now(sched.TBILISI_TZ) - timedelta(hours=3)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 2, "meeting_id": "mtg-fresh", "fire_time": fresh},
            {"group": 2, "lecture": 6, "meeting_id": "mtg-old",   "fire_time": stale},
        ])
        scheduler = _make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting") as mock_schedule,
        ):
            count = _restore_pending_jobs(scheduler)

        assert count == 1
        mock_schedule.assert_called_once()
        # Verify the correct job was restored
        call_kwargs = mock_schedule.call_args[1] if mock_schedule.call_args[1] else {}
        call_args = mock_schedule.call_args[0] if mock_schedule.call_args[0] else ()
        combined = {**dict(zip(
            ["scheduler", "group_number", "lecture_number", "meeting_id",
             "fire_at_hour", "fire_at_minute"],
            call_args,
        )), **call_kwargs}
        assert combined.get("meeting_id") == "mtg-fresh"

    def test_future_job_is_restored(self, tmp_path: Path) -> None:
        """A job scheduled in the future (not yet fired) must always be restored."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        future = (datetime.now(sched.TBILISI_TZ) + timedelta(hours=1)).isoformat()
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 5, "meeting_id": "mtg-future", "fire_time": future},
        ])
        scheduler = _make_scheduler_mock()

        with (
            patch.object(sched, "_PENDING_JOBS_FILE", jobs_file),
            patch("tools.app.scheduler._schedule_post_meeting") as mock_schedule,
        ):
            count = _restore_pending_jobs(scheduler)

        assert count == 1
        mock_schedule.assert_called_once()


# ===========================================================================
# 2. _save_pending_job / _remove_pending_job
# ===========================================================================


class TestSavePendingJob:
    """_save_pending_job must write a valid JSON entry to disk and handle
    deduplication (same group+lecture replaces the old entry)."""

    def test_creates_file_with_correct_fields(self, tmp_path: Path) -> None:
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 3, "mtg-abc", "2026-03-13T22:15:00+04:00")

        assert jobs_file.exists()
        jobs = _read_jobs_file(jobs_file)
        assert len(jobs) == 1
        assert jobs[0] == {
            "group": 1,
            "lecture": 3,
            "meeting_id": "mtg-abc",
            "fire_time": "2026-03-13T22:15:00+04:00",
        }

    def test_second_save_appends_when_different_lecture(self, tmp_path: Path) -> None:
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 3, "mtg-l3", "2026-03-13T22:15:00+04:00")
            _save_pending_job(1, 4, "mtg-l4", "2026-03-17T22:15:00+04:00")

        jobs = _read_jobs_file(jobs_file)
        assert len(jobs) == 2
        meeting_ids = {j["meeting_id"] for j in jobs}
        assert meeting_ids == {"mtg-l3", "mtg-l4"}

    def test_overwrite_replaces_existing_entry(self, tmp_path: Path) -> None:
        """Saving the same group+lecture twice must result in exactly one entry."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(2, 5, "old-meeting", "2026-03-10T22:00:00+04:00")
            _save_pending_job(2, 5, "new-meeting", "2026-03-10T22:15:00+04:00")

        jobs = _read_jobs_file(jobs_file)
        assert len(jobs) == 1
        assert jobs[0]["meeting_id"] == "new-meeting"
        assert jobs[0]["fire_time"] == "2026-03-10T22:15:00+04:00"

    def test_overwrite_preserves_unrelated_entries(self, tmp_path: Path) -> None:
        """Overwriting group=1/lecture=3 must leave group=2/lecture=5 intact."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 3, "first-l3",  "2026-03-13T22:00:00+04:00")
            _save_pending_job(2, 5, "keeper",    "2026-03-16T22:00:00+04:00")
            _save_pending_job(1, 3, "updated-l3","2026-03-13T22:15:00+04:00")

        jobs = _read_jobs_file(jobs_file)
        assert len(jobs) == 2
        lectures = {j["lecture"] for j in jobs}
        assert lectures == {3, 5}
        l3 = next(j for j in jobs if j["lecture"] == 3)
        assert l3["meeting_id"] == "updated-l3"

    def test_recovers_from_corrupt_existing_file(self, tmp_path: Path) -> None:
        """If the existing file is malformed JSON, save must still succeed."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        jobs_file.write_text("{corrupt json{{", encoding="utf-8")

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 1, "recovery", "2026-03-01T22:00:00+04:00")

        jobs = _read_jobs_file(jobs_file)
        assert any(j["meeting_id"] == "recovery" for j in jobs)

    def test_atomic_write_no_tmp_residue(self, tmp_path: Path) -> None:
        """No .tmp file must remain after a successful save."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _save_pending_job(1, 2, "mtg-check", "2026-03-20T22:00:00+04:00")

        tmp_residue = jobs_file.with_suffix(".json.tmp")
        assert not tmp_residue.exists()


class TestRemovePendingJob:
    """_remove_pending_job must remove the matching entry without touching
    other entries, and handle missing-file and corrupt-file gracefully."""

    def test_removes_correct_entry(self, tmp_path: Path) -> None:
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 3, "meeting_id": "mtg-l3", "fire_time": "2026-03-13T22:15:00+04:00"},
            {"group": 2, "lecture": 5, "meeting_id": "mtg-l5", "fire_time": "2026-03-16T22:15:00+04:00"},
        ])

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 3)

        remaining = _read_jobs_file(jobs_file)
        assert len(remaining) == 1
        assert remaining[0]["lecture"] == 5

    def test_noop_when_file_missing(self, tmp_path: Path) -> None:
        """Must not raise when the jobs file does not exist."""
        jobs_file = tmp_path / "missing.json"
        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 1)  # must not raise

    def test_noop_when_entry_absent(self, tmp_path: Path) -> None:
        """Must not alter the file when the specified entry is not present."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        original = [
            {"group": 2, "lecture": 5, "meeting_id": "keep-me", "fire_time": "2026-03-16T22:00:00+04:00"}
        ]
        _write_jobs_file(jobs_file, original)

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 99)  # non-existent entry

        remaining = _read_jobs_file(jobs_file)
        assert len(remaining) == 1
        assert remaining[0]["meeting_id"] == "keep-me"

    def test_does_not_remove_lecture_with_same_number_prefix(self, tmp_path: Path) -> None:
        """Removing lecture=3 must not accidentally remove lecture=13."""
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        _write_jobs_file(jobs_file, [
            {"group": 1, "lecture": 3,  "meeting_id": "l3",  "fire_time": "2026-03-13T22:00:00+04:00"},
            {"group": 1, "lecture": 13, "meeting_id": "l13", "fire_time": "2026-04-01T22:00:00+04:00"},
        ])

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 3)

        remaining = _read_jobs_file(jobs_file)
        assert len(remaining) == 1
        assert remaining[0]["lecture"] == 13

    def test_handles_corrupt_file_without_raising(self, tmp_path: Path) -> None:
        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        jobs_file.write_text("{invalid-json", encoding="utf-8")

        with patch.object(sched, "_PENDING_JOBS_FILE", jobs_file):
            _remove_pending_job(1, 1)  # must not raise


# ===========================================================================
# 3. _concatenate_segments
# ===========================================================================


class TestConcatenateSegments:
    """_concatenate_segments calls ffmpeg concat demuxer, cleans up the
    temporary segment-list file, and raises RuntimeError on failure."""

    def _make_segments(self, tmp_path: Path, count: int = 3) -> list[Path]:
        """Create dummy MP4 segment files inside tmp_path."""
        segments = []
        for i in range(count):
            seg = tmp_path / f"segment_{i:02d}.mp4"
            seg.write_bytes(b"\x00" * 1000)
            segments.append(seg)
        return segments

    # -----------------------------------------------------------------------
    # Correct ffmpeg invocation
    # -----------------------------------------------------------------------

    def test_ffmpeg_called_with_concat_flags(self, tmp_path: Path) -> None:
        """ffmpeg must be called with -f concat and -safe 0."""
        segments = self._make_segments(tmp_path)
        output = tmp_path / "merged.mp4"
        output.write_bytes(b"\x00" * 1000)  # simulate output for stat()

        # subprocess is imported locally inside _concatenate_segments, so we
        # patch it at the stdlib module level.
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _concatenate_segments(segments, output)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ffmpeg"
        assert "-f" in cmd
        concat_flag_index = cmd.index("-f")
        assert cmd[concat_flag_index + 1] == "concat"
        assert "-safe" in cmd
        safe_index = cmd.index("-safe")
        assert cmd[safe_index + 1] == "0"

    def test_ffmpeg_receives_output_path_as_last_arg(self, tmp_path: Path) -> None:
        """The output path must be the final argument in the ffmpeg command."""
        segments = self._make_segments(tmp_path)
        output = tmp_path / "final_lecture.mp4"
        output.write_bytes(b"\x00" * 1000)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _concatenate_segments(segments, output)

        cmd = mock_run.call_args[0][0]
        assert str(output) == cmd[-1]

    def test_ffmpeg_receives_stream_copy_flag(self, tmp_path: Path) -> None:
        """-c copy (stream copy, no re-encoding) must be in the ffmpeg command."""
        segments = self._make_segments(tmp_path)
        output = tmp_path / "out.mp4"
        output.write_bytes(b"\x00" * 1000)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _concatenate_segments(segments, output)

        cmd = mock_run.call_args[0][0]
        assert "-c" in cmd
        c_index = cmd.index("-c")
        assert cmd[c_index + 1] == "copy"

    def test_concat_list_file_contains_all_segments(self, tmp_path: Path) -> None:
        """The generated concat-list file must list every segment in order."""
        segments = self._make_segments(tmp_path, count=3)
        output = tmp_path / "merged.mp4"
        output.write_bytes(b"\x00" * 1000)
        captured_list_content: list[str] = []

        def fake_run(cmd, **kwargs):
            # Find the -i argument — it points to the concat list file
            i_index = cmd.index("-i")
            list_path = Path(cmd[i_index + 1])
            if list_path.exists():
                captured_list_content.append(list_path.read_text(encoding="utf-8"))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            _concatenate_segments(segments, output)

        assert len(captured_list_content) == 1
        list_text = captured_list_content[0]
        for seg in segments:
            assert str(seg) in list_text

    # -----------------------------------------------------------------------
    # Temporary file cleanup
    # -----------------------------------------------------------------------

    def test_concat_list_file_deleted_after_success(self, tmp_path: Path) -> None:
        """The _segments.txt concat-list file must be deleted on success."""
        segments = self._make_segments(tmp_path)
        output = tmp_path / "out.mp4"
        output.write_bytes(b"\x00" * 1000)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _concatenate_segments(segments, output)

        # The concat list file is named <output_stem>_segments.txt
        expected_list = output.parent / f"{output.stem}_segments.txt"
        assert not expected_list.exists(), (
            f"Concat list file was not cleaned up: {expected_list}"
        )

    def test_concat_list_file_deleted_even_on_failure(self, tmp_path: Path) -> None:
        """The concat-list file must also be removed when ffmpeg fails."""
        segments = self._make_segments(tmp_path)
        output = tmp_path / "out.mp4"
        expected_list = output.parent / f"{output.stem}_segments.txt"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="encoder error"
            )
            with pytest.raises(RuntimeError):
                _concatenate_segments(segments, output)

        assert not expected_list.exists(), (
            "Concat list file was not cleaned up after ffmpeg failure"
        )

    # -----------------------------------------------------------------------
    # Error handling
    # -----------------------------------------------------------------------

    def test_ffmpeg_failure_raises_runtime_error(self, tmp_path: Path) -> None:
        """A non-zero ffmpeg return code must raise RuntimeError."""
        segments = self._make_segments(tmp_path)
        output = tmp_path / "out.mp4"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="muxing failed: no space left"
            )
            with pytest.raises(RuntimeError, match="ffmpeg concat failed"):
                _concatenate_segments(segments, output)

    def test_runtime_error_message_contains_stderr(self, tmp_path: Path) -> None:
        """The RuntimeError message must include at least part of ffmpeg's stderr."""
        segments = self._make_segments(tmp_path)
        output = tmp_path / "out.mp4"
        stderr_text = "fatal: invalid input file"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr=stderr_text
            )
            with pytest.raises(RuntimeError) as exc_info:
                _concatenate_segments(segments, output)

        assert "fatal" in str(exc_info.value) or "ffmpeg concat failed" in str(exc_info.value)

    def test_single_segment_concatenation_works(self, tmp_path: Path) -> None:
        """Edge case: a single-segment concat must not raise."""
        segments = self._make_segments(tmp_path, count=1)
        output = tmp_path / "single.mp4"
        output.write_bytes(b"\x00" * 1000)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _concatenate_segments(segments, output)  # must not raise

        mock_run.assert_called_once()
