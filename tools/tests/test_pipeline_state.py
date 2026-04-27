"""Comprehensive tests for tools/core/pipeline_state.py.

Covers:
- PipelineState dataclass construction and immutability
- State file path generation
- Atomic write (write-then-rename)
- Serialize / deserialize round-trip (including tuple<->list conversion)
- save_state / load_state CRUD
- State transitions (valid, invalid, with field updates)
- create_pipeline (happy path, duplicate rejection)
- mark_failed / mark_complete convenience constructors
- is_pipeline_active / is_pipeline_done query helpers
- list_active_pipelines / list_all_pipelines directory scan
- cleanup_completed (age-based, FAILED retained)
- Edge cases: corrupt JSON, missing fields, concurrent state files

Run with:
    pytest tools/tests/test_pipeline_state.py -v
"""

from __future__ import annotations

import json
from datetime import datetime
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
    PipelineState,
    _deserialize,
    _serialize,
    atomic_write,
    cleanup_completed,
    create_pipeline,
    is_pipeline_active,
    is_pipeline_done,
    list_active_pipelines,
    list_all_pipelines,
    load_state,
    mark_complete,
    mark_failed,
    save_state,
    state_file_path,
    transition,
)

# ---------------------------------------------------------------------------
# Test group/lecture numbers — use high numbers to avoid collisions
# ---------------------------------------------------------------------------
_G = 88  # Test group
_L = 88  # Test lecture


@pytest.fixture(autouse=True)
def cleanup_state_files():
    """Remove any pipeline state files created during tests."""
    yield
    for path in TMP_DIR.glob("pipeline_state_g88_l*.json"):
        path.unlink(missing_ok=True)
    for path in TMP_DIR.glob("pipeline_state_g77_l*.json"):
        path.unlink(missing_ok=True)
    for path in TMP_DIR.glob("pipeline_state_g99_l*.json"):
        path.unlink(missing_ok=True)
    # Clean up lock file used by try_claim_pipeline tests
    lock_file = TMP_DIR / ".pipeline_lock"
    if lock_file.exists():
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass


# ===========================================================================
# 1. PipelineState dataclass
# ===========================================================================


class TestPipelineStateDataclass:
    """Tests for the PipelineState frozen dataclass."""

    def test_construction_with_required_fields_only(self):
        s = PipelineState(group=1, lecture=3, state=PENDING)
        assert s.group == 1
        assert s.lecture == 3
        assert s.state == PENDING
        assert s.meeting_id == ""
        assert s.transcript_chunks_done == ()
        assert s.cost_estimate_usd == 0.0

    def test_construction_with_all_fields(self):
        s = PipelineState(
            group=2, lecture=5, state=TRANSCRIBING,
            meeting_id="abc123", video_path="/tmp/vid.mp4",
            transcript_chunks_done=(0, 1, 2),
            transcript_total_chunks=4,
            analysis_done=True,
            cost_estimate_usd=5.50,
        )
        assert s.transcript_chunks_done == (0, 1, 2)
        assert s.transcript_total_chunks == 4
        assert s.analysis_done is True
        assert s.cost_estimate_usd == 5.50

    def test_frozen_immutability(self):
        s = PipelineState(group=1, lecture=1, state=PENDING)
        with pytest.raises(AttributeError):
            s.state = DOWNLOADING  # type: ignore[misc]

    def test_equality(self):
        a = PipelineState(group=1, lecture=1, state=PENDING)
        b = PipelineState(group=1, lecture=1, state=PENDING)
        assert a == b

    def test_inequality_on_different_state(self):
        a = PipelineState(group=1, lecture=1, state=PENDING)
        b = PipelineState(group=1, lecture=1, state=DOWNLOADING)
        assert a != b


# ===========================================================================
# 2. State constants
# ===========================================================================


class TestStateConstants:
    """Verify all expected states are defined."""

    def test_all_states_count(self):
        assert len(ALL_STATES) == 11

    def test_pending_is_first(self):
        assert ALL_STATES[0] == PENDING

    def test_complete_and_failed_are_terminal(self):
        assert COMPLETE in ALL_STATES
        assert FAILED in ALL_STATES

    def test_all_states_are_uppercase_strings(self):
        for s in ALL_STATES:
            assert isinstance(s, str)
            assert s == s.upper()


