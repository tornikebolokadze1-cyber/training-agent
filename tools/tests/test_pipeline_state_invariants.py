"""Regression tests for ``_COMPLETION_INVARIANTS`` enforcement.

History
-------
US-012 originally pinned every artifact — including ``drive_video_id`` —
as a HARD invariant on ``mark_complete()``.  On 2026-05-20 we hit a
catastrophic retry loop in production: pipelines that delivered the
summary Doc + analysis Doc + Pinecone vectors + group WhatsApp message,
but failed to upload the .mp4 to Drive, were being refused COMPLETE and
retried every 30 minutes by the nightly catch-all.  Each retry re-ran
the full pipeline (transcription + analysis + duplicate WhatsApp blast)
and cost ~$3 in API tokens.  Students received up to 3 duplicate
WhatsApp notifications per lecture.

The fix split the invariant tuple into HARD and SOFT:

- HARD (block COMPLETE → FAIL): analysis_done, summary_doc_id,
  report_doc_id, pinecone_indexed.
- SOFT (warn only, still COMPLETE): drive_video_id.

These tests pin the new contract:

- ``drive_video_id`` remains listed in ``_COMPLETION_INVARIANTS`` (so
  operators grepping the source can find the label) but is in
  ``_SOFT_COMPLETION_INVARIANTS``.
- ``mark_complete()`` succeeds with COMPLETE when ``drive_video_id`` is
  empty AND all HARD invariants hold.
- ``mark_complete()`` still refuses to COMPLETE when any HARD invariant
  is missing (e.g. ``summary_doc_id`` or ``pinecone_indexed``).
- The diagnostic FAIL message names the missing HARD field literally so
  an operator can grep logs.

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
    _HARD_COMPLETION_INVARIANTS,
    _SOFT_COMPLETION_INVARIANTS,
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


def _state_with_hard_invariants_satisfied(*, drive_video_id: str = ""):
    """Build a pipeline that satisfies every HARD invariant.

    By default ``drive_video_id`` is left empty so callers can verify the
    SOFT-only path.  Pass a non-empty string to also satisfy the SOFT
    invariant.
    """
    s = create_pipeline(_G, _L, meeting_id="test-meeting")
    s = transition(
        s,
        INDEXING,
        drive_video_id=drive_video_id,
        analysis_done=True,
        summary_doc_id="summary-doc-id",
        report_doc_id="report-doc-id",
        pinecone_indexed=True,
    )
    return s


# ---------------------------------------------------------------------------
# Invariant catalog: drive_video_id is tracked but soft.
# ---------------------------------------------------------------------------


def test_drive_video_id_is_in_completion_invariants():
    """``drive_video_id`` must remain in the combined invariant catalog
    so operators grepping the source can find the diagnostic label.
    """
    invariant_fields = {field_name for field_name, _label in _COMPLETION_INVARIANTS}
    assert "drive_video_id" in invariant_fields, (
        "drive_video_id must remain in _COMPLETION_INVARIANTS (HARD ∪ SOFT) "
        "so operators can locate the diagnostic label."
    )


def test_drive_video_id_is_soft_not_hard():
    """``drive_video_id`` must be in the SOFT tier, not HARD — otherwise
    we re-introduce the 2026-05-20 catastrophic retry loop where missing
    Drive uploads blocked COMPLETE and triggered duplicate WhatsApp blasts.
    """
    hard_fields = {field_name for field_name, _label in _HARD_COMPLETION_INVARIANTS}
    soft_fields = {field_name for field_name, _label in _SOFT_COMPLETION_INVARIANTS}
    assert "drive_video_id" not in hard_fields, (
        "drive_video_id must NOT be a HARD invariant — that would block "
        "COMPLETE and trigger the duplicate-WhatsApp retry loop."
    )
    assert "drive_video_id" in soft_fields, (
        "drive_video_id must be a SOFT invariant so operators are warned "
        "but pipelines still finalize."
    )


def test_hard_invariants_cover_delivery_critical_artifacts():
    """The HARD invariant set must include the artifacts whose absence
    means students were not actually served (no summary Doc, no analysis,
    no Pinecone index for the assistant).
    """
    hard_fields = {field_name for field_name, _label in _HARD_COMPLETION_INVARIANTS}
    for required in (
        "analysis_done",
        "summary_doc_id",
        "report_doc_id",
        "pinecone_indexed",
    ):
        assert required in hard_fields, (
            f"{required!r} must be a HARD completion invariant"
        )


# ---------------------------------------------------------------------------
# Behavior: SOFT-only failures still COMPLETE.
# ---------------------------------------------------------------------------


def test_complete_transition_accepts_empty_drive_video_id():
    """The 2026-05-20 fix: when only ``drive_video_id`` is missing but
    every HARD invariant holds (docs uploaded, Pinecone indexed),
    ``mark_complete()`` MUST transition to COMPLETE.

    Refusing here would resurrect the duplicate-WhatsApp retry loop.
    """
    s = _state_with_hard_invariants_satisfied(drive_video_id="")
    assert s.drive_video_id == "", "Precondition: drive_video_id starts empty"

    result = mark_complete(s)

    assert result.state == COMPLETE, (
        f"Expected COMPLETE (only SOFT invariant missing), got {result.state}. "
        "An empty drive_video_id MUST NOT block COMPLETE — otherwise "
        "students receive duplicate WhatsApp blasts on every retry."
    )
    assert result.state != FAILED


def test_complete_with_soft_violation_logs_warning(caplog):
    """A SOFT-only violation must log a warning so operators can chase
    the missing artifact out-of-band, even though COMPLETE proceeds.
    """
    import logging

    s = _state_with_hard_invariants_satisfied(drive_video_id="")

    with caplog.at_level(logging.WARNING, logger="tools.core.pipeline_state"):
        result = mark_complete(s)

    assert result.state == COMPLETE
    assert any(
        "drive_video_id" in record.message
        for record in caplog.records
        if record.levelno == logging.WARNING
    ), "Expected a WARNING log mentioning drive_video_id, got: " + ", ".join(
        r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Behavior: HARD failures still coerce to FAILED.
# ---------------------------------------------------------------------------


def test_complete_transition_rejects_missing_summary_doc():
    """A missing HARD invariant (here: summary_doc_id) must still coerce
    the pipeline to FAILED — the original US-012 contract for delivery-
    critical artifacts is unchanged.
    """
    s = create_pipeline(_G, _L, meeting_id="test-meeting")
    s = transition(
        s,
        INDEXING,
        drive_video_id="drive-vid-xyz",
        analysis_done=True,
        # summary_doc_id intentionally omitted
        report_doc_id="report-doc-id",
        pinecone_indexed=True,
    )

    result = mark_complete(s)

    assert result.state == FAILED
    assert "summary" in result.error.lower()


def test_complete_transition_rejects_missing_pinecone():
    """A pipeline without Pinecone vectors must FAIL — the assistant
    cannot answer student questions without the indexed transcript.
    """
    s = create_pipeline(_G, _L, meeting_id="test-meeting")
    s = transition(
        s,
        INDEXING,
        drive_video_id="drive-vid-xyz",
        analysis_done=True,
        summary_doc_id="summary-doc-id",
        report_doc_id="report-doc-id",
        pinecone_indexed=False,
    )

    result = mark_complete(s)

    assert result.state == FAILED
    assert "pinecone" in result.error.lower()


def test_complete_transition_accepts_all_invariants_satisfied():
    """``mark_complete()`` succeeds when every HARD invariant AND the
    SOFT ``drive_video_id`` are satisfied — the happy path is unchanged.
    """
    s = _state_with_hard_invariants_satisfied(drive_video_id="drive-vid-xyz")

    result = mark_complete(s)

    assert result.state == COMPLETE
    assert result.drive_video_id == "drive-vid-xyz"


def test_invariant_message_format_mentions_missing_hard_field():
    """The FAILED error message must name the missing HARD field so an
    operator scanning logs can identify exactly what failed.
    """
    s = create_pipeline(_G, _L, meeting_id="test-meeting")
    s = transition(
        s,
        INDEXING,
        drive_video_id="drive-vid-xyz",
        analysis_done=False,  # <- the missing field
        summary_doc_id="summary-doc-id",
        report_doc_id="report-doc-id",
        pinecone_indexed=True,
    )

    result = mark_complete(s)

    assert result.state == FAILED
    assert "analysis" in result.error.lower(), (
        "Operator diagnostic: the error message must mention the missing "
        f"HARD artifact label. Got: {result.error!r}"
    )
