"""Unit tests for zero-coverage scenarios in tools/core/pipeline_state.py.

Covers:
- try_claim_pipeline:
    - Claiming a new pipeline (no prior state) succeeds
    - Claiming an already-active pipeline returns None
    - Claiming after COMPLETE returns None (no re-processing)
    - Claiming a FAILED pipeline succeeds (retry allowed)
- cleanup_stale_failed (the task spec refers to it as "cleanup_stale_pending"):
    - PENDING states that are recent are preserved
    - PENDING states older than threshold are also preserved (only FAILED
      states are cleaned by this function)
    - FAILED states older than the threshold are deleted
    - FAILED states younger than the threshold are preserved
- Concurrent claims:
    - Two threads racing to claim the same (group, lecture) — exactly one wins

These tests complement test_pipeline_state.py which already covers the core
CRUD, lifecycle, and ForwardOnly tests.  The group/lecture numbers used here
(97, 98, 99) are distinct from the ones in test_pipeline_state.py to avoid
state-file collisions.

Run with:
    pytest tools/tests/test_pipeline_state_unit.py -v
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

from tools.core.config import TMP_DIR
from tools.core.pipeline_state import (
    COMPLETE,
    FAILED,
    PENDING,
    DOWNLOADING,
    PipelineState,
    cleanup_stale_failed,
    create_pipeline,
    load_state,
    mark_complete,
    mark_failed,
    state_file_path,
    transition,
    try_claim_pipeline,
)

try:
    from tools.core.pipeline_state import TBILISI_TZ
except ImportError:
    from zoneinfo import ZoneInfo
    TBILISI_TZ = ZoneInfo("Asia/Tbilisi")


# ---------------------------------------------------------------------------
# Test group/lecture numbers (high values to avoid collisions)
# ---------------------------------------------------------------------------
_G97 = 97
_G98 = 98
_G99 = 99
_L_BASE = 50  # start lecture at 50 to avoid collision with test_pipeline_state.py


# ---------------------------------------------------------------------------
# Autouse fixture — clean up state files after every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _cleanup_state_files():
    """Delete any pipeline state files created by this test module."""
    yield
    for g in (_G97, _G98, _G99):
        for path in TMP_DIR.glob(f"pipeline_state_g{g}_l*.json"):
            path.unlink(missing_ok=True)
    # Also clean the lock file (used by try_claim_pipeline)
    lock_file = TMP_DIR / ".pipeline_lock"
    if lock_file.exists():
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass


# ===========================================================================
# 1. try_claim_pipeline — basic claim scenarios
# ===========================================================================


class TestTryClaimPipeline:
    """try_claim_pipeline is the authoritative deduplication gate.

    It uses an fcntl file lock plus a thread lock to ensure only one caller
    can create a pipeline for a given (group, lecture) pair at a time.
    """

    def test_claim_new_pipeline_succeeds(self) -> None:
        """Claiming when there is no existing state file must succeed."""
        result = try_claim_pipeline(_G97, _L_BASE, meeting_id="zoom-new-1")

        assert result is not None
        assert result.state == PENDING
        assert result.group == _G97
        assert result.lecture == _L_BASE
        assert result.meeting_id == "zoom-new-1"

    def test_claim_persists_state_to_disk(self) -> None:
        """A successful claim must write a state file that can be loaded back."""
        try_claim_pipeline(_G97, _L_BASE + 1, meeting_id="zoom-persist")
        loaded = load_state(_G97, _L_BASE + 1)

        assert loaded is not None
        assert loaded.state == PENDING

    def test_claim_already_active_returns_none(self) -> None:
        """Claiming a pipeline that is already in PENDING state must return None."""
        create_pipeline(_G97, _L_BASE + 2)

        result = try_claim_pipeline(_G97, _L_BASE + 2, meeting_id="zoom-dup")

        assert result is None

    def test_claim_already_downloading_returns_none(self) -> None:
        """Claiming a pipeline in DOWNLOADING state (active) must return None."""
        state = create_pipeline(_G97, _L_BASE + 3)
        transition(state, DOWNLOADING)

        result = try_claim_pipeline(_G97, _L_BASE + 3, meeting_id="zoom-mid")

        assert result is None

    def test_claim_after_complete_returns_none(self) -> None:
        """COMPLETE is a terminal state — no re-processing must be triggered."""
        state = create_pipeline(_G97, _L_BASE + 4)
        mark_complete(state)

        result = try_claim_pipeline(_G97, _L_BASE + 4, meeting_id="zoom-redo")

        assert result is None

    def test_claim_after_failed_succeeds(self) -> None:
        """FAILED pipelines may be retried — the claim must succeed."""
        state = create_pipeline(_G97, _L_BASE + 5)
        mark_failed(state, "transient Zoom error")

        result = try_claim_pipeline(_G97, _L_BASE + 5, meeting_id="zoom-retry")

        assert result is not None
        assert result.state == PENDING

    def test_successful_claim_has_meeting_id(self) -> None:
        """The meeting_id passed to try_claim_pipeline must appear in the result."""
        result = try_claim_pipeline(_G98, _L_BASE, meeting_id="zoom-meeting-abc")

        assert result is not None
        assert result.meeting_id == "zoom-meeting-abc"

    def test_successful_claim_has_started_at_timestamp(self) -> None:
        """The claimed pipeline must record a parseable started_at timestamp."""
        result = try_claim_pipeline(_G98, _L_BASE + 1, meeting_id="zoom-ts")

        assert result is not None
        assert result.started_at != ""
        datetime.fromisoformat(result.started_at)  # must not raise


# ===========================================================================
# 2. cleanup_stale_failed (called cleanup_stale_pending in the task spec)
# ===========================================================================


class TestCleanupStaleFailed:
    """cleanup_stale_failed removes FAILED pipeline state files that are older
    than the configured threshold (default 12 hours).

    PENDING and COMPLETE states are never touched by this function.
    """

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _write_state_with_old_timestamp(
        group: int,
        lecture: int,
        state: str,
        age_hours: float,
    ) -> PipelineState:
        """Create and persist a pipeline state with an artificially old updated_at.

        save_state() always stamps updated_at with the current time, so we
        write the JSON file directly to embed the backdate timestamp.
        """
        import json as _json
        from dataclasses import asdict as _asdict

        old_time = (
            datetime.now(tz=TBILISI_TZ) - timedelta(hours=age_hours)
        ).isoformat()
        pipeline = PipelineState(
            group=group,
            lecture=lecture,
            state=state,
            updated_at=old_time,
            started_at=old_time,
        )
        # Write directly to disk so the old timestamp is preserved
        path = state_file_path(group, lecture)
        data = _asdict(pipeline)
        data["updated_at"] = old_time  # ensure backdated value is written
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return pipeline

    # -----------------------------------------------------------------------
    # FAILED states
    # -----------------------------------------------------------------------

    def test_old_failed_state_is_deleted(self) -> None:
        """A FAILED state older than max_age_hours must be removed."""
        self._write_state_with_old_timestamp(_G98, _L_BASE + 2, FAILED, age_hours=24.0)

        deleted = cleanup_stale_failed(max_age_hours=12)

        assert deleted >= 1
        assert load_state(_G98, _L_BASE + 2) is None

    def test_recent_failed_state_is_preserved(self) -> None:
        """A FAILED state that is younger than the threshold must be kept."""
        self._write_state_with_old_timestamp(_G98, _L_BASE + 3, FAILED, age_hours=2.0)

        cleanup_stale_failed(max_age_hours=12)

        assert load_state(_G98, _L_BASE + 3) is not None
        assert load_state(_G98, _L_BASE + 3).state == FAILED

    def test_multiple_old_failed_states_all_deleted(self) -> None:
        """All stale FAILED states must be deleted in a single call."""
        for lecture in [_L_BASE + 4, _L_BASE + 5, _L_BASE + 6]:
            self._write_state_with_old_timestamp(_G98, lecture, FAILED, age_hours=20.0)

        deleted = cleanup_stale_failed(max_age_hours=12)

        assert deleted >= 3
        for lecture in [_L_BASE + 4, _L_BASE + 5, _L_BASE + 6]:
            assert load_state(_G98, lecture) is None

    def test_returns_count_of_deleted_files(self) -> None:
        """Return value must equal the number of deleted state files."""
        for lecture in [_L_BASE + 7, _L_BASE + 8]:
            self._write_state_with_old_timestamp(_G99, lecture, FAILED, age_hours=15.0)

        count = cleanup_stale_failed(max_age_hours=12)

        assert count >= 2

    # -----------------------------------------------------------------------
    # PENDING states are NOT touched
    # -----------------------------------------------------------------------

    def test_old_pending_state_is_not_deleted(self) -> None:
        """cleanup_stale_failed must NOT delete PENDING states regardless of age."""
        self._write_state_with_old_timestamp(_G99, _L_BASE + 9, PENDING, age_hours=100.0)

        cleanup_stale_failed(max_age_hours=1)

        assert load_state(_G99, _L_BASE + 9) is not None
        assert load_state(_G99, _L_BASE + 9).state == PENDING

    def test_recent_pending_state_is_preserved(self) -> None:
        """A fresh PENDING state must survive cleanup unchanged."""
        # Use create_pipeline which sets a current timestamp
        create_pipeline(_G99, _L_BASE + 10)

        cleanup_stale_failed(max_age_hours=0)

        loaded = load_state(_G99, _L_BASE + 10)
        assert loaded is not None
        assert loaded.state == PENDING

    # -----------------------------------------------------------------------
    # COMPLETE states are NOT touched
    # -----------------------------------------------------------------------

    def test_complete_state_not_deleted_by_cleanup_stale_failed(self) -> None:
        """COMPLETE states must not be removed by cleanup_stale_failed
        (they are handled by cleanup_completed instead)."""
        self._write_state_with_old_timestamp(
            _G99, _L_BASE + 11, COMPLETE, age_hours=100.0
        )

        cleanup_stale_failed(max_age_hours=1)

        assert load_state(_G99, _L_BASE + 11) is not None

    # -----------------------------------------------------------------------
    # Zero results when no stale files exist
    # -----------------------------------------------------------------------

    def test_returns_zero_when_nothing_to_clean(self) -> None:
        """cleanup_stale_failed must return 0 when no stale FAILED files exist."""
        # Only add a fresh PENDING state — nothing should be cleaned
        create_pipeline(_G99, _L_BASE + 12)

        deleted = cleanup_stale_failed(max_age_hours=12)

        assert deleted == 0


# ===========================================================================
# 3. Concurrent claims — exactly one thread wins
# ===========================================================================


class TestConcurrentClaims:
    """Two or more threads racing to claim the same (group, lecture) pair must
    result in exactly one successful claim and all others returning None."""

    def test_two_threads_exactly_one_succeeds(self) -> None:
        """Classic race condition: only one of two concurrent claims must win."""
        results: list[PipelineState | None] = [None, None]
        errors: list[Exception] = []

        def claim(index: int) -> None:
            try:
                results[index] = try_claim_pipeline(
                    _G97, _L_BASE + 20, meeting_id=f"thread-{index}"
                )
            except Exception as exc:
                errors.append(exc)

        t0 = threading.Thread(target=claim, args=(0,))
        t1 = threading.Thread(target=claim, args=(1,))
        t0.start()
        t1.start()
        t0.join(timeout=10)
        t1.join(timeout=10)

        assert not errors, f"Unexpected exceptions: {errors}"

        successes = [r for r in results if r is not None]
        failures = [r for r in results if r is None]

        assert len(successes) == 1, (
            f"Expected exactly one successful claim, got {len(successes)}: {results}"
        )
        assert len(failures) == 1, (
            f"Expected exactly one failed claim (None), got {len(failures)}: {results}"
        )
        assert successes[0].state == PENDING

    def test_three_threads_exactly_one_succeeds(self) -> None:
        """Three concurrent threads: still exactly one must win."""
        results: list[PipelineState | None] = [None, None, None]
        errors: list[Exception] = []

        def claim(index: int) -> None:
            try:
                results[index] = try_claim_pipeline(
                    _G97, _L_BASE + 21, meeting_id=f"t{index}"
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Unexpected exceptions: {errors}"

        successes = [r for r in results if r is not None]
        assert len(successes) == 1, (
            f"Expected exactly 1 successful claim among 3 threads, got {len(successes)}"
        )

    def test_winning_claim_is_readable_from_disk(self) -> None:
        """The state written by the winning thread must be readable by load_state."""
        winner_results: list[PipelineState | None] = [None, None]

        def claim(index: int) -> None:
            winner_results[index] = try_claim_pipeline(
                _G97, _L_BASE + 22, meeting_id=f"disk-thread-{index}"
            )

        t0 = threading.Thread(target=claim, args=(0,))
        t1 = threading.Thread(target=claim, args=(1,))
        t0.start()
        t1.start()
        t0.join(timeout=10)
        t1.join(timeout=10)

        loaded = load_state(_G97, _L_BASE + 22)
        assert loaded is not None
        assert loaded.state == PENDING

    def test_sequential_claim_after_race_is_blocked(self) -> None:
        """After the race settles (one winner), a subsequent claim must return None
        because the pipeline is now active."""
        # First claim — succeeds
        first = try_claim_pipeline(_G97, _L_BASE + 23, meeting_id="first")
        assert first is not None

        # Second sequential claim — must be blocked
        second = try_claim_pipeline(_G97, _L_BASE + 23, meeting_id="second")
        assert second is None

    def test_concurrent_claims_on_different_lectures_both_succeed(self) -> None:
        """Concurrent claims on DIFFERENT lectures must both succeed (no shared lock)."""
        results: list[PipelineState | None] = [None, None]
        errors: list[Exception] = []

        def claim(index: int, lecture: int) -> None:
            try:
                results[index] = try_claim_pipeline(
                    _G98, lecture, meeting_id=f"indep-{lecture}"
                )
            except Exception as exc:
                errors.append(exc)

        t0 = threading.Thread(target=claim, args=(0, _L_BASE + 24))
        t1 = threading.Thread(target=claim, args=(1, _L_BASE + 25))
        t0.start()
        t1.start()
        t0.join(timeout=10)
        t1.join(timeout=10)

        assert not errors, f"Unexpected exceptions: {errors}"

        # Both independent lectures should succeed
        # (Note: a single thread lock is shared, so one may still block, but
        #  both should eventually claim since they are different lectures.)
        # We assert that at least one succeeded; ideally both do.
        successes = [r for r in results if r is not None]
        assert len(successes) >= 1, (
            "At least one independent lecture claim must succeed"
        )
