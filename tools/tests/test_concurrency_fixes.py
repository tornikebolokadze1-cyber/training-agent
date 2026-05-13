"""Regression tests for Wave 1 concurrency bug fixes.

Covers:
  C-2  create_pipeline TOCTOU — atomic SELECT+INSERT under concurrent callers
  C-4  _remove_pending_job non-atomic write — must use tmp_path.replace()
  H-2  _save_pending_job concurrent race — lock prevents lost entries
  H-5  _evict_stale_tasks runs without _processing_lock
  C-1  _cleanup_dedup pops without _processing_lock (scheduler.py)

Run with:
    pytest tools/tests/test_concurrency_fixes.py -v
"""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_LOCK_TEST_GROUP = 77   # high numbers avoid collision with other test suites
_LOCK_TEST_LECTURE = 77


# ---------------------------------------------------------------------------
# C-2: create_pipeline is atomic under concurrent calls
# ---------------------------------------------------------------------------


class TestCreatePipelineAtomic:
    """Concurrent calls to create_pipeline for the same (group, lecture)
    must result in exactly one winner and at least one raiser."""

    def _cleanup(self, group: int, lecture: int) -> None:
        from tools.core.pipeline_state import state_file_path
        p = state_file_path(group, lecture)
        if p.exists():
            p.unlink()

    def test_concurrent_create_exactly_one_succeeds(self) -> None:
        """Two threads racing on create_pipeline: exactly 1 succeeds, 1 raises."""
        from tools.core.pipeline_state import create_pipeline

        for attempt in range(10):
            group = _LOCK_TEST_GROUP
            lecture = _LOCK_TEST_LECTURE + attempt  # fresh slot each iteration

            self._cleanup(group, lecture)
            try:
                successes: list[Any] = []
                errors: list[Exception] = []
                barrier = threading.Barrier(2)

                def _worker(meeting_id: str) -> None:
                    barrier.wait()  # synchronise start to maximise race window
                    try:
                        state = create_pipeline(group, lecture, meeting_id=meeting_id)
                        successes.append(state)
                    except ValueError as exc:
                        errors.append(exc)

                t1 = threading.Thread(target=_worker, args=("meet-A",))
                t2 = threading.Thread(target=_worker, args=("meet-B",))
                t1.start()
                t2.start()
                t1.join(timeout=5)
                t2.join(timeout=5)

                assert not t1.is_alive() and not t2.is_alive(), (
                    f"Attempt {attempt}: thread(s) did not finish"
                )
                total = len(successes) + len(errors)
                assert total == 2, (
                    f"Attempt {attempt}: expected 2 outcomes, got {total}"
                )
                assert len(successes) == 1, (
                    f"Attempt {attempt}: expected exactly 1 success, got {len(successes)}"
                )
                assert len(errors) == 1, (
                    f"Attempt {attempt}: expected exactly 1 ValueError, got {len(errors)}"
                )
            finally:
                self._cleanup(group, lecture)

    def test_second_caller_sees_first_callers_state(self) -> None:
        """After two concurrent calls, the on-disk state must be from the winner."""
        from tools.core.pipeline_state import create_pipeline, load_state

        group = _LOCK_TEST_GROUP
        lecture = _LOCK_TEST_LECTURE + 20

        self._cleanup(group, lecture)
        try:
            winner_meeting_id: list[str] = []
            barrier = threading.Barrier(2)

            def _worker(meeting_id: str) -> None:
                barrier.wait()
                try:
                    create_pipeline(group, lecture, meeting_id=meeting_id)
                    winner_meeting_id.append(meeting_id)
                except ValueError:
                    pass

            t1 = threading.Thread(target=_worker, args=("meet-WIN",))
            t2 = threading.Thread(target=_worker, args=("meet-LOSE",))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            assert len(winner_meeting_id) == 1
            on_disk = load_state(group, lecture)
            assert on_disk is not None
            assert on_disk.meeting_id == winner_meeting_id[0], (
                f"Disk state meeting_id={on_disk.meeting_id!r} does not match "
                f"winner={winner_meeting_id[0]!r}"
            )
        finally:
            self._cleanup(group, lecture)


# ---------------------------------------------------------------------------
# C-4: _remove_pending_job uses atomic replace
# ---------------------------------------------------------------------------


class TestRemovePendingJobAtomic:
    """_remove_pending_job must write via tmp_path.replace(), not write_text."""

    def test_remove_pending_job_uses_atomic_replace(self) -> None:
        """Source-level check: the function body must contain tmp_path (or .tmp)
        AND .replace(  so we know atomic-write is used."""
        import tools.app.scheduler as sched

        src = inspect.getsource(sched._remove_pending_job)

        has_tmp = ".json.tmp" in src or "tmp_path" in src
        has_replace = ".replace(" in src

        assert has_tmp, (
            "_remove_pending_job must write to a .tmp file first (atomic pattern)"
        )
        assert has_replace, (
            "_remove_pending_job must call .replace() to atomically rename the tmp file"
        )

    def test_remove_pending_job_does_not_use_bare_write_text(self) -> None:
        """write_text must NOT appear outside the tmp_path assignment line
        (i.e., there must not be a direct write_text to _PENDING_JOBS_FILE)."""
        import tools.app.scheduler as sched

        src = inspect.getsource(sched._remove_pending_job)
        lines = src.splitlines()

        # Acceptable: tmp_path.write_text(...)
        # Not acceptable: _PENDING_JOBS_FILE.write_text(...)
        for line in lines:
            stripped = line.strip()
            if "write_text" in stripped and "_PENDING_JOBS_FILE" in stripped:
                pytest.fail(
                    f"_remove_pending_job writes directly to _PENDING_JOBS_FILE: {stripped!r}. "
                    "Use atomic tmp_path.replace() instead."
                )


