"""Pipeline State Machine for the Training Agent recording pipeline.

Provides atomic, file-backed state management for the full recording
processing lifecycle: download → transcription → analysis → delivery.

Each pipeline instance corresponds to one lecture for one group and is
persisted as a JSON file in TMP_DIR so that crashes and restarts can
resume mid-pipeline without reprocessing completed stages.

**Forward-only transitions** are enforced: states can only move forward
in the lifecycle (PENDING → DOWNLOADING → ... → COMPLETE).  Any attempt
to move backwards raises ``ValueError``.  The only exception is that
FAILED can be reset to PENDING via ``reset_failed()``.

**Error history** is recorded in the state file so recurring failures
can be diagnosed without needing Railway logs.

**Heartbeat** timestamps are updated periodically so stale-eviction
can distinguish truly stuck pipelines from legitimately long-running ones.

Usage::

    state = create_pipeline(group=1, lecture=3, meeting_id="abc123")
    state = transition(state, DOWNLOADING, video_path="/tmp/rec.mp4")
    state = mark_complete(state)

    # Or use the context manager for guaranteed FAILED marking:
    with pipeline_guard(group=1, lecture=3, meeting_id="abc123") as state:
        state = transition(state, DOWNLOADING, video_path="/tmp/rec.mp4")
        ...
        mark_complete(state)
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
    # Enforce forward-only transitions.
    # - FAILED can be reached from any state (it's always "forward" to error).
    # - Same-state is allowed (e.g. updating fields within TRANSCRIBING).
    # - From a terminal state, only FAILED is allowed (and only if not already FAILED).
    current_order = _STATE_ORDER.get(state.state, -1)
    new_order = _STATE_ORDER.get(new_state, -1)

    if new_state != FAILED and new_order < current_order:
        msg = (
            f"Backward transition rejected: {state.state} (#{current_order}) "
            f"→ {new_state} (#{new_order}) for g{state.group}/l{state.lecture}. "
            f"States can only move forward in the lifecycle."
        )
        logger.error(msg)
        raise ValueError(msg)

    # Prevent transitioning out of COMPLETE (except to FAILED).
    if state.state == COMPLETE and new_state != FAILED:
        msg = (
            f"Transition from COMPLETE rejected: cannot move to {new_state} "
            f"for g{state.group}/l{state.lecture}. "
            f"Use reset_failed() + create_pipeline() to restart."
        )
        logger.error(msg)
        raise ValueError(msg)

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
    values for tuple fields into tuples to preserve immutability.

    Args:
        source: Source state (frozen dataclass).
        **updates: Fields to override.

    Returns:
        New PipelineState with overrides applied.
    """
    if "transcript_chunks_done" in updates:
        raw = updates["transcript_chunks_done"]
        updates["transcript_chunks_done"] = tuple(raw) if raw is not None else ()

    if "errors" in updates:
        raw = updates["errors"]
        updates["errors"] = tuple(raw) if raw is not None else ()

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


