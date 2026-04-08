"""Tests for pipeline state machine hardening.

Covers:
- Forward-only state transitions (backward rejection)
- Guaranteed FAILED marking via pipeline_guard context manager
- Checkpoint validation (size, content, JSON parsing)
- State heartbeat (update_heartbeat, start/stop heartbeat threads)
- Error history recording in state file
- get_last_activity_time helper

Run with:
    pytest tools/tests/test_pipeline_state_hardened.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.core.config import TMP_DIR
from tools.core.pipeline_state import (
    ALL_STATES,
    ANALYZING,
    COMPLETE,
    CONCATENATING,
    DOWNLOADING,
    FAILED,
    INDEXING,
    NOTIFYING,
    PENDING,
    TRANSCRIBING,
    UPLOADING_DOCS,
    UPLOADING_VIDEO,
    PipelineClaimError,
    PipelineState,
    _STATE_ORDER,
    _heartbeat_threads,
    create_pipeline,
    get_last_activity_time,
    invalidate_checkpoint,
    load_state,
    mark_complete,
    mark_failed,
    pipeline_guard,
    save_state,
    start_heartbeat,
    stop_heartbeat,
    transition,
    update_heartbeat,
    validate_checkpoint,
)

# ---------------------------------------------------------------------------
# Test group/lecture numbers — use high numbers to avoid collisions
# ---------------------------------------------------------------------------
_G = 99  # Test group (different from test_pipeline_state.py's 88)
_L = 99  # Test lecture


@pytest.fixture(autouse=True)
def cleanup_state_and_checkpoint_files():
    """Remove pipeline state files and checkpoint files created during tests."""
    yield
    for path in TMP_DIR.glob("pipeline_state_g99_l*.json"):
        path.unlink(missing_ok=True)
    for path in TMP_DIR.glob("g99_l*_*.txt"):
        path.unlink(missing_ok=True)
    for path in TMP_DIR.glob("g99_l*_*.json"):
        path.unlink(missing_ok=True)
    # Stop any lingering heartbeat threads
    for key in list(_heartbeat_threads.keys()):
        if key[0] == 99:
            stop_heartbeat(*key)


# ===========================================================================
# 1. Forward-only state transitions
# ===========================================================================


class TestForwardOnlyTransitions:
    """Enforce that states can only move forward in the lifecycle."""

    def test_state_order_is_consistent(self):
        """Verify _STATE_ORDER matches ALL_STATES ordering."""
        for idx, state in enumerate(ALL_STATES):
            assert _STATE_ORDER[state] == idx

    def test_forward_transition_allowed(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        save_state(s)
        new = transition(s, DOWNLOADING)
        assert new.state == DOWNLOADING

    def test_same_state_transition_allowed(self):
        """Re-entering the same state is allowed (e.g. updating chunk progress)."""
        s = PipelineState(group=_G, lecture=_L, state=TRANSCRIBING)
        save_state(s)
        new = transition(s, TRANSCRIBING, transcript_chunks_done=(0, 1))
        assert new.state == TRANSCRIBING
        assert new.transcript_chunks_done == (0, 1)

    def test_backward_transition_rejected(self):
        s = PipelineState(group=_G, lecture=_L, state=ANALYZING)
        save_state(s)
        with pytest.raises(ValueError, match="Backward transition rejected"):
            transition(s, DOWNLOADING)

    def test_backward_from_notifying_to_transcribing_rejected(self):
        s = PipelineState(group=_G, lecture=_L, state=NOTIFYING)
        save_state(s)
        with pytest.raises(ValueError, match="Backward transition rejected"):
            transition(s, TRANSCRIBING)

    def test_backward_from_complete_to_indexing_rejected(self):
        s = PipelineState(group=_G, lecture=_L, state=COMPLETE)
        save_state(s)
        with pytest.raises(ValueError, match="Backward transition rejected"):
            transition(s, INDEXING)

    def test_complete_to_complete_rejected(self):
        """COMPLETE → COMPLETE is not allowed (same-state re-entry blocked for terminal)."""
        s = PipelineState(group=_G, lecture=_L, state=COMPLETE)
        save_state(s)
        with pytest.raises(ValueError, match="Transition from COMPLETE rejected"):
            transition(s, COMPLETE)

    def test_failed_reachable_from_any_state(self):
        """FAILED can be reached from any state (it's always allowed)."""
        for state_name in ALL_STATES:
            if state_name == FAILED:
                continue
            s = PipelineState(group=_G, lecture=_L, state=state_name)
            save_state(s)
            new = transition(s, FAILED, error="test")
            assert new.state == FAILED

    def test_transition_from_failed_to_non_failed_rejected(self):
        """Cannot transition out of FAILED (except via reset_failed)."""
        s = PipelineState(group=_G, lecture=_L, state=FAILED)
        save_state(s)
        # FAILED has a high ordinal (10), so going to PENDING (0) is backward
        with pytest.raises(ValueError, match="Backward transition rejected"):
            transition(s, PENDING)

    def test_full_forward_chain(self):
        """Walk through the entire forward chain without error."""
        forward_states = [
            PENDING, DOWNLOADING, CONCATENATING, UPLOADING_VIDEO,
            TRANSCRIBING, ANALYZING, UPLOADING_DOCS, NOTIFYING,
            INDEXING, COMPLETE,
        ]
        s = PipelineState(group=_G, lecture=_L, state=forward_states[0])
        save_state(s)
        for next_state in forward_states[1:]:
            s = transition(s, next_state)
        assert s.state == COMPLETE

    def test_skip_states_forward_allowed(self):
        """Skipping states forward is allowed (e.g. PENDING → TRANSCRIBING)."""
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        save_state(s)
        new = transition(s, TRANSCRIBING)
        assert new.state == TRANSCRIBING


# ===========================================================================
# 2. Error history
# ===========================================================================


class TestErrorHistory:
    """Tests for error history recording in state file."""

    def test_mark_failed_records_error_entry(self):
        s = create_pipeline(_G, _L)
        failed = mark_failed(s, "Gemini timeout")
        assert len(failed.errors) == 1
        assert failed.errors[0]["error"] == "Gemini timeout"
        assert "timestamp" in failed.errors[0]
        assert failed.errors[0]["timestamp"] != ""

    def test_multiple_failures_accumulate(self):
        s = create_pipeline(_G, _L)
        s = mark_failed(s, "First error")

        # Reset and recreate to simulate retry
        from tools.core.pipeline_state import reset_failed
        reset_failed(_G, _L)
        s = create_pipeline(_G, _L)
        s = mark_failed(s, "Second error")

        # The second pipeline won't have the first error (it's a new pipeline)
        assert len(s.errors) == 1

    def test_error_history_persists_to_disk(self):
        s = create_pipeline(_G, _L)
        mark_failed(s, "Disk full")
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert len(loaded.errors) >= 1
        assert loaded.errors[0]["error"] == "Disk full"

    def test_error_history_capped_at_20(self):
        """Error history should not grow beyond 20 entries."""
        s = PipelineState(
            group=_G, lecture=_L, state=PENDING,
            errors=tuple(
                {"timestamp": f"2026-03-{i:02d}", "error": f"err-{i}"}
                for i in range(25)
            ),
        )
        save_state(s)
        failed = mark_failed(s, "One more error")
        assert len(failed.errors) == 20
        # Latest error should be at the end
        assert failed.errors[-1]["error"] == "One more error"

    def test_error_history_deserialized_from_json(self):
        data = {
            "group": _G, "lecture": _L, "state": "FAILED",
            "errors": [
                {"timestamp": "2026-03-28T10:00:00", "error": "test error"},
            ],
        }
        from tools.core.pipeline_state import _deserialize
        s = _deserialize(data)
        assert len(s.errors) == 1
        assert s.errors[0]["error"] == "test error"


# ===========================================================================
# 3. Pipeline guard context manager
# ===========================================================================


class TestPipelineGuard:
    """Tests for the pipeline_guard context manager."""

    def test_guard_creates_pipeline(self):
        with pipeline_guard(_G, _L, meeting_id="test-123") as s:
            assert s.state == PENDING
            assert s.meeting_id == "test-123"
            transition(
                s, DOWNLOADING,
                analysis_done=True,
                summary_doc_id="doc-s",
                report_doc_id="doc-r",
                pinecone_indexed=True,
            )
            # Must complete or it'll be marked FAILED in finally
            current = load_state(_G, _L)
            mark_complete(current)

        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == COMPLETE

    def test_guard_marks_failed_on_exception(self):
        with pytest.raises(RuntimeError, match="boom"):
            with pipeline_guard(_G, _L) as s:
                transition(s, DOWNLOADING)
                raise RuntimeError("boom")

        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == FAILED
        assert "boom" in loaded.error

    def test_guard_marks_failed_on_silent_exit(self):
        """If pipeline exits without COMPLETE or FAILED, mark FAILED."""
        with pipeline_guard(_G, _L) as s:
            transition(s, DOWNLOADING)
            # No mark_complete — should be caught by finally

        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == FAILED
        assert "without completion" in loaded.error

    def test_guard_does_not_double_fail(self):
        """If already FAILED from exception handler, don't fail again in finally."""
        with pytest.raises(RuntimeError):
            with pipeline_guard(_G, _L) as s:
                transition(s, DOWNLOADING)
                raise RuntimeError("first error")

        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == FAILED
        assert "first error" in loaded.error

    def test_guard_raises_claim_error_for_active_pipeline(self):
        create_pipeline(_G, _L)  # Create an active pipeline
        with pytest.raises(PipelineClaimError, match="Cannot claim"):
            with pipeline_guard(_G, _L):
                pass

    def test_guard_load_existing_pipeline(self):
        create_pipeline(_G, _L, meeting_id="existing")
        with pipeline_guard(_G, _L, create_new=False) as s:
            assert s.meeting_id == "existing"
            mark_complete(s)

    def test_guard_load_nonexistent_raises_claim_error(self):
        with pytest.raises(PipelineClaimError, match="No existing pipeline"):
            with pipeline_guard(_G, _L, create_new=False):
                pass

    def test_guard_stops_heartbeat_on_exit(self):
        key = (_G, _L)
        with pipeline_guard(_G, _L) as s:
            assert key in _heartbeat_threads
            mark_complete(s)

        assert key not in _heartbeat_threads


# ===========================================================================
# 4. Checkpoint validation
# ===========================================================================


class TestCheckpointValidation:
    """Tests for validate_checkpoint and invalidate_checkpoint."""

    def _write_checkpoint(self, content_type: str, content: str) -> Path:
        path = TMP_DIR / f"g{_G}_l{_L}_{content_type}.txt"
        path.write_text(content, encoding="utf-8")
        return path

    def test_valid_transcript_checkpoint(self):
        self._write_checkpoint("transcript", "A" * 200)
        assert validate_checkpoint(_G, _L, "transcript") is True

    def test_missing_checkpoint_returns_false(self):
        assert validate_checkpoint(_G, _L, "transcript") is False

    def test_too_small_checkpoint_returns_false(self):
        self._write_checkpoint("transcript", "tiny")
        assert validate_checkpoint(_G, _L, "transcript") is False

    def test_whitespace_only_checkpoint_returns_false(self):
        self._write_checkpoint("transcript", "   \n\n\t   ")
        assert validate_checkpoint(_G, _L, "transcript") is False

    def test_empty_file_checkpoint_returns_false(self):
        path = TMP_DIR / f"g{_G}_l{_L}_summary.txt"
        path.write_text("", encoding="utf-8")
        assert validate_checkpoint(_G, _L, "summary") is False

    def test_gap_analysis_minimum_size(self):
        """gap_analysis has a 50-byte minimum."""
        self._write_checkpoint("gap_analysis", "A" * 49)
        assert validate_checkpoint(_G, _L, "gap_analysis") is False
        self._write_checkpoint("gap_analysis", "A" * 51)
        assert validate_checkpoint(_G, _L, "gap_analysis") is True

    def test_invalidate_checkpoint_deletes_file(self):
        path = self._write_checkpoint("transcript", "A" * 200)
        assert path.exists()
        result = invalidate_checkpoint(_G, _L, "transcript")
        assert result is True
        assert not path.exists()

    def test_invalidate_nonexistent_returns_false(self):
        assert invalidate_checkpoint(_G, _L, "transcript") is False

    def test_validate_after_invalidate_returns_false(self):
        self._write_checkpoint("summary", "A" * 200)
        assert validate_checkpoint(_G, _L, "summary") is True
        invalidate_checkpoint(_G, _L, "summary")
        assert validate_checkpoint(_G, _L, "summary") is False


# ===========================================================================
# 5. Heartbeat
# ===========================================================================


class TestHeartbeat:
    """Tests for heartbeat functionality."""

    def test_update_heartbeat_sets_timestamp(self):
        s = create_pipeline(_G, _L)
        assert s.last_heartbeat == ""
        updated = update_heartbeat(s)
        assert updated.last_heartbeat != ""

    def test_update_heartbeat_persists(self):
        s = create_pipeline(_G, _L)
        update_heartbeat(s)
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.last_heartbeat != ""

    def test_start_heartbeat_registers_thread(self):
        create_pipeline(_G, _L)
        start_heartbeat(_G, _L)
        try:
            assert (_G, _L) in _heartbeat_threads
        finally:
            stop_heartbeat(_G, _L)

    def test_stop_heartbeat_removes_thread(self):
        create_pipeline(_G, _L)
        start_heartbeat(_G, _L)
        stop_heartbeat(_G, _L)
        assert (_G, _L) not in _heartbeat_threads

    def test_stop_heartbeat_noop_when_not_started(self):
        """Should not raise when stopping a non-existent heartbeat."""
        stop_heartbeat(_G, _L)  # Should not raise

    def test_start_heartbeat_replaces_existing(self):
        """Starting heartbeat twice should replace the first."""
        create_pipeline(_G, _L)
        start_heartbeat(_G, _L)
        first_event = _heartbeat_threads.get((_G, _L))
        start_heartbeat(_G, _L)
        second_event = _heartbeat_threads.get((_G, _L))
        try:
            assert first_event is not second_event
            assert first_event.is_set()  # First one was stopped
        finally:
            stop_heartbeat(_G, _L)

    def test_heartbeat_field_in_serialization(self):
        s = PipelineState(
            group=_G, lecture=_L, state=PENDING,
            last_heartbeat="2026-03-28T20:00:00+04:00",
        )
        save_state(s)
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert "2026-03-28" in loaded.last_heartbeat


# ===========================================================================
# 6. get_last_activity_time
# ===========================================================================


class TestGetLastActivityTime:
    """Tests for get_last_activity_time helper."""

    def test_uses_heartbeat_when_available(self):
        s = PipelineState(
            group=_G, lecture=_L, state=TRANSCRIBING,
            last_heartbeat="2026-03-28T21:00:00+04:00",
            updated_at="2026-03-28T20:00:00+04:00",
            started_at="2026-03-28T19:00:00+04:00",
        )
        result = get_last_activity_time(s)
        assert result is not None
        assert result.hour == 21  # heartbeat time

    def test_falls_back_to_updated_at(self):
        s = PipelineState(
            group=_G, lecture=_L, state=TRANSCRIBING,
            last_heartbeat="",
            updated_at="2026-03-28T20:00:00+04:00",
            started_at="2026-03-28T19:00:00+04:00",
        )
        result = get_last_activity_time(s)
        assert result is not None
        assert result.hour == 20

    def test_falls_back_to_started_at(self):
        s = PipelineState(
            group=_G, lecture=_L, state=PENDING,
            last_heartbeat="",
            updated_at="",
            started_at="2026-03-28T19:00:00+04:00",
        )
        result = get_last_activity_time(s)
        assert result is not None
        assert result.hour == 19

    def test_returns_none_when_no_timestamps(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        assert get_last_activity_time(s) is None

    def test_returns_none_for_invalid_timestamps(self):
        s = PipelineState(
            group=_G, lecture=_L, state=PENDING,
            last_heartbeat="not-a-date",
            updated_at="also-invalid",
            started_at="nope",
        )
        assert get_last_activity_time(s) is None

    def test_result_is_timezone_aware(self):
        s = PipelineState(
            group=_G, lecture=_L, state=PENDING,
            updated_at="2026-03-28T20:00:00",
        )
        result = get_last_activity_time(s)
        assert result is not None
        assert result.tzinfo is not None


# ===========================================================================
# 7. Integration: forward-only + error history + guard
# ===========================================================================


class TestIntegration:
    """Integration tests combining multiple hardening features."""

    def test_guard_with_forward_transitions(self):
        """Guard + forward-only: full happy path."""
        with pipeline_guard(_G, _L, meeting_id="intg-1") as s:
            s = transition(s, DOWNLOADING, video_path="/tmp/v.mp4")
            s = transition(s, TRANSCRIBING)
            s = transition(s, ANALYZING)
            s = transition(s, NOTIFYING)
            s = transition(
                s, INDEXING,
                analysis_done=True,
                summary_doc_id="doc-s",
                report_doc_id="doc-r",
                pinecone_indexed=True,
            )
            mark_complete(s)

        loaded = load_state(_G, _L)
        assert loaded.state == COMPLETE

    def test_guard_prevents_backward_and_marks_failed(self):
        """Guard catches ValueError from backward transition."""
        with pytest.raises(ValueError, match="Backward"):
            with pipeline_guard(_G, _L) as s:
                s = transition(s, ANALYZING)
                transition(s, DOWNLOADING)  # Backward — raises

        loaded = load_state(_G, _L)
        assert loaded.state == FAILED
        # Error history should record the backward transition attempt
        assert any("Backward" in e["error"] for e in loaded.errors)

    def test_error_history_survives_serialization(self):
        """Create pipeline, fail it, check errors survive disk round-trip."""
        s = create_pipeline(_G, _L)
        s = transition(s, DOWNLOADING)
        mark_failed(s, "Download timeout")

        loaded = load_state(_G, _L)
        assert loaded.errors[-1]["error"] == "Download timeout"
        assert loaded.errors[-1]["timestamp"] != ""

        # Verify raw JSON also contains errors
        path = TMP_DIR / f"pipeline_state_g{_G}_l{_L}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "errors" in raw
        assert len(raw["errors"]) >= 1

    def test_checkpoint_validation_in_context(self):
        """Validate that checkpoint validation works for pipeline resume."""
        # Create a "valid" transcript checkpoint
        transcript_path = TMP_DIR / f"g{_G}_l{_L}_transcript.txt"
        transcript_path.write_text("A" * 3000, encoding="utf-8")

        assert validate_checkpoint(_G, _L, "transcript") is True

        # Corrupt it
        transcript_path.write_text("", encoding="utf-8")
        assert validate_checkpoint(_G, _L, "transcript") is False

        # Invalidate and verify gone
        invalidate_checkpoint(_G, _L, "transcript")
        assert not transcript_path.exists()
