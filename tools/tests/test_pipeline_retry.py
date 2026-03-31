"""Tests for tools/core/pipeline_retry.py.

Covers:
- RetryRecord construction and persistence
- schedule_retry: exponential backoff, max retries, permanent failure
- get_retry_status: pending vs permanently failed
- clear_retry: record removal
- nightly_catch_all: stuck pipelines, Zoom scan, Pinecone gaps
- APScheduler job scheduling (mocked)
- Operator alerts on permanent failure (mocked)
- Edge cases: corrupt tracker file, concurrent access

Run with:
    pytest tools/tests/test_pipeline_retry.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.core.config import TBILISI_TZ, TMP_DIR
from tools.core.pipeline_retry import (
    BACKOFF_MINUTES,
    MAX_RETRIES,
    PERMANENTLY_FAILED,
    RETRY_TRACKER_PATH,
    RetryOrchestrator,
    RetryRecord,
    _execute_retry,
    _load_tracker,
    _record_key,
    _save_tracker,
    _to_record,
    nightly_catch_all,
    retry_orchestrator,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the retry tracker to a temp directory for test isolation."""
    tracker_path = tmp_path / "retry_tracker.json"
    monkeypatch.setattr(
        "tools.core.pipeline_retry.RETRY_TRACKER_PATH", tracker_path
    )
    yield tracker_path
    # Cleanup
    if tracker_path.exists():
        tracker_path.unlink()


@pytest.fixture
def orchestrator() -> RetryOrchestrator:
    """Fresh orchestrator instance for each test."""
    return RetryOrchestrator()


# ---------------------------------------------------------------------------
# Unit tests: data model
# ---------------------------------------------------------------------------


class TestRetryRecord:
    def test_default_construction(self):
        record = RetryRecord(group=1, lecture=3, meeting_id="abc123")
        assert record.group == 1
        assert record.lecture == 3
        assert record.meeting_id == "abc123"
        assert record.attempt == 0
        assert record.errors == []
        assert record.status == "pending"

    def test_record_key(self):
        assert _record_key(1, 3) == "g1_l3"
        assert _record_key(2, 15) == "g2_l15"

    def test_to_record_with_defaults(self):
        record = _to_record({})
        assert record.group == 0
        assert record.lecture == 0
        assert record.meeting_id == ""
        assert record.errors == []

    def test_to_record_full(self):
        data = {
            "group": 2,
            "lecture": 5,
            "meeting_id": "xyz",
            "attempt": 3,
            "errors": ["err1", "err2"],
            "status": "scheduled",
            "next_retry_at": "2026-03-31T03:00:00+04:00",
        }
        record = _to_record(data)
        assert record.group == 2
        assert record.lecture == 5
        assert record.attempt == 3
        assert len(record.errors) == 2
        assert record.status == "scheduled"


# ---------------------------------------------------------------------------
# Unit tests: persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_load_empty_tracker(self, clean_tracker: Path):
        assert _load_tracker() == {}

    def test_save_and_load_roundtrip(self, clean_tracker: Path):
        data = {"g1_l3": {"group": 1, "lecture": 3, "attempt": 2}}
        _save_tracker(data)
        loaded = _load_tracker()
        assert loaded["g1_l3"]["attempt"] == 2

    def test_load_corrupt_file(self, clean_tracker: Path):
        clean_tracker.write_text("not valid json{{{", encoding="utf-8")
        result = _load_tracker()
        assert result == {}

    def test_load_non_dict_json(self, clean_tracker: Path):
        clean_tracker.write_text("[1, 2, 3]", encoding="utf-8")
        result = _load_tracker()
        assert result == {}


# ---------------------------------------------------------------------------
# Unit tests: schedule_retry
# ---------------------------------------------------------------------------


