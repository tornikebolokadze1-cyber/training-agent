"""Regression tests for ``_COMPLETION_INVARIANTS`` enforcement (US-012).

The ``drive_video_id`` field existed on the PipelineState dataclass but was
missing from ``_COMPLETION_INVARIANTS``, so a pipeline could reach COMPLETE
with an empty ``drive_video_id`` — the silent partial-completion failure
mode that hit G1 L7 on 2026-04-03 and is documented in the docstring of
``_COMPLETION_INVARIANTS`` itself.

These tests pin the contract:

- ``mark_complete()`` refuses to transition to COMPLETE when
  ``drive_video_id`` is empty; it coerces the pipeline to FAILED with a
  diagnostic message naming the field.
- ``mark_complete()`` succeeds when all invariants (including
  ``drive_video_id``) are satisfied.
- The diagnostic message mentions ``drive_video_id`` literally so an
  operator can grep logs and identify the missing artifact.

Run with::

    pytest tools/tests/test_pipeline_state_invariants.py -v
"""

from __future__ import annotations

import pytest

from tools.core.config import TMP_DIR
from tools.core.pipeline_state import (
    COMPLETE,
    FAILED,
    INDEXING,
    _COMPLETION_INVARIANTS,
    create_pipeline,
    mark_complete,
    transition,
)

# High-numbered test pipeline coordinates to avoid collisions with real state.
_G = 87
_L = 87


@pytest.fixture(autouse=True)
def _cleanup_state_files():
    """Remove any pipeline state files created by these tests."""
    yield
    for path in TMP_DIR.glob(f"pipeline_state_g{_G}_l*.json"):
        path.unlink(missing_ok=True)


def _state_with_other_invariants_satisfied():
    """Build a pipeline that satisfies every COMPLETE invariant EXCEPT
    ``drive_video_id``.  Returns the state in INDEXING.
    """
    s = create_pipeline(_G, _L, meeting_id="test-meeting")
    s = transition(
        s,
        INDEXING,
        # drive_video_id intentionally omitted — this is the regression target.
        analysis_done=True,
        summary_doc_id="summary-doc-id",
        report_doc_id="report-doc-id",
        pinecone_indexed=True,
    )
    return s


def test_drive_video_id_is_in_completion_invariants():
    """The invariant tuple must list drive_video_id as a required field."""
    invariant_fields = {field_name for field_name, _label in _COMPLETION_INVARIANTS}
    assert "drive_video_id" in invariant_fields, (
        "drive_video_id must be part of _COMPLETION_INVARIANTS so empty "
        "Drive uploads cannot pass the COMPLETE gate."
    )


def test_complete_transition_rejects_empty_drive_video_id():
    """mark_complete() must refuse to mark COMPLETE when drive_video_id is empty.

    The G1 L7 2026-04-03 failure mode: analysis + Pinecone + docs all
    succeeded, but the Drive video upload silently failed.  The pipeline
    should be coerced to FAILED instead of COMPLETE so the nightly
    catch-all can retry it.
    """
    s = _state_with_other_invariants_satisfied()
    assert s.drive_video_id == "", "Precondition: drive_video_id starts empty"

    result = mark_complete(s)

    assert result.state == FAILED, (
        f"Expected FAILED (invariant violation), got {result.state}. "
        "mark_complete() should coerce to FAILED when drive_video_id is empty."
    )
    assert result.state != COMPLETE


def test_complete_transition_accepts_with_drive_video_id():
    """mark_complete() succeeds when every invariant is satisfied,
    including ``drive_video_id``.
    """
    s = create_pipeline(_G, _L, meeting_id="test-meeting")
    s = transition(
        s,
        INDEXING,
        drive_video_id="drive-vid-xyz",
        analysis_done=True,
        summary_doc_id="summary-doc-id",
        report_doc_id="report-doc-id",
        pinecone_indexed=True,
    )

    result = mark_complete(s)

    assert result.state == COMPLETE, (
        f"Expected COMPLETE (all invariants satisfied), got {result.state}."
    )
    assert result.drive_video_id == "drive-vid-xyz"


def test_invariant_message_format_mentions_drive_video_id():
    """The FAILED error message must mention the missing field by name so an
    operator scanning logs can identify exactly what artifact was missing.
    """
    s = _state_with_other_invariants_satisfied()

    result = mark_complete(s)

    assert result.state == FAILED
    assert "drive_video_id" in result.error, (
        "Operator diagnostic: the error message must literally contain "
        f"'drive_video_id'. Got: {result.error!r}"
    )