def try_claim_pipeline(
    group: int,
    lecture: int,
    meeting_id: str = "",
) -> PipelineState | None:
    """Atomically check-and-create a pipeline. Returns the new state, or None if already active.

    Uses file locking to prevent race conditions between webhook handler,
    scheduler fallback, and startup recovery.  This is the SOLE authority
    for deduplication decisions — callers must not maintain their own
    check-then-act logic.

    Args:
        group: Training group number (1 or 2).
        lecture: Lecture number (1–15).
        meeting_id: Zoom meeting UUID associated with this recording.

    Returns:
        The newly created PipelineState if the claim succeeded, or None if
        a pipeline is already active or complete for this group/lecture.
    """
    # Thread lock first (intra-process), then file lock (inter-process)
    if not _THREAD_LOCK.acquire(blocking=False):
        logger.debug("Pipeline thread lock held for g%d/l%d", group, lecture)
        return None

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _LOCK_FILE

    lock_fd: Any = None  # typing: IO[str]
    try:
        lock_fd = open(lock_path, "w")  # noqa: SIM115
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        # Another process holds the lock — treat as "already claimed"
        logger.debug(
            "Pipeline lock held by another process for g%d/l%d", group, lecture
        )
        if lock_fd is not None:
            lock_fd.close()
        _THREAD_LOCK.release()
        return None

    try:
        # Check if pipeline is already active or complete
        existing = load_state(group, lecture)
        if existing is not None:
            if existing.state == COMPLETE:
                logger.info(
                    "Pipeline g%d/l%d already COMPLETE — skipping", group, lecture
                )
                return None
            if existing.state == FAILED:
                # Allow retry of failed pipelines
                logger.info(
                    "Pipeline g%d/l%d was FAILED — allowing retry", group, lecture
                )
                state_file_path(group, lecture).unlink(missing_ok=True)
            elif existing.state not in _TERMINAL_STATES:
                logger.info(
                    "Pipeline g%d/l%d already active (state=%s) — skipping",
                    group, lecture, existing.state,
                )
                return None

        # Create the pipeline
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
            "Claimed pipeline g%d/l%d (meeting_id=%r)", group, lecture, meeting_id
        )
        return state
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        _THREAD_LOCK.release()


def release_pipeline(group: int, lecture: int) -> None:
    """Release a pipeline claim. Called in finally blocks after pipeline completes or fails.

    The state file already reflects COMPLETE or FAILED via mark_complete/mark_failed.
    This function exists for future extensibility (e.g., releasing named locks).

    Args:
        group: Training group number.
        lecture: Lecture number.
    """
    logger.debug("Pipeline g%d/l%d released", group, lecture)


