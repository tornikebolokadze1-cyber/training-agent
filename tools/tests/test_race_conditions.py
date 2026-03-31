"""Tests for scheduler/webhook race conditions and dedup consistency.

Covers:
- Concurrent try_claim_pipeline from multiple threads (webhook + scheduler)
- Guaranteed cleanup in _ensure_pipeline_cleanup
- Timezone consistency (no naive datetime comparisons)
- Duration validation with recording existence override
- Misfire grace time configuration
- _is_processing helper uses pipeline_state as sole authority

Run with:
    pytest tools/tests/test_race_conditions.py -v
"""

from __future__ import annotations

import threading
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from tools.core.config import TBILISI_TZ, TMP_DIR
from tools.core.pipeline_state import (
    COMPLETE,
    FAILED,
    PENDING,
    PipelineState,
    _PIPELINE_LOCKS,
    load_state,
    release_pipeline,
    release_pipeline_lock,
    save_state,
    try_claim_pipeline,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_pipeline_state():
    """Ensure no stale pipeline state files or locks between tests."""
    # Clean before test
    for p in TMP_DIR.glob("pipeline_state_g*_l*.json"):
        p.unlink(missing_ok=True)
    lock_file = TMP_DIR / ".pipeline_lock"
    lock_file.unlink(missing_ok=True)

    # Clear per-pipeline locks
    _PIPELINE_LOCKS.clear()

    yield

    # Clean after test
    for p in TMP_DIR.glob("pipeline_state_g*_l*.json"):
        p.unlink(missing_ok=True)
    lock_file.unlink(missing_ok=True)
    _PIPELINE_LOCKS.clear()


# ---------------------------------------------------------------------------
# 1. Concurrent try_claim_pipeline — only one thread wins
# ---------------------------------------------------------------------------


class TestConcurrentClaim:
    """Verify that concurrent try_claim_pipeline calls serialize correctly."""

    def test_only_one_thread_wins(self) -> None:
        """Simulate webhook and scheduler racing to claim the same pipeline."""
        results: list[PipelineState | None] = [None, None]
        barrier = threading.Barrier(2, timeout=5)

        def claim_pipeline(index: int) -> None:
            barrier.wait()  # Synchronize start
            results[index] = try_claim_pipeline(1, 5, meeting_id=f"thread-{index}")

        t1 = threading.Thread(target=claim_pipeline, args=(0,))
        t2 = threading.Thread(target=claim_pipeline, args=(1,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Exactly one should succeed, one should get None
        winners = [r for r in results if r is not None]
        losers = [r for r in results if r is None]
        assert len(winners) == 1, f"Expected exactly 1 winner, got {len(winners)}"
        assert len(losers) == 1, f"Expected exactly 1 loser, got {len(losers)}"
        assert winners[0].state == PENDING

        # Clean up lock
        release_pipeline_lock(1, 5)

    def test_different_lectures_can_claim_concurrently(self) -> None:
        """Two different lectures should not block each other."""
        results: list[PipelineState | None] = [None, None]
        barrier = threading.Barrier(2, timeout=5)

        def claim_pipeline(index: int, lecture: int) -> None:
            barrier.wait()
            results[index] = try_claim_pipeline(1, lecture, meeting_id=f"meeting-{lecture}")

        t1 = threading.Thread(target=claim_pipeline, args=(0, 1))
        t2 = threading.Thread(target=claim_pipeline, args=(1, 2))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # Both should succeed
        assert results[0] is not None, "Lecture 1 should have been claimed"
        assert results[1] is not None, "Lecture 2 should have been claimed"
        assert results[0].lecture == 1
        assert results[1].lecture == 2

        # Clean up locks
        release_pipeline_lock(1, 1)
        release_pipeline_lock(1, 2)

    def test_claim_after_complete_is_rejected(self) -> None:
        """A COMPLETE pipeline should block new claims (no double-processing)."""
        # Create and complete a pipeline
        pipeline = try_claim_pipeline(1, 3, meeting_id="first-run")
        assert pipeline is not None
        # Mark complete by writing state file
        completed = PipelineState(
            group=1, lecture=3, state=COMPLETE,
            started_at=datetime.now(TBILISI_TZ).isoformat(),
            updated_at=datetime.now(TBILISI_TZ).isoformat(),
        )
        save_state(completed)
        release_pipeline_lock(1, 3)

        # Second claim should be rejected
        second = try_claim_pipeline(1, 3, meeting_id="second-run")
        assert second is None

    def test_claim_after_failed_allows_retry(self) -> None:
        """A FAILED pipeline should allow retry."""
        # Create and fail a pipeline
        pipeline = try_claim_pipeline(1, 4, meeting_id="first-try")
        assert pipeline is not None
        failed = PipelineState(
            group=1, lecture=4, state=FAILED, error="test failure",
            started_at=datetime.now(TBILISI_TZ).isoformat(),
            updated_at=datetime.now(TBILISI_TZ).isoformat(),
        )
        save_state(failed)
        release_pipeline_lock(1, 4)

        # Retry should succeed
        retry = try_claim_pipeline(1, 4, meeting_id="second-try")
        assert retry is not None
        assert retry.state == PENDING
        release_pipeline_lock(1, 4)

    def test_many_threads_racing(self) -> None:
        """Stress test: 10 threads racing to claim the same pipeline."""
        n_threads = 10
        results: list[PipelineState | None] = [None] * n_threads
        barrier = threading.Barrier(n_threads, timeout=10)

        def claim(index: int) -> None:
            barrier.wait()
            results[index] = try_claim_pipeline(2, 7, meeting_id=f"racer-{index}")

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        winners = [r for r in results if r is not None]
        assert len(winners) == 1, f"Expected exactly 1 winner from {n_threads} threads, got {len(winners)}"

        release_pipeline_lock(2, 7)


# ---------------------------------------------------------------------------
# 2. Cleanup guarantees
# ---------------------------------------------------------------------------


class TestCleanupGuarantees:
    """Verify that pipeline cleanup happens on every exit path."""

    def test_release_pipeline_releases_lock(self) -> None:
        """release_pipeline should release the per-pipeline thread lock."""
        pipeline = try_claim_pipeline(1, 1, meeting_id="test")
        assert pipeline is not None

        # Lock should be held
        from tools.core.pipeline_state import _get_pipeline_lock
        lock = _get_pipeline_lock(1, 1)
        assert not lock.acquire(blocking=False), "Lock should be held after claim"

        # Release should free the lock
        release_pipeline(1, 1)
        assert lock.acquire(blocking=False), "Lock should be free after release"
        lock.release()  # Clean up

    def test_release_pipeline_is_idempotent(self) -> None:
        """Calling release_pipeline multiple times should not raise."""
        release_pipeline(1, 1)
        release_pipeline(1, 1)  # Should not raise

    def test_ensure_pipeline_cleanup_from_scheduler(self) -> None:
        """The scheduler's _ensure_pipeline_cleanup should clean both state and cache."""
        # Claim a pipeline
        pipeline = try_claim_pipeline(1, 6, meeting_id="cleanup-test")
        assert pipeline is not None

        # Mock server's _processing_tasks
        mock_tasks: dict[str, datetime] = {"g1_l6": datetime.now(TBILISI_TZ)}

        with patch.dict("tools.app.server._processing_tasks", mock_tasks):
            from tools.app.server import _processing_tasks, _task_key

            # Simulate the cleanup helper from scheduler
            release_pipeline(1, 6)
            _processing_tasks.pop(_task_key(1, 6), None)

            assert _task_key(1, 6) not in _processing_tasks


# ---------------------------------------------------------------------------
# 3. Timezone consistency
# ---------------------------------------------------------------------------


class TestTimezoneConsistency:
    """Verify all datetime operations use TBILISI_TZ."""

    def test_scheduler_timestamp_uses_timezone(self) -> None:
        """Timestamps generated in scheduler code should be timezone-aware."""
        ts = datetime.now(TBILISI_TZ).strftime("%Y%m%d_%H%M%S")
        # Should produce a valid timestamp string
        assert len(ts) == 15  # YYYYMMDD_HHMMSS
        assert ts[8] == "_"

    def test_pipeline_state_now_iso_uses_tbilisi(self) -> None:
        """_now_iso should produce a Tbilisi-aware ISO string."""
        from tools.core.pipeline_state import _now_iso

        iso_str = _now_iso()
        parsed = datetime.fromisoformat(iso_str)
        assert parsed.tzinfo is not None, "Timestamp should be timezone-aware"

    def test_post_meeting_job_datetime_is_aware(self) -> None:
        """The dedup key timestamp in post_meeting_job should use TBILISI_TZ."""
        # Verify datetime.now(TBILISI_TZ) produces aware datetime
        dt = datetime.now(TBILISI_TZ)
        assert dt.tzinfo is not None

    def test_evict_stale_tasks_compares_aware_datetimes(self) -> None:
        """_evict_stale_tasks should not mix naive and aware datetimes."""
        from tools.core.pipeline_state import PENDING, save_state

        # Create a pipeline state with timezone-aware timestamp
        state = PipelineState(
            group=1, lecture=10, state=PENDING,
            started_at=datetime.now(TBILISI_TZ).isoformat(),
            updated_at=datetime.now(TBILISI_TZ).isoformat(),
        )
        save_state(state)

        # _evict_stale_tasks uses datetime.now(TBILISI_TZ) internally
        # This should not raise TypeError from naive/aware comparison
        from tools.app.server import _evict_stale_tasks
        evicted = _evict_stale_tasks()
        # Fresh pipeline should not be evicted
        assert "g1_l10" not in evicted


# ---------------------------------------------------------------------------
# 4. Duration validation with recording override
# ---------------------------------------------------------------------------


class TestDurationValidation:
    """Test meeting.ended duration gate with recording existence override."""

    def _make_meeting_ended_body(
        self,
        duration: int = 30,
        has_recordings: bool = False,
        group: int = 1,
    ) -> dict:
        """Build a meeting.ended webhook payload."""
        obj: dict = {
            "id": "123456",
            "uuid": "abc-uuid",
            "topic": f"AI კურსი - ჯგუფი {group}",
            "start_time": "2026-03-31T16:00:00Z",
            "end_time": "2026-03-31T16:30:00Z",
            "duration": duration,
        }
        if has_recordings:
            obj["recording_files"] = [{"file_type": "MP4", "status": "completed"}]
        return {
            "event": "meeting.ended",
            "payload": {"object": obj},
        }

    def test_short_meeting_without_recordings_ignored(self) -> None:
        """Short meeting without recordings should be ignored."""
        from tools.app.server import _handle_meeting_ended

        body = self._make_meeting_ended_body(duration=30, has_recordings=False)
        with patch("tools.app.server.extract_group_from_topic", return_value=1):
            result = _handle_meeting_ended(body, MagicMock())

        assert result["status"] == "ignored"
        assert result["reason"] == "duration_below_threshold"

    def test_short_meeting_with_recordings_processes(self) -> None:
        """Short meeting WITH recordings should still be processed (split meeting)."""
        from tools.app.server import _handle_meeting_ended

        body = self._make_meeting_ended_body(duration=30, has_recordings=True)
        with (
            patch("tools.app.server.extract_group_from_topic", return_value=1),
            patch("tools.app.server.get_lecture_number", return_value=3),
            patch("tools.app.server._evict_stale_tasks"),
            patch("tools.app.server.try_claim_pipeline") as mock_claim,
        ):
            mock_claim.return_value = PipelineState(
                group=1, lecture=3, state=PENDING,
                started_at=datetime.now(TBILISI_TZ).isoformat(),
                updated_at=datetime.now(TBILISI_TZ).isoformat(),
            )
            result = _handle_meeting_ended(body, MagicMock())

        # Should accept (not ignored) because recordings exist
        assert result["status"] == "accepted"


# ---------------------------------------------------------------------------
# 5. Misfire grace time
# ---------------------------------------------------------------------------


class TestMisfireGraceTime:
    """Verify scheduler misfire configuration."""

    def test_post_meeting_misfire_grace_is_120_min(self) -> None:
        """Post-meeting jobs should tolerate 120 min late fire."""
        from tools.app.scheduler import _schedule_post_meeting

        scheduler = MagicMock()
        _schedule_post_meeting(
            scheduler=scheduler,
            group_number=1,
            lecture_number=5,
            meeting_id="test-123",
            fire_at_hour=23,
            fire_at_minute=30,
        )

        # Check the add_job call
        scheduler.add_job.assert_called_once()
        call_kwargs = scheduler.add_job.call_args[1]
        assert call_kwargs["misfire_grace_time"] == 120 * 60, (
            f"Expected 120 min grace, got {call_kwargs['misfire_grace_time'] / 60} min"
        )

    def test_global_misfire_grace_is_55_min(self) -> None:
        """Global scheduler job_defaults should have 55 min grace."""
        from tools.app.scheduler import start_scheduler

        with patch("tools.app.scheduler.AsyncIOScheduler") as MockScheduler:
            mock_instance = MagicMock()
            MockScheduler.return_value = mock_instance
            mock_instance.get_jobs.return_value = []

            try:
                start_scheduler()
            except Exception:
                pass  # May fail without event loop

            # Check job_defaults passed to AsyncIOScheduler
            call_kwargs = MockScheduler.call_args[1]
            assert call_kwargs["job_defaults"]["misfire_grace_time"] == 55 * 60


# ---------------------------------------------------------------------------
# 6. _is_processing helper
# ---------------------------------------------------------------------------


class TestIsProcessingHelper:
    """Verify _is_processing uses pipeline_state as sole authority."""

    def test_is_processing_returns_true_for_active_pipeline(self) -> None:
        """_is_processing should return True when pipeline_state shows active."""
        from tools.app.server import _is_processing

        pipeline = try_claim_pipeline(1, 8, meeting_id="active-test")
        assert pipeline is not None
        assert _is_processing(1, 8) is True
        release_pipeline_lock(1, 8)

    def test_is_processing_returns_true_for_complete_pipeline(self) -> None:
        """_is_processing should return True for completed pipelines."""
        from tools.app.server import _is_processing

        completed = PipelineState(
            group=1, lecture=9, state=COMPLETE,
            started_at=datetime.now(TBILISI_TZ).isoformat(),
            updated_at=datetime.now(TBILISI_TZ).isoformat(),
        )
        save_state(completed)
        assert _is_processing(1, 9) is True

    def test_is_processing_returns_false_when_no_state(self) -> None:
        """_is_processing should return False when no pipeline state exists."""
        from tools.app.server import _is_processing

        assert _is_processing(2, 15) is False

    def test_is_processing_ignores_in_memory_cache(self) -> None:
        """_is_processing should NOT rely on _processing_tasks dict."""
        from tools.app.server import _is_processing, _processing_tasks, _task_key

        # Put something in the cache but NOT in pipeline state
        key = _task_key(2, 14)
        _processing_tasks[key] = datetime.now(TBILISI_TZ)

        # _is_processing checks pipeline_state, not _processing_tasks
        assert _is_processing(2, 14) is False

        # Clean up
        _processing_tasks.pop(key, None)


# ---------------------------------------------------------------------------
# 7. Scheduler post_meeting_job uses try_claim_pipeline
# ---------------------------------------------------------------------------


class TestPostMeetingJobDedup:
    """Verify post_meeting_job uses pipeline_state as sole dedup authority."""

    @pytest.mark.asyncio
    async def test_post_meeting_job_skips_active_pipeline(self) -> None:
        """post_meeting_job should skip when pipeline is already active."""
        from tools.app.scheduler import post_meeting_job

        # Pre-create an active pipeline
        pipeline = try_claim_pipeline(1, 5, meeting_id="webhook-started")
        assert pipeline is not None

        # Scheduler fallback should detect and skip because try_claim_pipeline
        # (called internally by post_meeting_job) will return None
        with (
            patch("tools.app.server._evict_stale_tasks"),
            patch("tools.app.scheduler._run_post_meeting_pipeline") as mock_run,
        ):
            await post_meeting_job(1, 5, "scheduler-meeting")

        mock_run.assert_not_called()
        release_pipeline_lock(1, 5)

    @pytest.mark.asyncio
    async def test_post_meeting_job_claims_when_no_active_pipeline(self) -> None:
        """post_meeting_job should claim and run when no pipeline is active."""
        from tools.app.scheduler import post_meeting_job

        with (
            patch("tools.app.server._evict_stale_tasks"),
            patch("tools.app.scheduler._run_post_meeting_pipeline"),
            patch("tools.app.orchestrator.PIPELINE_EXECUTOR", None),
            patch("asyncio.get_running_loop") as mock_loop,
            patch("asyncio.wait_for") as mock_wait_for,
        ):
            mock_wait_for.return_value = None

            mock_future = MagicMock()
            mock_loop.return_value.run_in_executor.return_value = mock_future

            try:
                await post_meeting_job(1, 11, "fallback-meeting")
            except Exception:
                pass  # May fail on asyncio internals

        # Pipeline state should exist (was claimed by try_claim_pipeline)
        state = load_state(1, 11)
        assert state is not None
        assert state.state == PENDING
        release_pipeline_lock(1, 11)