# ===========================================================================
# 3. Path helpers
# ===========================================================================


class TestStateFilePath:
    """Tests for state_file_path()."""

    def test_returns_path_in_tmp_dir(self):
        p = state_file_path(1, 3)
        assert p.parent == TMP_DIR

    def test_filename_format(self):
        p = state_file_path(2, 15)
        assert p.name == "pipeline_state_g2_l15.json"

    def test_returns_path_object(self):
        assert isinstance(state_file_path(1, 1), Path)


# ===========================================================================
# 4. Atomic write
# ===========================================================================


class TestAtomicWrite:
    """Tests for atomic_write()."""

    def test_creates_file_with_content(self):
        path = TMP_DIR / "test_atomic_write.txt"
        try:
            atomic_write(path, "hello world")
            assert path.read_text() == "hello world"
        finally:
            path.unlink(missing_ok=True)

    def test_no_tmp_file_remains_on_success(self):
        path = TMP_DIR / "test_atomic_clean.txt"
        try:
            atomic_write(path, "data")
            assert not path.with_suffix(".tmp").exists()
        finally:
            path.unlink(missing_ok=True)

    def test_overwrites_existing_file(self):
        path = TMP_DIR / "test_atomic_overwrite.txt"
        try:
            atomic_write(path, "first")
            atomic_write(path, "second")
            assert path.read_text() == "second"
        finally:
            path.unlink(missing_ok=True)


# ===========================================================================
# 5. Serialization round-trip
# ===========================================================================


class TestSerialization:
    """Tests for _serialize and _deserialize."""

    def test_round_trip_basic(self):
        original = PipelineState(group=1, lecture=3, state=PENDING)
        json_str = _serialize(original)
        data = json.loads(json_str)
        restored = _deserialize(data)
        assert restored.group == original.group
        assert restored.lecture == original.lecture
        assert restored.state == original.state

    def test_round_trip_with_tuple_field(self):
        original = PipelineState(
            group=2, lecture=5, state=TRANSCRIBING,
            transcript_chunks_done=(0, 1, 2),
        )
        json_str = _serialize(original)
        data = json.loads(json_str)
        # In JSON, tuple becomes a list
        assert isinstance(data["transcript_chunks_done"], list)
        # After deserialization, it should be a tuple again
        restored = _deserialize(data)
        assert isinstance(restored.transcript_chunks_done, tuple)
        assert restored.transcript_chunks_done == (0, 1, 2)

    def test_deserialize_with_missing_fields_uses_defaults(self):
        data = {"group": 1, "lecture": 1, "state": "PENDING"}
        s = _deserialize(data)
        assert s.meeting_id == ""
        assert s.cost_estimate_usd == 0.0
        assert s.transcript_chunks_done == ()

    def test_serialize_produces_valid_json(self):
        s = PipelineState(group=1, lecture=1, state=PENDING)
        json_str = _serialize(s)
        parsed = json.loads(json_str)  # Should not raise
        assert parsed["group"] == 1

    def test_georgian_text_preserved(self):
        """Ensure ensure_ascii=False allows Georgian text."""
        s = PipelineState(group=1, lecture=1, state=FAILED, error="ქართული ტექსტი")
        json_str = _serialize(s)
        assert "ქართული" in json_str
        data = json.loads(json_str)
        restored = _deserialize(data)
        assert restored.error == "ქართული ტექსტი"


# ===========================================================================
# 6. save_state / load_state
# ===========================================================================