# ---------------------------------------------------------------------------
# H-2 + C-4 (lock): concurrent _save_pending_job calls both survive
# ---------------------------------------------------------------------------


class TestPendingJobsConcurrentSaveAndRemove:
    """Two threads calling _save_pending_job simultaneously must not lose entries."""

    def test_concurrent_save_both_entries_survive(self, tmp_path: Path) -> None:
        import tools.app.scheduler as sched

        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        original_file = sched._PENDING_JOBS_FILE

        # Patch the module-level path to our temp file
        sched._PENDING_JOBS_FILE = jobs_file
        try:
            barrier = threading.Barrier(2)
            errors: list[Exception] = []

            def _save_group(group_number: int) -> None:
                barrier.wait()  # maximise race window
                try:
                    sched._save_pending_job(
                        group_number=group_number,
                        lecture_number=1,
                        meeting_id=f"meet-g{group_number}",
                        fire_time_iso="2026-05-13T23:30:00+04:00",
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=_save_group, args=(1,))
            t2 = threading.Thread(target=_save_group, args=(2,))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            assert not errors, f"Unexpected errors during concurrent save: {errors}"

            final = json.loads(jobs_file.read_text(encoding="utf-8"))
            groups_in_file = {entry["group"] for entry in final}
            assert 1 in groups_in_file, "Group 1 entry was lost during concurrent save"
            assert 2 in groups_in_file, "Group 2 entry was lost during concurrent save"
        finally:
            sched._PENDING_JOBS_FILE = original_file

    def test_concurrent_save_and_remove_no_corruption(self, tmp_path: Path) -> None:
        """One thread saves while another removes a different entry — file stays valid."""
        import tools.app.scheduler as sched

        jobs_file = tmp_path / "pending_post_meeting_jobs.json"
        # Pre-seed with one entry for group 2 (to be removed)
        jobs_file.write_text(json.dumps([
            {"group": 2, "lecture": 1, "meeting_id": "old", "fire_time": "2026-05-13T22:00:00+04:00"}
        ]), encoding="utf-8")

        original_file = sched._PENDING_JOBS_FILE
        sched._PENDING_JOBS_FILE = jobs_file
        try:
            barrier = threading.Barrier(2)
            errors: list[Exception] = []

            def _saver() -> None:
                barrier.wait()
                try:
                    sched._save_pending_job(1, 1, "meet-new", "2026-05-13T23:30:00+04:00")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            def _remover() -> None:
                barrier.wait()
                try:
                    sched._remove_pending_job(2, 1)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            t1 = threading.Thread(target=_saver)
            t2 = threading.Thread(target=_remover)
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

            assert not errors, f"Unexpected errors: {errors}"

            # File must be valid JSON
            final = json.loads(jobs_file.read_text(encoding="utf-8"))
            assert isinstance(final, list)
            # The new group-1 entry must be present
            assert any(e["group"] == 1 for e in final), "Group 1 save was lost"
            # The old group-2 entry should be removed (or absent)
            assert not any(e["group"] == 2 for e in final), "Group 2 was not removed"
        finally:
            sched._PENDING_JOBS_FILE = original_file


# ---------------------------------------------------------------------------
# H-5: _evict_stale_tasks acquires _processing_lock
# ---------------------------------------------------------------------------


class TestEvictStaleTasksHoldsLock:
    """_evict_stale_tasks must hold _processing_lock during the snapshot+pop."""

    def test_evict_stale_tasks_acquires_processing_lock(self) -> None:
        """Source-level check: _processing_lock context manager is used."""
        import tools.app.server as srv

        src = inspect.getsource(srv._evict_stale_tasks)
        assert "_processing_lock" in src, (
            "_evict_stale_tasks must reference _processing_lock"
        )
        # The lock must be used as a context manager (with statement)
        assert "with _processing_lock" in src, (
            "_evict_stale_tasks must acquire _processing_lock via 'with _processing_lock:'"
        )

    def test_evict_stale_tasks_lock_called_before_pop(self) -> None:
        """Integration check: mock the lock and confirm __enter__ is called."""
        import tools.app.server as srv

        real_lock = srv._processing_lock
        mock_lock = MagicMock(spec=threading.Lock())
        mock_lock.__enter__ = MagicMock(return_value=None)
        mock_lock.__exit__ = MagicMock(return_value=False)

        srv._processing_lock = mock_lock  # type: ignore[assignment]
        try:
            srv._evict_stale_tasks()
            mock_lock.__enter__.assert_called()
        finally:
            srv._processing_lock = real_lock  # type: ignore[assignment]

    def test_cleanup_dedup_acquires_processing_lock(self) -> None:
        """Source-level check: _cleanup_dedup in scheduler.py uses _processing_lock."""
        import tools.app.scheduler as sched

        src = inspect.getsource(sched._run_post_meeting_pipeline)
        # _cleanup_dedup is a nested function — check it's in the outer source
        assert "_processing_lock" in src, (
            "_cleanup_dedup in _run_post_meeting_pipeline must import and use "
            "_processing_lock from server.py (audit finding C-1)"
        )