def mark_failed(state: PipelineState, error: str) -> PipelineState:
    """Transition a pipeline to the FAILED state with an error message.

    Appends the error to the error history list with a timestamp so
    recurring failures can be diagnosed without needing external logs.

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
    # Append to error history (keep last 20 entries to bound file size)
    error_entry = {"timestamp": _now_iso(), "error": error}
    new_errors = (*state.errors, error_entry)[-20:]
    return transition(state, FAILED, error=error, errors=new_errors)


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


def try_claim_pipeline(
    group: int,
    lecture: int,
    meeting_id: str = "",
) -> PipelineState | None:
    """Atomically claim a pipeline for processing, returning None on conflict.

    Uses a per-(group, lecture) lock so that two *different* lectures can
    be claimed concurrently — only the same lecture is serialized.

    Args:
        group: Training group number (1 or 2).
        lecture: Lecture number (1-15).
        meeting_id: Zoom meeting UUID associated with this recording.

    Returns:
        A new PipelineState in PENDING if the claim succeeded, or None if
        another thread already holds an active pipeline for this slot.
    """
    lock = _get_pipeline_lock(group, lecture)
    if not lock.acquire(blocking=False):
        logger.info(
            "Pipeline claim rejected (lock held): g%d/l%d",
            group,
            lecture,
        )
        return None

    try:
        if is_pipeline_active(group, lecture):
            logger.info(
                "Pipeline claim rejected (already active): g%d/l%d",
                group,
                lecture,
            )
            return None
        return create_pipeline(group, lecture, meeting_id=meeting_id)
    except Exception:
        lock.release()
        raise
    # NOTE: the caller is responsible for releasing the lock when the
    # pipeline finishes (via release_pipeline_lock).


def release_pipeline_lock(group: int, lecture: int) -> None:
    """Release the per-pipeline lock after processing completes.

    Safe to call even if the lock is not held (e.g. during cleanup).

    Args:
        group: Training group number.
        lecture: Lecture number.
    """
    lock = _get_pipeline_lock(group, lecture)
    try:
        lock.release()
    except RuntimeError:
        # Lock was not held — harmless.
        pass


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_last_activity_time(state: PipelineState) -> datetime | None:
    """Return the most recent activity timestamp for a pipeline.

    Uses ``last_heartbeat`` if available, falling back to ``updated_at``,
    then ``started_at``.  This is the preferred timestamp for stale
    pipeline detection — it reflects actual liveness, not just the last
    state transition.

    Args:
        state: The pipeline state to inspect.

    Returns:
        A timezone-aware datetime, or None if no valid timestamp exists.
    """
    for ts_field in (state.last_heartbeat, state.updated_at, state.started_at):
        if not ts_field:
            continue
        try:
            dt = datetime.fromisoformat(ts_field)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TBILISI_TZ)
            return dt
        except (ValueError, TypeError):
            continue
    return None


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
# Heartbeat
# ---------------------------------------------------------------------------

# Background heartbeat threads, keyed by (group, lecture).
_heartbeat_threads: dict[tuple[int, int], threading.Event] = {}

HEARTBEAT_INTERVAL_SECONDS = 300  # 5 minutes


def update_heartbeat(state: PipelineState) -> PipelineState:
    """Update the last_heartbeat timestamp on the pipeline state file.

    Called periodically while a pipeline is running so that stale-eviction
    logic can distinguish truly stuck pipelines from long-running ones.

    Args:
        state: The current pipeline state.

    Returns:
        New PipelineState with ``last_heartbeat`` updated.
    """
    now = _now_iso()
    new = _replace_state(state, last_heartbeat=now)
    save_state(new)
    logger.debug(
        "Heartbeat updated for g%d/l%d at %s",
        state.group, state.lecture, now,
    )
    return new


def _heartbeat_loop(group: int, lecture: int, stop_event: threading.Event) -> None:
    """Background thread that updates the heartbeat every 5 minutes."""
    while not stop_event.wait(timeout=HEARTBEAT_INTERVAL_SECONDS):
        try:
            current = load_state(group, lecture)
            if current is None or current.state in _TERMINAL_STATES:
                break
            update_heartbeat(current)
        except Exception as exc:
            logger.warning("Heartbeat update failed for g%d/l%d: %s", group, lecture, exc)


def start_heartbeat(group: int, lecture: int) -> None:
    """Start a background heartbeat thread for a pipeline.

    The thread updates ``last_heartbeat`` every 5 minutes and stops when
    the pipeline reaches a terminal state or ``stop_heartbeat()`` is called.
    """
    key = (group, lecture)
    stop_existing = _heartbeat_threads.pop(key, None)
    if stop_existing is not None:
        stop_existing.set()

    stop_event = threading.Event()
    _heartbeat_threads[key] = stop_event
    thread = threading.Thread(
        target=_heartbeat_loop,
        args=(group, lecture, stop_event),
        daemon=True,
        name=f"heartbeat-g{group}-l{lecture}",
    )
    thread.start()
    logger.debug("Heartbeat thread started for g%d/l%d", group, lecture)


def stop_heartbeat(group: int, lecture: int) -> None:
    """Stop the background heartbeat thread for a pipeline."""
    key = (group, lecture)
    stop_event = _heartbeat_threads.pop(key, None)
    if stop_event is not None:
        stop_event.set()
        logger.debug("Heartbeat thread stopped for g%d/l%d", group, lecture)


# ---------------------------------------------------------------------------
# Checkpoint validation
# ---------------------------------------------------------------------------

# Minimum size thresholds for checkpoint files (bytes)
_CHECKPOINT_MIN_SIZES: dict[str, int] = {
    "transcript": 100,
    "summary": 100,
    "gap_analysis": 50,
    "deep_analysis": 50,
}


def validate_checkpoint(
    group: int,
    lecture: int,
    content_type: str,
) -> bool:
    """Validate a checkpoint file for a specific pipeline stage.

    Checks that the checkpoint file exists, is non-empty, meets a minimum
    size threshold, and (for JSON files) parses correctly.  For text
    checkpoints (transcript, summary), verifies the content contains
    actual text, not just whitespace.

    Args:
        group: Training group number.
        lecture: Lecture number.
        content_type: One of ``transcript``, ``summary``, ``gap_analysis``,
            ``deep_analysis``.

    Returns:
        True if the checkpoint is valid and can be resumed from.
    """
    checkpoint_path = TMP_DIR / f"g{group}_l{lecture}_{content_type}.txt"

    if not checkpoint_path.exists():
        return False

    try:
        file_size = checkpoint_path.stat().st_size
    except OSError:
        return False

    min_size = _CHECKPOINT_MIN_SIZES.get(content_type, 50)
    if file_size < min_size:
        logger.warning(
            "Checkpoint %s too small (%d bytes, min %d) — invalid",
            checkpoint_path.name, file_size, min_size,
        )
        return False

    # Read and validate content
    try:
        content = checkpoint_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Checkpoint %s unreadable: %s", checkpoint_path.name, exc)
        return False

    # Check for actual text content (not just whitespace)
    if not content.strip():
        logger.warning("Checkpoint %s contains only whitespace", checkpoint_path.name)
        return False

    # For JSON checkpoint files, validate JSON parsing
    if checkpoint_path.suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Checkpoint %s has invalid JSON: %s", checkpoint_path.name, exc
            )
            return False

    return True


def invalidate_checkpoint(group: int, lecture: int, content_type: str) -> bool:
    """Delete an invalid checkpoint file so the stage restarts from scratch.

    Args:
        group: Training group number.
        lecture: Lecture number.
        content_type: Content type identifier.

    Returns:
        True if the file was deleted, False if it didn't exist.
    """
    checkpoint_path = TMP_DIR / f"g{group}_l{lecture}_{content_type}.txt"
    if checkpoint_path.exists():
        try:
            checkpoint_path.unlink()
            logger.info(
                "Invalidated checkpoint %s — stage will restart",
                checkpoint_path.name,
            )
            return True
        except OSError as exc:
            logger.warning("Failed to delete checkpoint %s: %s", checkpoint_path.name, exc)
    return False


# ---------------------------------------------------------------------------
# Pipeline guard context manager
# ---------------------------------------------------------------------------


@contextmanager
def pipeline_guard(
    group: int,
    lecture: int,
    meeting_id: str = "",
    *,
    create_new: bool = True,
) -> Generator[PipelineState, None, None]:
    """Context manager that guarantees FAILED marking on unhandled exceptions.

    Creates (or loads) a pipeline state, starts a heartbeat, yields it
    to the caller, and ensures the pipeline is marked FAILED if the block
    exits without reaching COMPLETE or FAILED.

    Args:
        group: Training group number.
        lecture: Lecture number.
        meeting_id: Zoom meeting ID (used when creating a new pipeline).
        create_new: If True (default), create a new pipeline. If False,
            load the existing one (raises PipelineClaimError if not found).

    Yields:
        The pipeline state for the caller to use and transition.

    Raises:
        PipelineClaimError: If the pipeline cannot be claimed (already
            active when create_new=True, or not found when create_new=False).
    """
    pipeline: PipelineState | None = None
    try:
        if create_new:
            try:
                pipeline = create_pipeline(group, lecture, meeting_id)
            except ValueError as exc:
                raise PipelineClaimError(
                    f"Cannot claim pipeline for g{group}/l{lecture}: {exc}"
                ) from exc
        else:
            pipeline = load_state(group, lecture)
            if pipeline is None:
                raise PipelineClaimError(
                    f"No existing pipeline found for g{group}/l{lecture}"
                )

        start_heartbeat(group, lecture)
        yield pipeline
    except PipelineClaimError:
        raise
    except Exception as exc:
        if pipeline is not None:
            # Only mark_failed if not already in a terminal state
            current = load_state(group, lecture)
            if current is not None and current.state not in _TERMINAL_STATES:
                mark_failed(current, str(exc))
        raise
    finally:
        stop_heartbeat(group, lecture)
        # Safety net: if pipeline exited without reaching terminal state,
        # mark as FAILED to prevent it from being stuck forever.
        if pipeline is not None:
            current = load_state(group, lecture)
            if current is not None and current.state not in _TERMINAL_STATES:
                mark_failed(current, "Pipeline exited without completion")


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def reset_failed(group: int, lecture: int) -> bool:
    """Delete the state file for a FAILED pipeline so it can be retried.

    Only removes the file if the current state is FAILED.  This allows
    the pipeline to be re-created from scratch.

    Args:
        group: Training group number.
        lecture: Lecture number.

    Returns:
        True if the state file was deleted, False otherwise.
    """
    state = load_state(group, lecture)
    if state is None:
        return False
    if state.state != FAILED:
        logger.warning(
            "Cannot reset pipeline g%d/l%d — state is %s, not FAILED",
            group, lecture, state.state,
        )
        return False
    path = state_file_path(group, lecture)
    try:
        path.unlink(missing_ok=True)
        logger.info("Reset FAILED pipeline state: g%d/l%d", group, lecture)
        return True
    except OSError as exc:
        logger.warning("Failed to delete state file %s: %s", path, exc)
        return False


def cleanup_stale_failed(max_age_hours: int = 12) -> int:
    """Auto-clean FAILED pipeline state files older than max_age_hours.

    This prevents old FAILED states from blocking retry attempts indefinitely.

    Args:
        max_age_hours: Age threshold in hours. Defaults to 12 hours.

    Returns:
        Number of state files deleted.
    """
    deleted = 0
    now = datetime.now(tz=TBILISI_TZ)

    for state in list_all_pipelines():
        if state.state != FAILED:
            continue

        try:
            updated = datetime.fromisoformat(state.updated_at)
        except (ValueError, TypeError):
            continue

        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=TBILISI_TZ)

        age_hours = (now - updated).total_seconds() / 3600.0
        if age_hours >= max_age_hours:
            path = state_file_path(state.group, state.lecture)
            try:
                path.unlink(missing_ok=True)
                deleted += 1
                logger.info(
                    "Auto-cleaned stale FAILED pipeline: g%d/l%d (age=%.1fh)",
                    state.group, state.lecture, age_hours,
                )
            except OSError:
                pass

    if deleted:
        logger.info("Stale FAILED pipeline cleanup: removed %d file(s).", deleted)
    return deleted


def cleanup_stale_pending(max_age_minutes: int = 30) -> int:
    """Reset non-terminal pipeline states older than max_age_minutes.

    Called at startup to recover from Railway restarts that killed
    in-progress pipelines.  PENDING/ACTIVE states from before the
    restart are stuck and must be cleared so the lecture can be
    reprocessed.

    Args:
        max_age_minutes: Age threshold in minutes.  Non-terminal states
            whose ``started_at`` timestamp is older than this are marked
            FAILED.  Defaults to 30 minutes.

    Returns:
        Number of pipeline states marked FAILED.
    """
    recovered = 0
    now = datetime.now(tz=TBILISI_TZ)

    for state in list_all_pipelines():
        # Only touch non-terminal states (PENDING, DOWNLOADING, etc.)
        if state.state in _TERMINAL_STATES:
            continue

        # Use started_at as the reference — it reflects when the
        # pipeline was originally created, not the last transition.
        timestamp_str = state.started_at or state.updated_at
        try:
            started = datetime.fromisoformat(timestamp_str)
        except (ValueError, TypeError):
            # Unparseable timestamp — mark as stale to be safe.
            logger.warning(
                "Pipeline g%d/l%d has unparseable started_at=%r — marking FAILED.",
                state.group,
                state.lecture,
                state.started_at,
            )
            mark_failed(state, "Stale pipeline recovered at startup (bad timestamp)")
            recovered += 1
            continue

        if started.tzinfo is None:
            started = started.replace(tzinfo=TBILISI_TZ)

        age_minutes = (now - started).total_seconds() / 60.0
        if age_minutes >= max_age_minutes:
            logger.info(
                "Recovering stale %s pipeline: g%d/l%d (age=%.1fm)",
                state.state,
                state.group,
                state.lecture,
                age_minutes,
            )
            mark_failed(
                state,
                f"Stale {state.state} pipeline recovered at startup "
                f"(age={age_minutes:.0f}m, threshold={max_age_minutes}m)",
            )
            recovered += 1

    if recovered:
        logger.info(
            "Stale pending pipeline cleanup: recovered %d pipeline(s).",
            recovered,
        )
    return recovered


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