class TestSaveLoadState:
    """Tests for save_state and load_state."""

    def test_save_and_load_round_trip(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING, meeting_id="test123")
        save_state(s)
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.group == _G
        assert loaded.lecture == _L
        assert loaded.meeting_id == "test123"

    def test_load_nonexistent_returns_none(self):
        assert load_state(77, 77) is None

    def test_load_corrupt_json_returns_none(self):
        path = state_file_path(77, 77)
        path.write_text("not valid json{{{", encoding="utf-8")
        try:
            assert load_state(77, 77) is None
        finally:
            path.unlink(missing_ok=True)

    def test_save_creates_file_on_disk(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        save_state(s)
        assert state_file_path(_G, _L).exists()

    def test_save_overwrites_previous(self):
        s1 = PipelineState(group=_G, lecture=_L, state=PENDING)
        save_state(s1)
        s2 = PipelineState(group=_G, lecture=_L, state=DOWNLOADING, video_path="/tmp/v.mp4")
        save_state(s2)
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == DOWNLOADING
        assert loaded.video_path == "/tmp/v.mp4"

    def test_save_sets_updated_at_timestamp(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        save_state(s)
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.updated_at != ""
        # Should be parseable as ISO datetime
        datetime.fromisoformat(loaded.updated_at)


# ===========================================================================
# 7. State transitions
# ===========================================================================


class TestTransition:
    """Tests for transition()."""

    def test_basic_transition(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING, started_at="2026-01-01")
        new = transition(s, DOWNLOADING)
        assert new.state == DOWNLOADING
        assert new.group == _G  # unchanged
        assert new.started_at == "2026-01-01"  # preserved

    def test_transition_with_field_updates(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        new = transition(s, DOWNLOADING, video_path="/tmp/test.mp4", meeting_id="m123")
        assert new.state == DOWNLOADING
        assert new.video_path == "/tmp/test.mp4"
        assert new.meeting_id == "m123"

    def test_transition_persists_to_disk(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        save_state(s)
        transition(s, DOWNLOADING)
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == DOWNLOADING

    def test_transition_updates_updated_at(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING, updated_at="old")
        new = transition(s, DOWNLOADING)
        assert new.updated_at != "old"

    def test_transition_to_invalid_state_raises(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        with pytest.raises(ValueError, match="Unknown pipeline state"):
            transition(s, "NONEXISTENT_STATE")

    def test_transition_preserves_immutability(self):
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        new = transition(s, DOWNLOADING)
        assert s.state == PENDING  # original unchanged
        assert new.state == DOWNLOADING

    def test_transition_with_list_to_tuple_conversion(self):
        s = PipelineState(group=_G, lecture=_L, state=TRANSCRIBING)
        new = transition(s, TRANSCRIBING, transcript_chunks_done=[0, 1, 2])
        assert isinstance(new.transcript_chunks_done, tuple)
        assert new.transcript_chunks_done == (0, 1, 2)


# ===========================================================================
# 8. create_pipeline
# ===========================================================================


class TestCreatePipeline:
    """Tests for create_pipeline()."""

    def test_creates_pending_state(self):
        s = create_pipeline(_G, _L)
        assert s.state == PENDING
        assert s.group == _G
        assert s.lecture == _L

    def test_sets_meeting_id(self):
        s = create_pipeline(_G, _L, meeting_id="zoom-123")
        assert s.meeting_id == "zoom-123"

    def test_persists_to_disk(self):
        create_pipeline(_G, _L)
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == PENDING

    def test_sets_started_at(self):
        s = create_pipeline(_G, _L)
        assert s.started_at != ""
        datetime.fromisoformat(s.started_at)  # Should be parseable

    def test_duplicate_active_raises_valueerror(self):
        create_pipeline(_G, _L)
        with pytest.raises(ValueError, match="already active"):
            create_pipeline(_G, _L)

    def test_allows_recreation_after_complete(self):
        s = create_pipeline(_G, _L)
        mark_complete(s)
        # Now we should be able to create a new one
        s2 = create_pipeline(_G, _L)
        assert s2.state == PENDING

    def test_allows_recreation_after_failed(self):
        s = create_pipeline(_G, _L)
        mark_failed(s, "test error")
        s2 = create_pipeline(_G, _L)
        assert s2.state == PENDING


# ===========================================================================
# 9. mark_failed / mark_complete
# ===========================================================================


class TestMarkFailedComplete:
    """Tests for mark_failed and mark_complete."""

    def test_mark_complete(self):
        s = create_pipeline(_G, _L)
        # Populate completion-invariant artifacts so mark_complete actually
        # transitions to COMPLETE rather than coercing to FAILED.
        s = transition(
            s, INDEXING,
            analysis_done=True,
            summary_doc_id="doc-summary",
            report_doc_id="doc-report",
            pinecone_indexed=True,
        )
        done = mark_complete(s)
        assert done.state == COMPLETE

    def test_mark_failed_with_error(self):
        s = create_pipeline(_G, _L)
        failed = mark_failed(s, "Gemini timeout")
        assert failed.state == FAILED
        assert failed.error == "Gemini timeout"

    def test_mark_complete_persists(self):
        s = create_pipeline(_G, _L)
        s = transition(
            s, INDEXING,
            analysis_done=True,
            summary_doc_id="doc-summary",
            report_doc_id="doc-report",
            pinecone_indexed=True,
        )
        mark_complete(s)
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == COMPLETE

    def test_mark_failed_persists(self):
        s = create_pipeline(_G, _L)
        mark_failed(s, "out of disk")
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == FAILED
        assert loaded.error == "out of disk"


# ===========================================================================
# 10. Query helpers
# ===========================================================================


class TestQueryHelpers:
    """Tests for is_pipeline_active, is_pipeline_done."""

    def test_active_for_pending_pipeline(self):
        create_pipeline(_G, _L)
        assert is_pipeline_active(_G, _L) is True

    def test_active_for_downloading_pipeline(self):
        s = create_pipeline(_G, _L)
        transition(s, DOWNLOADING)
        assert is_pipeline_active(_G, _L) is True

    def test_not_active_for_complete(self):
        s = create_pipeline(_G, _L)
        mark_complete(s)
        assert is_pipeline_active(_G, _L) is False

    def test_not_active_for_failed(self):
        s = create_pipeline(_G, _L)
        mark_failed(s, "err")
        assert is_pipeline_active(_G, _L) is False

    def test_not_active_for_nonexistent(self):
        assert is_pipeline_active(77, 77) is False

    def test_done_for_complete(self):
        s = create_pipeline(_G, _L)
        s = transition(
            s, INDEXING,
            analysis_done=True,
            summary_doc_id="doc-summary",
            report_doc_id="doc-report",
            pinecone_indexed=True,
        )
        mark_complete(s)
        assert is_pipeline_done(_G, _L) is True

    def test_not_done_for_failed(self):
        s = create_pipeline(_G, _L)
        mark_failed(s, "err")
        assert is_pipeline_done(_G, _L) is False

    def test_not_done_for_active(self):
        create_pipeline(_G, _L)
        assert is_pipeline_done(_G, _L) is False

    def test_not_done_for_nonexistent(self):
        assert is_pipeline_done(77, 77) is False


# ===========================================================================
# 11. List pipelines
# ===========================================================================


class TestListPipelines:
    """Tests for list_active_pipelines and list_all_pipelines."""

    def test_list_all_includes_all_states(self):
        s1 = create_pipeline(_G, 1)
        s2 = create_pipeline(_G, 2)
        mark_complete(s1)
        mark_failed(s2, "err")
        create_pipeline(_G, 3)

        all_states = list_all_pipelines()
        g88 = [s for s in all_states if s.group == _G]
        assert len(g88) == 3

    def test_list_active_excludes_terminal(self):
        s1 = create_pipeline(_G, 1)
        create_pipeline(_G, 2)
        mark_complete(s1)

        active = list_active_pipelines()
        g88 = [s for s in active if s.group == _G]
        assert len(g88) == 1
        assert g88[0].lecture == 2

    def test_list_sorted_by_group_lecture(self):
        create_pipeline(_G, 3)
        create_pipeline(_G, 1)
        create_pipeline(_G, 2)
        all_states = list_all_pipelines()
        g88 = [s for s in all_states if s.group == _G]
        lectures = [s.lecture for s in g88]
        assert lectures == sorted(lectures)

    def test_list_empty_when_no_files(self):
        # No state files for group 77
        active = [s for s in list_active_pipelines() if s.group == 77]
        assert active == []

    def test_list_skips_corrupt_files(self):
        create_pipeline(_G, 1)
        # Create a corrupt state file
        corrupt_path = TMP_DIR / "pipeline_state_g88_l99.json"
        corrupt_path.write_text("INVALID JSON{{", encoding="utf-8")
        try:
            all_states = list_all_pipelines()
            g88 = [s for s in all_states if s.group == _G]
            # Should have the valid one, skip the corrupt one
            assert len(g88) == 1
            assert g88[0].lecture == 1
        finally:
            corrupt_path.unlink(missing_ok=True)


# ===========================================================================
# 12. Cleanup completed
# ===========================================================================


class TestCleanupCompleted:
    """Tests for cleanup_completed()."""

    def test_removes_completed_over_age(self):
        s = create_pipeline(_G, _L)
        s = transition(
            s, INDEXING,
            analysis_done=True,
            summary_doc_id="doc-summary",
            report_doc_id="doc-report",
            pinecone_indexed=True,
        )
        mark_complete(s)
        # With max_age_hours=0, everything qualifies
        deleted = cleanup_completed(max_age_hours=0)
        assert deleted >= 1
        assert load_state(_G, _L) is None

    def test_retains_failed_pipelines(self):
        s = create_pipeline(_G, _L)
        mark_failed(s, "intentional test failure")
        cleanup_completed(max_age_hours=0)
        # FAILED should NOT be deleted
        assert load_state(_G, _L) is not None
        assert load_state(_G, _L).state == FAILED

    def test_retains_active_pipelines(self):
        create_pipeline(_G, _L)
        cleanup_completed(max_age_hours=0)
        # PENDING should NOT be deleted
        assert load_state(_G, _L) is not None

    def test_retains_recent_completed(self):
        s = create_pipeline(_G, _L)
        mark_complete(s)
        # With very high age threshold, nothing should be cleaned
        deleted = cleanup_completed(max_age_hours=9999)
        assert deleted == 0
        assert load_state(_G, _L) is not None


# ===========================================================================
# 13. Full pipeline lifecycle
# ===========================================================================


class TestFullLifecycle:
    """Integration test: walk through the entire state machine."""

    def test_complete_lifecycle(self):
        # Create
        s = create_pipeline(_G, _L, meeting_id="zm-456")
        assert s.state == PENDING

        # Download
        s = transition(s, DOWNLOADING, video_path="/tmp/rec.mp4")
        assert s.state == DOWNLOADING
        assert s.video_path == "/tmp/rec.mp4"

        # Concatenate
        s = transition(s, CONCATENATING)
        assert s.state == CONCATENATING

        # Upload video
        s = transition(s, UPLOADING_VIDEO, drive_video_id="drive-vid-id")
        assert s.drive_video_id == "drive-vid-id"

        # Transcribe (chunk progress)
        s = transition(s, TRANSCRIBING, transcript_total_chunks=3)
        s = transition(s, TRANSCRIBING, transcript_chunks_done=(0,))
        s = transition(s, TRANSCRIBING, transcript_chunks_done=(0, 1))
        s = transition(s, TRANSCRIBING, transcript_chunks_done=(0, 1, 2))
        assert s.transcript_chunks_done == (0, 1, 2)

        # Analyze
        s = transition(s, ANALYZING, analysis_done=True)
        assert s.analysis_done is True

        # Upload docs
        s = transition(s, UPLOADING_DOCS, summary_doc_id="sum-id", report_doc_id="rep-id")

        # Notify
        s = transition(s, NOTIFYING, group_notified=True, private_notified=True)

        # Index
        s = transition(s, INDEXING, pinecone_indexed=True)

        # Complete
        s = mark_complete(s)
        assert s.state == COMPLETE

        # Verify final persisted state
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == COMPLETE
        assert loaded.meeting_id == "zm-456"
        assert loaded.drive_video_id == "drive-vid-id"
        assert loaded.summary_doc_id == "sum-id"
        assert loaded.group_notified is True
        assert loaded.pinecone_indexed is True

    def test_failed_lifecycle(self):
        s = create_pipeline(_G, _L)
        s = transition(s, DOWNLOADING)
        s = mark_failed(s, "Zoom download 404")

        loaded = load_state(_G, _L)
        assert loaded.state == FAILED
        assert loaded.error == "Zoom download 404"

        # Can create a new pipeline for the same group/lecture
        s2 = create_pipeline(_G, _L)
        assert s2.state == PENDING


# ===========================================================================
# 14. try_claim_pipeline
# ===========================================================================


class TestTryClaimPipeline:
    """Tests for try_claim_pipeline() — atomic check-and-create with locking."""

    def test_claim_succeeds_when_no_existing_state(self):
        """Claiming a pipeline with no prior state should succeed."""
        from tools.core.pipeline_state import try_claim_pipeline

        result = try_claim_pipeline(_G, _L, meeting_id="zoom-new")
        assert result is not None
        assert result.state == PENDING
        assert result.meeting_id == "zoom-new"

    def test_claim_returns_none_when_active_pipeline_exists(self):
        """Claiming when an active (non-terminal) pipeline exists returns None."""
        from tools.core.pipeline_state import try_claim_pipeline

        create_pipeline(_G, _L)
        result = try_claim_pipeline(_G, _L, meeting_id="zoom-dup")
        assert result is None

    def test_claim_returns_none_when_complete_pipeline_exists(self):
        """Claiming when a COMPLETE pipeline exists returns None (no re-processing)."""
        from tools.core.pipeline_state import try_claim_pipeline

        s = create_pipeline(_G, _L)
        # Populate all required artifacts so mark_complete actually transitions
        # to COMPLETE — the new completion-invariant gate refuses to mark COMPLETE
        # without these (which is the correct production behavior).
        s = transition(
            s,
            INDEXING,
            analysis_done=True,
            summary_doc_id="doc-summary-test",
            report_doc_id="doc-report-test",
            pinecone_indexed=True,
        )
        mark_complete(s)
        result = try_claim_pipeline(_G, _L, meeting_id="zoom-redo")
        assert result is None

    def test_claim_allows_retry_of_failed_pipeline(self):
        """Claiming when a FAILED pipeline exists should succeed (retry allowed)."""
        from tools.core.pipeline_state import try_claim_pipeline

        s = create_pipeline(_G, _L)
        mark_failed(s, "test error")
        result = try_claim_pipeline(_G, _L, meeting_id="zoom-retry")
        assert result is not None
        assert result.state == PENDING

    def test_concurrent_claims_only_one_succeeds(self):
        """Two threads racing to claim the same pipeline — only one should win."""
        import threading
        from tools.core.pipeline_state import try_claim_pipeline

        results: list[PipelineState | None] = [None, None]

        def claim(index: int) -> None:
            results[index] = try_claim_pipeline(99, 1, meeting_id=f"thread-{index}")

        t0 = threading.Thread(target=claim, args=(0,))
        t1 = threading.Thread(target=claim, args=(1,))
        t0.start()
        t1.start()
        t0.join(timeout=5)
        t1.join(timeout=5)

        # Exactly one should succeed, the other should get None
        successes = [r for r in results if r is not None]
        assert len(successes) == 1, (
            f"Expected exactly 1 successful claim, got {len(successes)}: {results}"
        )
        assert successes[0].state == PENDING


# ===========================================================================
# 15. Forward-only state transitions
# ===========================================================================


class TestForwardOnlyTransitions:
    """Tests for the forward-only constraint in transition()."""

    def test_forward_transition_succeeds(self):
        """PENDING -> TRANSCRIBING (forward) should produce a new state."""
        s = PipelineState(group=_G, lecture=_L, state=PENDING)
        new = transition(s, TRANSCRIBING)
        assert new.state == TRANSCRIBING

    def test_backward_transition_blocked(self):
        """NOTIFYING -> TRANSCRIBING (backward) should raise ValueError.

        The forward-only invariant is now hard-enforced (was previously
        a soft-fail that returned the original state). Hard rejection
        surfaces caller bugs immediately rather than silently swallowing
        the bad transition.
        """
        s = PipelineState(group=_G, lecture=_L, state=NOTIFYING)
        save_state(s)
        with pytest.raises(ValueError, match="Backward transition rejected"):
            transition(s, TRANSCRIBING)
        # State on disk must remain unchanged
        loaded = load_state(_G, _L)
        assert loaded is not None
        assert loaded.state == NOTIFYING

    def test_failed_transition_allowed_from_any_state(self):
        """NOTIFYING -> FAILED should always be allowed (FAILED is special)."""
        s = PipelineState(group=_G, lecture=_L, state=NOTIFYING)
        save_state(s)
        result = transition(s, FAILED, error="test error")
        assert result.state == FAILED
        assert result.error == "test error"

    def test_same_state_transition_allowed(self):
        """TRANSCRIBING -> TRANSCRIBING (same state, e.g. chunk progress) should succeed."""
        s = PipelineState(group=_G, lecture=_L, state=TRANSCRIBING)
        save_state(s)
        result = transition(s, TRANSCRIBING, transcript_chunks_done=(0, 1))
        assert result.state == TRANSCRIBING
        assert result.transcript_chunks_done == (0, 1)