class TestScheduleRetry:
    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_first_retry_schedules_15min(self, mock_sched, orchestrator: RetryOrchestrator):
        result = orchestrator.schedule_retry(1, 3, "abc", "connection timeout")
        assert result["status"] == "scheduled"
        assert result["attempt"] == 1
        assert result["delay_minutes"] == 15
        mock_sched.assert_called_once()

    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_exponential_backoff(self, mock_sched, orchestrator: RetryOrchestrator):
        for i, expected_delay in enumerate(BACKOFF_MINUTES):
            result = orchestrator.schedule_retry(1, 5, "xyz", f"error {i+1}")
            assert result["delay_minutes"] == expected_delay
            assert result["attempt"] == i + 1

    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    @patch.object(RetryOrchestrator, "_alert_permanent_failure")
    def test_max_retries_triggers_permanent_failure(
        self, mock_alert, mock_sched, orchestrator: RetryOrchestrator
    ):
        # Exhaust all retries
        for i in range(MAX_RETRIES):
            orchestrator.schedule_retry(1, 3, "abc", f"error {i+1}")

        # Next attempt should be permanent failure
        result = orchestrator.schedule_retry(1, 3, "abc", "final error")
        assert result["status"] == PERMANENTLY_FAILED
        assert result["attempt"] == MAX_RETRIES + 1
        mock_alert.assert_called_once()

    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_error_history_capped_at_10(self, mock_sched, orchestrator: RetryOrchestrator):
        # Write 12 errors via manual tracker manipulation
        tracker = _load_tracker()
        key = _record_key(1, 1)
        tracker[key] = {
            "group": 1, "lecture": 1, "meeting_id": "m",
            "attempt": 2, "status": "scheduled",
            "errors": [f"old error {i}" for i in range(10)],
        }
        _save_tracker(tracker)

        orchestrator.schedule_retry(1, 1, "m", "new error")
        tracker = _load_tracker()
        assert len(tracker[key]["errors"]) <= 10

    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_meeting_id_updated_on_retry(self, mock_sched, orchestrator: RetryOrchestrator):
        orchestrator.schedule_retry(1, 3, "old_id", "first error")
        orchestrator.schedule_retry(1, 3, "new_id", "second error")
        record = orchestrator.get_record(1, 3)
        assert record is not None
        assert record.meeting_id == "new_id"


# ---------------------------------------------------------------------------
# Unit tests: get_retry_status
# ---------------------------------------------------------------------------


class TestGetRetryStatus:
    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_empty_status(self, mock_sched, orchestrator: RetryOrchestrator):
        status = orchestrator.get_retry_status()
        assert status["total_pending"] == 0
        assert status["total_permanently_failed"] == 0

    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_mixed_status(self, mock_sched, orchestrator: RetryOrchestrator):
        orchestrator.schedule_retry(1, 3, "abc", "err1")
        orchestrator.schedule_retry(2, 5, "xyz", "err2")

        # Make one permanently failed
        for i in range(MAX_RETRIES + 1):
            with patch.object(RetryOrchestrator, "_alert_permanent_failure"):
                orchestrator.schedule_retry(2, 7, "ppp", f"err {i}")

        status = orchestrator.get_retry_status()
        assert status["total_pending"] == 2  # g1_l3, g2_l5
        assert status["total_permanently_failed"] == 1  # g2_l7


# ---------------------------------------------------------------------------
# Unit tests: clear_retry
# ---------------------------------------------------------------------------


class TestClearRetry:
    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_clear_existing(self, mock_sched, orchestrator: RetryOrchestrator):
        orchestrator.schedule_retry(1, 3, "abc", "err")
        assert orchestrator.clear_retry(1, 3) is True
        assert orchestrator.get_record(1, 3) is None

    def test_clear_nonexistent(self, orchestrator: RetryOrchestrator):
        assert orchestrator.clear_retry(1, 99) is False


# ---------------------------------------------------------------------------
# Unit tests: get_record
# ---------------------------------------------------------------------------


class TestGetRecord:
    def test_nonexistent_record(self, orchestrator: RetryOrchestrator):
        assert orchestrator.get_record(1, 99) is None

    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_existing_record(self, mock_sched, orchestrator: RetryOrchestrator):
        orchestrator.schedule_retry(2, 4, "mid", "some error")
        record = orchestrator.get_record(2, 4)
        assert record is not None
        assert record.group == 2
        assert record.lecture == 4
        assert record.attempt == 1


# ---------------------------------------------------------------------------
# Unit tests: APScheduler integration (mocked)
# ---------------------------------------------------------------------------


class TestAPSchedulerIntegration:
    def test_scheduler_not_running_graceful(self, orchestrator: RetryOrchestrator):
        """schedule_retry should not crash when scheduler is not running."""
        with patch(
            "tools.app.scheduler._get_running_scheduler",
            side_effect=RuntimeError("not started"),
        ):
            result = orchestrator.schedule_retry(1, 1, "m", "err")
            assert result["status"] == "scheduled"

    def test_scheduler_available_adds_job(self, orchestrator: RetryOrchestrator):
        mock_scheduler = MagicMock()
        with patch(
            "tools.app.scheduler._get_running_scheduler",
            return_value=mock_scheduler,
        ):
            # Use the private method directly to test APScheduler wiring
            fire_at = datetime.now(tz=TBILISI_TZ) + timedelta(minutes=15)
            orchestrator._schedule_apscheduler_job(1, 3, "abc", fire_at)
            mock_scheduler.add_job.assert_called_once()
            call_kwargs = mock_scheduler.add_job.call_args
            assert call_kwargs.kwargs["id"] == "retry_g1_l3_attempt"


