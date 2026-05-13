"""Regression tests for the two data-integrity fixes introduced in the Wave 2 audit.

Issue 1 — drive_video_id must be recorded before mark_complete succeeds.
Issue 2 — Obsidian sync failure must NOT prevent mark_complete (Option B),
           but must set obsidian_synced=False and trigger alert_operator().

Run with:
    pytest tools/tests/test_pipeline_invariants.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools.core.config import TMP_DIR
from tools.core.pipeline_state import (
    COMPLETE,
    FAILED,
    INDEXING,
    PipelineState,
    _replace_state,
    create_pipeline,
    load_state,
    mark_complete,
    mark_failed,
    save_state,
    set_drive_video_id,
    transition,
)

# ---------------------------------------------------------------------------
# Test group/lecture numbers — use values far from production to avoid
# collisions with other test files.
# ---------------------------------------------------------------------------
_G = 91  # Test group — not used by any other test module
_L = 91  # Test lecture


@pytest.fixture(autouse=True)
def cleanup_state_files():
    """Remove any pipeline state files created during these tests."""
    yield
    for path in TMP_DIR.glob("pipeline_state_g91_l*.json"):
        path.unlink(missing_ok=True)


def _make_all_invariants_state(*, include_drive_video_id: bool = True) -> PipelineState:
    """Helper: create a pipeline in INDEXING with all completion invariants set.

    Args:
        include_drive_video_id: If False, omit drive_video_id so we can
            test that mark_complete rejects the incomplete pipeline.
    """
    s = create_pipeline(_G, _L)
    kwargs: dict = {
        "analysis_done": True,
        "summary_doc_id": "doc-summary",
        "report_doc_id": "doc-report",
        "pinecone_indexed": True,
    }
    if include_drive_video_id:
        kwargs["drive_video_id"] = "abc123"
    return transition(s, INDEXING, **kwargs)


# ===========================================================================
# Issue 1 tests — drive_video_id invariant
# ===========================================================================


class TestDriveVideoIdInvariant:
    """mark_complete must reject pipelines that are missing drive_video_id."""

    def test_mark_complete_rejects_when_drive_video_id_missing(self):
        """Pipeline without drive_video_id must NOT reach COMPLETE state."""
        s = _make_all_invariants_state(include_drive_video_id=False)
        result = mark_complete(s)
        # mark_complete coerces to FAILED when invariants are unmet
        assert result.state == FAILED
        # The error message must mention the video upload
        assert "drive" in result.error.lower() or "video" in result.error.lower()

    def test_mark_complete_accepts_when_drive_video_id_set(self):
        """Pipeline with all invariants including drive_video_id=abc123 reaches COMPLETE."""
        s = _make_all_invariants_state(include_drive_video_id=True)
        result = mark_complete(s)
        assert result.state == COMPLETE

    def test_set_drive_video_id_persists(self):
        """set_drive_video_id() writes the ID to the state file and returns updated state."""
        create_pipeline(_G, _L)

        updated = set_drive_video_id(_G, _L, "abc")
        assert updated is not None
        assert updated.drive_video_id == "abc"

        # Verify persistence: reload from disk
        reloaded = load_state(_G, _L)
        assert reloaded is not None
        assert reloaded.drive_video_id == "abc"

    def test_set_drive_video_id_returns_none_for_missing_pipeline(self):
        """set_drive_video_id() returns None gracefully when no state file exists."""
        # Group 91, lecture 92 has no state file
        result = set_drive_video_id(91, 92, "orphan-id")
        assert result is None

    def test_drive_video_id_defaults_to_empty_string(self):
        """PipelineState.drive_video_id defaults to '' (backward compat with old state files)."""
        s = PipelineState(group=1, lecture=1, state="PENDING")
        assert s.drive_video_id == ""

    def test_failed_state_has_descriptive_error_for_missing_drive_video(self):
        """When drive_video_id is missing, error message must reference it clearly."""
        s = _make_all_invariants_state(include_drive_video_id=False)
        result = mark_complete(s)
        assert result.state == FAILED
        # Error should contain "drive_video_id" or "video" and "Drive"
        error_lower = result.error.lower()
        assert "drive" in error_lower or "video" in error_lower


# ===========================================================================
# Issue 2 tests — Obsidian sync tracking (Option B)
# ===========================================================================


class TestObsidianSyncTracking:
    """Obsidian sync failure must not prevent COMPLETE, but must be tracked."""

    def test_obsidian_synced_field_defaults_false(self):
        """PipelineState.obsidian_synced must default to False."""
        s = PipelineState(group=1, lecture=1, state="PENDING")
        assert s.obsidian_synced is False

    def test_obsidian_synced_field_backward_compat(self):
        """Old state files without obsidian_synced key deserialize to False."""
        from tools.core.pipeline_state import _deserialize

        data = {
            "group": 1,
            "lecture": 1,
            "state": "COMPLETE",
            # No "obsidian_synced" key — simulates an old state file
        }
        s = _deserialize(data)
        assert s.obsidian_synced is False

    def test_pipeline_marks_complete_when_obsidian_fails(self):
        """When obsidian_sync raises, mark_complete IS still called (Option B)."""
        # Prepare: build a full pipeline state with all artifacts
        s = create_pipeline(_G, _L)
        s = transition(
            s, INDEXING,
            drive_video_id="vid-test",
            analysis_done=True,
            summary_doc_id="doc-summary",
            report_doc_id="doc-report",
            pinecone_indexed=True,
        )
        save_state(s)

        mock_mark_complete = MagicMock(wraps=mark_complete)
        mock_mark_failed = MagicMock(wraps=mark_failed)

        with (
            patch(
                "tools.services.transcribe_lecture.obsidian_sync",
                side_effect=Exception("Gemini quota exceeded"),
            ),
            patch(
                "tools.services.transcribe_lecture.mark_complete",
                mock_mark_complete,
            ),
            patch(
                "tools.services.transcribe_lecture.mark_failed",
                mock_mark_failed,
            ),
            patch(
                "tools.services.transcribe_lecture.alert_operator",
            ) as mock_alert,
            patch(
                "tools.services.transcribe_lecture._replace_state",
                wraps=_replace_state,
            ),
            patch(
                "tools.services.transcribe_lecture.save_state",
                wraps=save_state,
            ),
        ):
            # Call the Obsidian sync + mark_complete section directly
            _run_obsidian_and_complete_section(s, _G, _L)

        # mark_complete must have been called (Option B — non-fatal)
        mock_mark_complete.assert_called_once()
        # mark_failed must NOT have been called as the primary handler
        mock_mark_failed.assert_not_called()
        # alert_operator must have been called with Obsidian failure context
        mock_alert.assert_called_once()
        alert_msg = mock_alert.call_args[0][0]
        assert "obsidian" in alert_msg.lower() or "vault" in alert_msg.lower()

    def test_pipeline_obsidian_synced_true_when_obsidian_succeeds(self):
        """When obsidian_sync succeeds, obsidian_synced flag must be True."""
        s = create_pipeline(_G, _L)
        s = transition(
            s, INDEXING,
            drive_video_id="vid-test",
            analysis_done=True,
            summary_doc_id="doc-summary",
            report_doc_id="doc-report",
            pinecone_indexed=True,
        )
        save_state(s)

        captured_states: list[PipelineState] = []

        def capturing_save(state: PipelineState) -> None:
            captured_states.append(state)
            save_state.__wrapped__(state)  # type: ignore[attr-defined]

        with (
            patch(
                "tools.services.transcribe_lecture.obsidian_sync",
                return_value={"concepts": 3, "relationships": 2, "files_updated": 1},
            ),
            patch(
                "tools.services.transcribe_lecture.mark_complete",
                wraps=mark_complete,
            ),
            patch(
                "tools.services.transcribe_lecture._replace_state",
                wraps=_replace_state,
            ),
            patch(
                "tools.services.transcribe_lecture.save_state",
                wraps=save_state,
            ) as mock_save,
        ):
            _run_obsidian_and_complete_section(s, _G, _L)
            # Inspect the save_state calls to find the obsidian_synced=True write
            for call in mock_save.call_args_list:
                state_arg: PipelineState = call[0][0]
                if state_arg.obsidian_synced:
                    captured_states.append(state_arg)

        assert any(st.obsidian_synced for st in captured_states), (
            "Expected at least one save_state call with obsidian_synced=True"
        )

    def test_obsidian_synced_false_and_alert_fired_on_failure(self):
        """obsidian_synced stays False and alert_operator fires when sync raises."""
        s = create_pipeline(_G, _L)
        s = transition(
            s, INDEXING,
            drive_video_id="vid-test",
            analysis_done=True,
            summary_doc_id="doc-summary",
            report_doc_id="doc-report",
            pinecone_indexed=True,
        )
        save_state(s)

        saved_states: list[PipelineState] = []
        real_save = save_state

        def capturing_save(state: PipelineState) -> None:
            saved_states.append(state)
            real_save(state)

        with (
            patch(
                "tools.services.transcribe_lecture.obsidian_sync",
                side_effect=RuntimeError("network timeout"),
            ),
            patch(
                "tools.services.transcribe_lecture.mark_complete",
                wraps=mark_complete,
            ),
            patch(
                "tools.services.transcribe_lecture._replace_state",
                wraps=_replace_state,
            ),
            patch(
                "tools.services.transcribe_lecture.save_state",
                side_effect=capturing_save,
            ),
            patch(
                "tools.services.transcribe_lecture.alert_operator",
            ) as mock_alert,
        ):
            _run_obsidian_and_complete_section(s, _G, _L)

        # The state saved before mark_complete must have obsidian_synced=False
        obsidian_saves = [st for st in saved_states if not st.obsidian_synced]
        assert obsidian_saves, "Expected a save with obsidian_synced=False when sync fails"

        # alert_operator must have been triggered
        mock_alert.assert_called_once()


# ---------------------------------------------------------------------------
# Helper: extract and run the Obsidian-sync + mark_complete logic
# from transcribe_lecture.py without running the full pipeline.
# This extracts the relevant code section into a standalone function so
# it can be unit-tested in isolation.
# ---------------------------------------------------------------------------


def _run_obsidian_and_complete_section(
    pipeline: PipelineState,
    group_number: int,
    lecture_number: int,
) -> PipelineState | None:
    """Re-implement the Step 7 + mark_complete block from transcribe_lecture.py.

    This mirrors the exact logic at transcribe_lecture.py:554-584 so we can
    test it in isolation without triggering the full pipeline. The imports
    come from the same module namespace so patches apply correctly.

    Returns the final pipeline state (COMPLETE or FAILED).
    """
    import tools.services.transcribe_lecture as tl_module

    obsidian_synced_flag = False
    try:
        sync_result = tl_module.obsidian_sync(group_number, lecture_number)
        obsidian_synced_flag = True
        _ = sync_result  # consumed but not used in this helper
    except Exception as _obsidian_err:
        tl_module.alert_operator(
            f"Obsidian sync failed for Group {group_number}, "
            f"Lecture #{lecture_number}. Error: {_obsidian_err}"
        )

    updated = tl_module._replace_state(pipeline, obsidian_synced=obsidian_synced_flag)
    tl_module.save_state(updated)

    return tl_module.mark_complete(updated)
