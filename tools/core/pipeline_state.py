"""Pipeline State Machine for the Training Agent recording pipeline.

Provides atomic, file-backed state management for the full recording
processing lifecycle: download → transcription → analysis → delivery.

Each pipeline instance corresponds to one lecture for one group and is
persisted as a JSON file in TMP_DIR so that crashes and restarts can
resume mid-pipeline without reprocessing completed stages.

Usage::

    state = create_pipeline(group=1, lecture=3, meeting_id="abc123")
    state = transition(state, DOWNLOADING, video_path="/tmp/rec.mp4")
    state = mark_complete(state)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.core.config import TBILISI_TZ, TMP_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

PENDING = "PENDING"
DOWNLOADING = "DOWNLOADING"
CONCATENATING = "CONCATENATING"
UPLOADING_VIDEO = "UPLOADING_VIDEO"
TRANSCRIBING = "TRANSCRIBING"
ANALYZING = "ANALYZING"
UPLOADING_DOCS = "UPLOADING_DOCS"
NOTIFYING = "NOTIFYING"
INDEXING = "INDEXING"
COMPLETE = "COMPLETE"
FAILED = "FAILED"

# States that represent a finished pipeline (no further processing expected).
_TERMINAL_STATES: frozenset[str] = frozenset({COMPLETE, FAILED})

# Ordered list of all valid states — used for validation.
ALL_STATES: tuple[str, ...] = (
    PENDING,
    DOWNLOADING,
    CONCATENATING,
    UPLOADING_VIDEO,
    TRANSCRIBING,
    ANALYZING,
    UPLOADING_DOCS,
    NOTIFYING,
    INDEXING,
    COMPLETE,
    FAILED,
)


# ---------------------------------------------------------------------------
# PipelineState dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineState:
    """Immutable snapshot of a recording pipeline's progress.

    All mutations produce a new instance via ``dataclasses.replace``.
    Tuple fields (e.g. ``transcript_chunks_done``) are used for
    immutability; they serialize as JSON arrays and are restored on load.

    Attributes:
        group: Training group number (1 or 2).
        lecture: Lecture number within the group (1–15).
        state: Current pipeline state string (one of the module-level constants).
        meeting_id: Zoom meeting UUID for this recording session.
        started_at: ISO 8601 timestamp when the pipeline was created.
        updated_at: ISO 8601 timestamp of the most recent state transition.
        video_path: Local filesystem path to the downloaded recording file.
        drive_video_id: Google Drive file ID of the uploaded recording.
        transcript_chunks_done: Indices of transcription chunks completed so far.
        transcript_total_chunks: Total number of transcription chunks expected.
        analysis_done: Whether the Gemini + Claude analysis stage is complete.
        summary_doc_id: Google Docs file ID of the lecture summary document.
        report_doc_id: Google Docs file ID of the private analysis report.
        group_notified: Whether the group WhatsApp notification was sent.
        private_notified: Whether the private (operator) notification was sent.
        pinecone_indexed: Whether lecture content was indexed in Pinecone.
        error: Human-readable error message if the pipeline is in FAILED state.
        retry_count: Number of times the pipeline has been retried after failure.
        cost_estimate_usd: Running estimate of API costs incurred (USD).
    """

    group: int
    lecture: int
    state: str
    meeting_id: str = ""
    started_at: str = ""
    updated_at: str = ""
    video_path: str = ""
    drive_video_id: str = ""
    transcript_chunks_done: tuple[int, ...] = field(default_factory=tuple)
    transcript_total_chunks: int = 0
    analysis_done: bool = False
    summary_doc_id: str = ""
    report_doc_id: str = ""
    group_notified: bool = False
    private_notified: bool = False
    pinecone_indexed: bool = False
    error: str = ""
    retry_count: int = 0
    cost_estimate_usd: float = 0.0


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def state_file_path(group: int, lecture: int) -> Path:
    """Return the canonical path for a pipeline state file.

    Args:
        group: Training group number (1 or 2).
        lecture: Lecture number (1–15).

    Returns:
        Path to ``.tmp/pipeline_state_g{group}_l{lecture}.json``.
    """
    return TMP_DIR / f"pipeline_state_g{group}_l{lecture}.json"


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current Tbilisi time as an ISO 8601 string."""
    return datetime.now(tz=TBILISI_TZ).isoformat()


def atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a sibling temp file.

    Creates a temporary file in the same directory as *path*, writes the
    content, then performs an ``os.rename`` which is atomic on POSIX
    systems.  This prevents partial writes from leaving a corrupt state
    file on disk.

    Args:
        path: Destination file path.
        content: UTF-8 string to write.
    """
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.rename(tmp_path, path)
    except OSError:
        # Clean up orphaned temp file if rename failed.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _serialize(state: PipelineState) -> str:
    """Serialize a PipelineState to a JSON string.

    Tuple fields are stored as JSON arrays; the receiver is responsible
    for converting them back on load.

    Args:
        state: The pipeline state to serialize.

    Returns:
        Formatted JSON string (UTF-8, human-readable indentation).
    """
    data: dict[str, Any] = asdict(state)
    # asdict converts tuples to lists; keep that behaviour — deserialization
    # will convert them back to tuples.
    return json.dumps(data, ensure_ascii=False, indent=2)


def _deserialize(data: dict[str, Any]) -> PipelineState:
    """Construct a PipelineState from a raw JSON-decoded dictionary.

    Missing keys fall back to dataclass defaults, allowing forward
    compatibility when new fields are added to the dataclass.

    Args:
        data: Decoded JSON dictionary.

    Returns:
        Reconstructed PipelineState instance.
    """
    # Convert list → tuple for the immutable sequence field.
    raw_chunks = data.get("transcript_chunks_done", [])
    chunks: tuple[int, ...] = tuple(int(c) for c in raw_chunks)

    return PipelineState(
        group=int(data.get("group", 0)),
        lecture=int(data.get("lecture", 0)),
        state=str(data.get("state", PENDING)),
        meeting_id=str(data.get("meeting_id", "")),
        started_at=str(data.get("started_at", "")),
        updated_at=str(data.get("updated_at", "")),
        video_path=str(data.get("video_path", "")),
        drive_video_id=str(data.get("drive_video_id", "")),
        transcript_chunks_done=chunks,
        transcript_total_chunks=int(data.get("transcript_total_chunks", 0)),
        analysis_done=bool(data.get("analysis_done", False)),
        summary_doc_id=str(data.get("summary_doc_id", "")),
        report_doc_id=str(data.get("report_doc_id", "")),
        group_notified=bool(data.get("group_notified", False)),
        private_notified=bool(data.get("private_notified", False)),
        pinecone_indexed=bool(data.get("pinecone_indexed", False)),
        error=str(data.get("error", "")),
        retry_count=int(data.get("retry_count", 0)),
        cost_estimate_usd=float(data.get("cost_estimate_usd", 0.0)),
    )


# ---------------------------------------------------------------------------
# Core CRUD
# ---------------------------------------------------------------------------


def save_state(state: PipelineState) -> None:
    """Persist a pipeline state to disk atomically.

    The ``updated_at`` timestamp in the serialized JSON always reflects
    the wall-clock time at the moment of the write — the caller does not
    need to set it.  (The in-memory state object retains whatever
    ``updated_at`` was set during the last ``transition`` call.)

    Args:
        state: The pipeline state to persist.
    """
    path = state_file_path(state.group, state.lecture)
    # Stamp the current time into the serialized dict without mutating the
    # frozen dataclass (the caller's in-memory copy already has the right
    # timestamp from transition()).
    data: dict[str, Any] = asdict(state)
    data["updated_at"] = _now_iso()
    content = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write(path, content)
    logger.debug(
        "Saved pipeline state g%d/l%d → %s",
        state.group,
        state.lecture,
        state.state,
    )


def load_state(group: int, lecture: int) -> PipelineState | None:
    """Load a pipeline state from disk.

    Returns ``None`` if the state file does not exist or is corrupt.
    A warning is logged for corrupt files to aid debugging.

    Args:
        group: Training group number (1 or 2).
        lecture: Lecture number (1–15).

    Returns:
        The loaded PipelineState, or None if unavailable.
    """
    path = state_file_path(group, lecture)
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)
        return _deserialize(data)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Corrupt pipeline state file %s — skipping: %s", path, exc
        )
        return None
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Invalid pipeline state data in %s — skipping: %s", path, exc
        )
        return None


# ---------------------------------------------------------------------------
# State machine transitions
# ---------------------------------------------------------------------------


def transition(
    state: PipelineState,
    new_state: str,
    **updates: Any,
) -> PipelineState:
    """Transition a pipeline to a new state, applying optional field updates.

    Produces an immutable new ``PipelineState`` via ``dataclasses.replace``,
    logs the transition, and persists the result to disk.

    Args:
        state: The current pipeline state (will not be mutated).
        new_state: The target state string (one of the module-level constants).
        **updates: Optional field overrides to apply alongside the state change.

    Returns:
        A new PipelineState with ``state`` set to *new_state*, ``updated_at``
        stamped to the current Tbilisi time, and any *updates* applied.

    Raises:
        ValueError: If *new_state* is not a recognised state constant.
    """
    if new_state not in ALL_STATES:
        raise ValueError(
            f"Unknown pipeline state: {new_state!r}. "
            f"Valid states: {ALL_STATES}"
        )

    now = _now_iso()
    new = _replace_state(state, state=new_state, updated_at=now, **updates)  # type: ignore[call-arg]

    logger.info(
        "Pipeline g%d/l%d: %s → %s",
        state.group,
        state.lecture,
        state.state,
        new_state,
    )
    save_state(new)
    return new


def _replace_state(source: PipelineState, **updates: Any) -> PipelineState:
    """Return a new PipelineState with the given fields replaced.

    A thin wrapper around ``dataclasses.replace`` that converts any list
    value for ``transcript_chunks_done`` into a tuple to preserve the
    field's immutable type contract.

    Args:
        source: Source state (frozen dataclass).
        **updates: Fields to override.

    Returns:
        New PipelineState with overrides applied.
    """
    if "transcript_chunks_done" in updates:
        raw = updates["transcript_chunks_done"]
        updates["transcript_chunks_done"] = tuple(raw) if raw is not None else ()

    import dataclasses  # local import to keep module-level namespace clean

    return dataclasses.replace(source, **updates)


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


def create_pipeline(
    group: int,
    lecture: int,
    meeting_id: str = "",
) -> PipelineState:
    """Create and persist a new pipeline in the PENDING state.

    Args:
        group: Training group number (1 or 2).
        lecture: Lecture number (1–15).
        meeting_id: Zoom meeting UUID associated with this recording.

    Returns:
        The newly created PipelineState.

    Raises:
        ValueError: If an active pipeline already exists for this
            group/lecture combination (prevents accidental double-start).
    """
    if is_pipeline_active(group, lecture):
        existing = load_state(group, lecture)
        current_state = existing.state if existing else "unknown"
        raise ValueError(
            f"Pipeline already active for group {group}, lecture {lecture} "
            f"(current state: {current_state}). "
            "Call mark_failed() or wait for completion before creating a new one."
        )

    now = _now_iso()
    state = PipelineState(
        group=group,
        lecture=lecture,
        state=PENDING,
        meeting_id=meeting_id,
        started_at=now,
        updated_at=now,
    )
    save_state(state)
    logger.info(
        "Created pipeline g%d/l%d (meeting_id=%r)",
        group,
        lecture,
        meeting_id,
    )
    return state


def mark_failed(state: PipelineState, error: str) -> PipelineState:
    """Transition a pipeline to the FAILED state with an error message.

    Args:
        state: The current pipeline state.
        error: Human-readable description of what went wrong.

    Returns:
        New PipelineState in FAILED with the error recorded.
    """
    logger.error(
        "Pipeline g%d/l%d failed: %s",
        state.group,
        state.lecture,
        error,
    )
    return transition(state, FAILED, error=error)


def mark_complete(state: PipelineState) -> PipelineState:
    """Transition a pipeline to the COMPLETE state.

    Args:
        state: The current pipeline state.

    Returns:
        New PipelineState in COMPLETE.
    """
    logger.info(
        "Pipeline g%d/l%d completed successfully.",
        state.group,
        state.lecture,
    )
    return transition(state, COMPLETE)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def is_pipeline_active(group: int, lecture: int) -> bool:
    """Return True if a non-terminal pipeline exists for this group/lecture.

    A pipeline is considered active if its state file exists and its
    current state is not COMPLETE or FAILED.

    Args:
        group: Training group number.
        lecture: Lecture number.

    Returns:
        True if the pipeline exists and is in a non-terminal state.
    """
    state = load_state(group, lecture)
    if state is None:
        return False
    return state.state not in _TERMINAL_STATES


def is_pipeline_done(group: int, lecture: int) -> bool:
    """Return True if a pipeline exists and has reached COMPLETE.

    Args:
        group: Training group number.
        lecture: Lecture number.

    Returns:
        True if the pipeline state file exists and state is COMPLETE.
    """
    state = load_state(group, lecture)
    if state is None:
        return False
    return state.state == COMPLETE


def list_active_pipelines() -> list[PipelineState]:
    """Return all non-terminal pipeline states found in TMP_DIR.

    Scans for files matching ``pipeline_state_g*_l*.json``, loads each,
    and filters to those not in a terminal state (COMPLETE or FAILED).

    Returns:
        List of active PipelineState objects, sorted by (group, lecture).
    """
    return [s for s in list_all_pipelines() if s.state not in _TERMINAL_STATES]


def list_all_pipelines() -> list[PipelineState]:
    """Return all pipeline states found in TMP_DIR.

    Corrupt or unreadable files are skipped with a warning log.

    Returns:
        List of all PipelineState objects, sorted by (group, lecture).
    """
    results: list[PipelineState] = []
    for path in sorted(TMP_DIR.glob("pipeline_state_g*_l*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            data: dict[str, Any] = json.loads(raw)
            results.append(_deserialize(data))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Skipping unreadable pipeline state file %s: %s", path, exc
            )
    results.sort(key=lambda s: (s.group, s.lecture))
    return results


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def cleanup_completed(max_age_hours: int = 24) -> int:
    """Delete state files for COMPLETE pipelines older than *max_age_hours*.

    Only COMPLETE pipelines are removed; FAILED pipelines are retained for
    post-mortem inspection.

    Args:
        max_age_hours: Age threshold in hours.  Files whose ``updated_at``
            timestamp is older than this are deleted.  Defaults to 24 hours.

    Returns:
        Number of state files deleted.
    """
    deleted = 0
    now = datetime.now(tz=TBILISI_TZ)

    for state in list_all_pipelines():
        if state.state != COMPLETE:
            continue

        try:
            updated = datetime.fromisoformat(state.updated_at)
        except (ValueError, TypeError):
            logger.warning(
                "Pipeline g%d/l%d has unparseable updated_at=%r — skipping cleanup.",
                state.group,
                state.lecture,
                state.updated_at,
            )
            continue

        # Ensure comparison is timezone-aware.
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=TBILISI_TZ)

        age_hours = (now - updated).total_seconds() / 3600.0
        if age_hours >= max_age_hours:
            path = state_file_path(state.group, state.lecture)
            try:
                path.unlink(missing_ok=True)
                deleted += 1
                logger.info(
                    "Cleaned up completed pipeline state: g%d/l%d (age=%.1fh)",
                    state.group,
                    state.lecture,
                    age_hours,
                )
            except OSError as exc:
                logger.warning(
                    "Failed to delete state file %s: %s", path, exc
                )

    if deleted:
        logger.info("Pipeline state cleanup: removed %d completed file(s).", deleted)
    return deleted