# ---------------------------------------------------------------------------
# Unit tests: _execute_retry
# ---------------------------------------------------------------------------


class TestExecuteRetry:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_skip_if_already_complete(self):
        """Retry should no-op if the lecture is already processed."""
        with (
            patch("tools.core.pipeline_state.load_state", return_value=None),
            patch("tools.core.pipeline_state.is_pipeline_done", return_value=True),
            patch.object(retry_orchestrator, "clear_retry") as mock_clear,
        ):
            await _execute_retry(1, 3, "abc")
            mock_clear.assert_called_once_with(1, 3)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_skip_if_already_active(self):
        """Retry should skip if pipeline is already running."""
        with (
            patch("tools.core.pipeline_state.is_pipeline_done", return_value=False),
            patch("tools.core.pipeline_state.is_pipeline_active", return_value=True),
        ):
            await _execute_retry(1, 3, "abc")
            # No error = success (just skipped)


# ---------------------------------------------------------------------------
# Unit tests: nightly_catch_all
# ---------------------------------------------------------------------------


class TestNightlyCatchAll:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_empty_scan(self):
        with (
            patch("tools.core.pipeline_state.list_all_pipelines", return_value=[]),
            patch(
                "tools.core.pipeline_retry._check_zoom_recordings",
                new_callable=AsyncMock,
            ),
            patch(
                "tools.core.pipeline_retry._check_pinecone_gaps",
                new_callable=AsyncMock,
            ),
        ):
            result = await nightly_catch_all()
            assert result["stuck_reset"] == 0
            assert result["retries_scheduled"] == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_stuck_pipeline_detected(self):
        """Pipelines stuck for >4 hours should be marked failed and retried."""
        from tools.core.pipeline_state import PipelineState

        stuck_time = (datetime.now(tz=TBILISI_TZ) - timedelta(hours=5)).isoformat()
        stuck_pipeline = PipelineState(
            group=1, lecture=3, state="TRANSCRIBING",
            meeting_id="abc123",
            started_at=stuck_time, updated_at=stuck_time,
        )

        with (
            patch(
                "tools.core.pipeline_state.list_all_pipelines",
                return_value=[stuck_pipeline],
            ),
            patch("tools.core.pipeline_state.mark_failed") as mock_fail,
            patch.object(retry_orchestrator, "get_record", return_value=None),
            patch.object(retry_orchestrator, "schedule_retry") as mock_retry,
            patch(
                "tools.core.pipeline_retry._check_zoom_recordings",
                new_callable=AsyncMock,
            ),
            patch(
                "tools.core.pipeline_retry._check_pinecone_gaps",
                new_callable=AsyncMock,
            ),
        ):
            result = await nightly_catch_all()
            assert result["stuck_reset"] == 1
            mock_fail.assert_called_once()
            mock_retry.assert_called_once()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_concurrent_retries_different_lectures(
        self, mock_sched, orchestrator: RetryOrchestrator
    ):
        """Multiple lectures can have independent retry tracks."""
        orchestrator.schedule_retry(1, 1, "m1", "err1")
        orchestrator.schedule_retry(1, 2, "m2", "err2")
        orchestrator.schedule_retry(2, 1, "m3", "err3")

        status = orchestrator.get_retry_status()
        assert status["total_pending"] == 3

    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_backoff_index_capped(self, mock_sched, orchestrator: RetryOrchestrator):
        """Backoff delay should not exceed the last entry in BACKOFF_MINUTES."""
        for i in range(MAX_RETRIES):
            result = orchestrator.schedule_retry(1, 1, "m", f"err{i}")

        # Last attempt should use the last backoff value
        assert result["delay_minutes"] == BACKOFF_MINUTES[-1]

    def test_singleton_instance_exists(self):
        """Module-level singleton should be available."""
        assert retry_orchestrator is not None
        assert isinstance(retry_orchestrator, RetryOrchestrator)

    @patch.object(RetryOrchestrator, "_schedule_apscheduler_job")
    def test_permanently_failed_not_retriable(
        self, mock_sched, orchestrator: RetryOrchestrator
    ):
        """Once permanently failed, further schedule_retry calls stay failed."""
        for i in range(MAX_RETRIES + 1):
            with patch.object(RetryOrchestrator, "_alert_permanent_failure"):
                orchestrator.schedule_retry(1, 1, "m", f"err{i}")

        # Already permanently failed — next call should also return permanently failed
        with patch.object(RetryOrchestrator, "_alert_permanent_failure"):
            result = orchestrator.schedule_retry(1, 1, "m", "one more try")
        assert result["status"] == PERMANENTLY_FAILED
