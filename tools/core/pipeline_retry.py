"""Pipeline Retry Orchestrator — guarantees every lecture processes.

Tracks retry attempts per (group, lecture) in a persistent JSON file,
schedules retries with exponential backoff via APScheduler date triggers,
and runs a nightly catch-all scan to find anything the webhook and
scheduler missed.

Usage::

    from tools.core.pipeline_retry import retry_orchestrator
    retry_orchestrator.schedule_retry(group=1, lecture=3, meeting_id="abc", error_msg="timeout")
    status = retry_orchestrator.get_retry_status()
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from tools.core.config import GROUPS, TBILISI_TZ, TMP_DIR

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES = 5
BACKOFF_MINUTES: tuple[int, ...] = (15, 30, 60, 120, 240)  # 15m, 30m, 1h, 2h, 4h
PERMANENTLY_FAILED = "PERMANENTLY_FAILED"
RETRY_TRACKER_PATH = TMP_DIR / "retry_tracker.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RetryRecord:
    """Mutable record tracking retry state for one lecture."""

    group: int
    lecture: int
    meeting_id: str
    attempt: int = 0
    errors: list[str] = field(default_factory=list)
    next_retry_at: str = ""
    status: str = "pending"  # pending | scheduled | permanently_failed
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=TBILISI_TZ).isoformat()


def _load_tracker() -> dict[str, dict[str, Any]]:
    """Load the retry tracker JSON from disk.

    Returns:
        Dict keyed by "g{group}_l{lecture}" with RetryRecord-compatible dicts.
    """
    if not RETRY_TRACKER_PATH.exists():
        return {}
    try:
        raw = RETRY_TRACKER_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load retry tracker: %s", exc)
    return {}


def _save_tracker(data: dict[str, dict[str, Any]]) -> None:
    """Atomically persist the retry tracker to disk."""
    tmp = RETRY_TRACKER_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.rename(RETRY_TRACKER_PATH)
    except OSError as exc:
        logger.error("Failed to save retry tracker: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _record_key(group: int, lecture: int) -> str:
    return f"g{group}_l{lecture}"


def _to_record(data: dict[str, Any]) -> RetryRecord:
    """Convert a raw dict to a RetryRecord with safe defaults."""
    return RetryRecord(
        group=int(data.get("group", 0)),
        lecture=int(data.get("lecture", 0)),
        meeting_id=str(data.get("meeting_id", "")),
        attempt=int(data.get("attempt", 0)),
        errors=list(data.get("errors", [])),
        next_retry_at=str(data.get("next_retry_at", "")),
        status=str(data.get("status", "pending")),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
    )


# ---------------------------------------------------------------------------
# RetryOrchestrator
# ---------------------------------------------------------------------------


class RetryOrchestrator:
    """Manages retry scheduling for failed pipeline runs.

    Thread-safe: all mutations go through atomic JSON file writes.
    The orchestrator does not hold in-memory state — it reads from disk
    on every call, making it safe across restarts.
    """

    def schedule_retry(
        self,
        group: int,
        lecture: int,
        meeting_id: str,
        error_msg: str,
    ) -> dict[str, Any]:
        """Record a failure and schedule the next retry attempt.

        Args:
            group: Training group number (1 or 2).
            lecture: Lecture number (1-15).
            meeting_id: Zoom meeting ID for re-polling.
            error_msg: Human-readable error from the failed attempt.

        Returns:
            Dict with retry status info (attempt, next_retry_at, or
            permanently_failed flag).
        """
        tracker = _load_tracker()
        key = _record_key(group, lecture)
        now = _now_iso()

        existing = tracker.get(key, {})
        record = _to_record(existing) if existing else RetryRecord(
            group=group,
            lecture=lecture,
            meeting_id=meeting_id,
            created_at=now,
        )

        # Update meeting_id if provided (might have changed on retry)
        if meeting_id:
            record.meeting_id = meeting_id

        record.attempt += 1
        # Keep last 10 error messages to avoid unbounded growth
        record.errors.append(f"[attempt {record.attempt}] {error_msg}")
        if len(record.errors) > 10:
            record.errors = record.errors[-10:]
        record.updated_at = now

        if record.attempt > MAX_RETRIES:
            record.status = PERMANENTLY_FAILED
            record.next_retry_at = ""
            tracker[key] = asdict(record)
            _save_tracker(tracker)

            logger.error(
                "[retry] G%d L%d PERMANENTLY FAILED after %d attempts",
                group, lecture, record.attempt,
            )
            self._alert_permanent_failure(record)
            return {
                "status": PERMANENTLY_FAILED,
                "attempt": record.attempt,
                "errors": record.errors,
            }

        # Calculate next retry time with exponential backoff
        backoff_idx = min(record.attempt - 1, len(BACKOFF_MINUTES) - 1)
        delay_minutes = BACKOFF_MINUTES[backoff_idx]
        next_time = datetime.now(tz=TBILISI_TZ) + timedelta(minutes=delay_minutes)
        record.next_retry_at = next_time.isoformat()
        record.status = "scheduled"

        tracker[key] = asdict(record)
        _save_tracker(tracker)

        logger.info(
            "[retry] G%d L%d scheduled retry #%d in %d min (at %s)",
            group, lecture, record.attempt, delay_minutes,
            next_time.strftime("%H:%M"),
        )

        # Schedule via APScheduler if available
        self._schedule_apscheduler_job(group, lecture, meeting_id, next_time)

        return {
            "status": "scheduled",
            "attempt": record.attempt,
            "next_retry_at": record.next_retry_at,
            "delay_minutes": delay_minutes,
        }

    def get_retry_status(self) -> dict[str, Any]:
        """Return current retry status for all tracked lectures.

        Returns:
            Dict with pending retries, permanent failures, and summary counts.
        """
        tracker = _load_tracker()
        pending: list[dict[str, Any]] = []
        permanently_failed: list[dict[str, Any]] = []

        for _key, raw in tracker.items():
            record = _to_record(raw)
            entry = {
                "group": record.group,
                "lecture": record.lecture,
                "meeting_id": record.meeting_id,
                "attempt": record.attempt,
                "status": record.status,
                "next_retry_at": record.next_retry_at,
                "last_error": record.errors[-1] if record.errors else "",
                "updated_at": record.updated_at,
            }
            if record.status == PERMANENTLY_FAILED:
                permanently_failed.append(entry)
            else:
                pending.append(entry)

        return {
            "pending_retries": pending,
            "permanently_failed": permanently_failed,
            "total_pending": len(pending),
            "total_permanently_failed": len(permanently_failed),
        }

    def clear_retry(self, group: int, lecture: int) -> bool:
        """Remove a lecture from the retry tracker (e.g. after manual success).

        Args:
            group: Training group number.
            lecture: Lecture number.

        Returns:
            True if the record was found and removed.
        """
        tracker = _load_tracker()
        key = _record_key(group, lecture)
        if key in tracker:
            del tracker[key]
            _save_tracker(tracker)
            logger.info("[retry] Cleared retry record for G%d L%d", group, lecture)
            return True
        return False

    def get_record(self, group: int, lecture: int) -> RetryRecord | None:
        """Load a single retry record from the tracker.

        Args:
            group: Training group number.
            lecture: Lecture number.

        Returns:
            RetryRecord if found, None otherwise.
        """
        tracker = _load_tracker()
        key = _record_key(group, lecture)
        raw = tracker.get(key)
        if raw:
            return _to_record(raw)
        return None

    def _schedule_apscheduler_job(
        self,
        group: int,
        lecture: int,
        meeting_id: str,
        fire_at: datetime,
    ) -> None:
        """Add a one-shot retry job to the running APScheduler.

        Fails silently if the scheduler is not running (e.g. during tests).
        """
        try:
            from tools.app.scheduler import _get_running_scheduler
            scheduler = _get_running_scheduler()
        except (ImportError, RuntimeError):
            logger.debug(
                "[retry] Scheduler not available — retry for G%d L%d "
                "will be picked up by nightly_catch_all instead",
                group, lecture,
            )
            return

        job_id = f"retry_g{group}_l{lecture}_attempt"

        try:
            scheduler.add_job(
                _execute_retry,
                trigger="date",
                run_date=fire_at,
                args=[group, lecture, meeting_id],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=30 * 60,
            )
            logger.info(
                "[retry] APScheduler job '%s' scheduled at %s",
                job_id, fire_at.strftime("%Y-%m-%d %H:%M %Z"),
            )
        except Exception as exc:
            logger.warning("[retry] Failed to schedule APScheduler job: %s", exc)

    def _alert_permanent_failure(self, record: RetryRecord) -> None:
        """Send operator alert with full error history for a permanently failed lecture."""
        try:
            from tools.integrations.whatsapp_sender import alert_operator

            error_history = "\n".join(record.errors[-5:])
            alert_operator(
                f"PERMANENT FAILURE: Group {record.group}, "
                f"Lecture #{record.lecture}\n"
                f"After {record.attempt} attempts, all retries exhausted.\n\n"
                f"Last errors:\n{error_history}\n\n"
                f"Manual intervention required: "
                f"POST /retry-lecture with group={record.group} "
                f"lecture={record.lecture}"
            )
        except Exception as exc:
            logger.error(
                "[retry] Failed to alert operator about permanent failure: %s", exc
            )


# Module-level singleton
retry_orchestrator = RetryOrchestrator()


# ---------------------------------------------------------------------------
# Retry execution (called by APScheduler)
# ---------------------------------------------------------------------------


async def _execute_retry(group: int, lecture: int, meeting_id: str) -> None:
    """Execute a scheduled retry: reset pipeline state, then run the pipeline.

    This is an async function called by APScheduler's AsyncIOExecutor.
    """
    import asyncio

    from tools.core.pipeline_state import (
        FAILED,
        create_pipeline,
        is_pipeline_active,
        is_pipeline_done,
        load_state,
        reset_failed,
    )

    logger.info(
        "[retry] Executing retry for G%d L%d (meeting_id=%s)",
        group, lecture, meeting_id,
    )

    # Skip if already complete or actively processing
    if is_pipeline_done(group, lecture):
        logger.info("[retry] G%d L%d already COMPLETE — clearing retry record", group, lecture)
        retry_orchestrator.clear_retry(group, lecture)
        return

    if is_pipeline_active(group, lecture):
        logger.info("[retry] G%d L%d already active — skipping retry", group, lecture)
        return

    # Reset FAILED state so pipeline can be re-created
    existing = load_state(group, lecture)
    if existing and existing.state == FAILED:
        reset_failed(group, lecture)

    # Claim dedup key in server's processing tracker
    try:
        from tools.app.server import (
            _evict_stale_tasks,
            _processing_lock,
            _processing_tasks,
            _task_key,
        )
        _evict_stale_tasks()
        key = _task_key(group, lecture)
        with _processing_lock:
            if key in _processing_tasks:
                logger.info("[retry] G%d L%d dedup key already claimed — skipping", group, lecture)
                return
            _processing_tasks[key] = datetime.now()
    except (ImportError, RuntimeError):
        pass  # Server module not available in standalone mode

    # Create pipeline state
    try:
        create_pipeline(group, lecture, meeting_id=meeting_id)
    except ValueError:
        pass  # Already exists

    # Run the pipeline in a thread (blocking I/O)
    from tools.app.scheduler import _run_post_meeting_pipeline

    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                None,
                _run_post_meeting_pipeline,
                group,
                lecture,
                meeting_id,
                True,  # skip_initial_delay — recording is already on Zoom
            ),
            timeout=4 * 3600,
        )
        # Success — clear the retry record
        retry_orchestrator.clear_retry(group, lecture)
        logger.info("[retry] G%d L%d retry succeeded — record cleared", group, lecture)
    except Exception as exc:
        logger.error("[retry] G%d L%d retry failed: %s", group, lecture, exc)
        # schedule_retry will be called by the pipeline's own failure handler


# ---------------------------------------------------------------------------
# Nightly catch-all scan
# ---------------------------------------------------------------------------


async def nightly_catch_all() -> dict[str, Any]:
    """Ultimate safety net: find and retry any unprocessed lectures.

    Runs at 02:00 Tbilisi time every night. Scans:
    1. Pipeline state files for stuck PENDING/ACTIVE states
    2. Zoom recordings from last 7 days not yet processed
    3. Pinecone for missing lecture indexes vs expected

    For each gap found, schedules a retry if under max attempts.

    Returns:
        Summary dict with counts of actions taken.
    """


    logger.info("[nightly] Starting catch-all scan...")
    now = datetime.now(tz=TBILISI_TZ)
    actions: dict[str, list[str]] = {
        "stuck_reset": [],
        "retries_scheduled": [],
        "already_complete": [],
        "skipped_max_retries": [],
    }

    # --- Phase 1: Check stuck pipelines ---
    _check_stuck_pipelines(actions, now)

    # --- Phase 2: Check Zoom recordings from last 7 days ---
    await _check_zoom_recordings(actions, now)

    # --- Phase 3: Check Pinecone for missing indexes ---
    await _check_pinecone_gaps(actions, now)

    summary = {
        "timestamp": now.isoformat(),
        "stuck_reset": len(actions["stuck_reset"]),
        "retries_scheduled": len(actions["retries_scheduled"]),
        "already_complete": len(actions["already_complete"]),
        "skipped_max_retries": len(actions["skipped_max_retries"]),
        "details": actions,
    }

    logger.info(
        "[nightly] Scan complete: %d stuck reset, %d retries scheduled, "
        "%d already complete, %d skipped (max retries)",
        summary["stuck_reset"],
        summary["retries_scheduled"],
        summary["already_complete"],
        summary["skipped_max_retries"],
    )

    return summary


def _check_stuck_pipelines(
    actions: dict[str, list[str]],
    now: datetime,
) -> None:
    """Phase 1: Reset pipelines stuck in non-terminal states for >4 hours."""
    from tools.core.pipeline_state import (
        COMPLETE,
        FAILED,
        list_all_pipelines,
        mark_failed,
    )

    for pipeline in list_all_pipelines():
        if pipeline.state in (COMPLETE, FAILED):
            continue

        # Check if stuck (updated_at > 4 hours ago)
        try:
            updated = datetime.fromisoformat(pipeline.updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=TBILISI_TZ)
        except (ValueError, TypeError):
            continue

        age_hours = (now - updated).total_seconds() / 3600.0
        if age_hours < 4.0:
            continue

        label = f"G{pipeline.group} L{pipeline.lecture}"
        logger.warning(
            "[nightly] Stuck pipeline %s in state %s for %.1f hours — marking failed",
            label, pipeline.state, age_hours,
        )
        mark_failed(pipeline, f"Stuck in {pipeline.state} for {age_hours:.1f}h (nightly cleanup)")
        actions["stuck_reset"].append(label)

        # Schedule retry
        if pipeline.meeting_id:
            record = retry_orchestrator.get_record(pipeline.group, pipeline.lecture)
            if record and record.status == PERMANENTLY_FAILED:
                actions["skipped_max_retries"].append(label)
            else:
                retry_orchestrator.schedule_retry(
                    pipeline.group, pipeline.lecture,
                    pipeline.meeting_id,
                    f"Stuck in {pipeline.state} for {age_hours:.1f}h",
                )
                actions["retries_scheduled"].append(label)


async def _check_zoom_recordings(
    actions: dict[str, list[str]],
    now: datetime,
) -> None:
    """Phase 2: Find Zoom recordings from last 7 days not yet processed."""
    import asyncio

    from tools.core.pipeline_state import is_pipeline_done, load_state, FAILED, reset_failed

    try:
        zm = __import__("tools.integrations.zoom_manager", fromlist=["zoom_manager"])
    except ImportError:
        logger.warning("[nightly] zoom_manager not available — skipping Zoom scan")
        return

    from_date = (now.date() - timedelta(days=7)).isoformat()
    to_date = now.date().isoformat()

    try:
        meetings = await asyncio.to_thread(zm.list_user_recordings, from_date, to_date)
    except Exception as exc:
        logger.warning("[nightly] Failed to list Zoom recordings: %s", exc)
        return

    if not meetings:
        return

    from tools.core.config import extract_group_from_topic, get_lecture_number

    for meeting in meetings:
        topic = meeting.get("topic", "")
        group_number = extract_group_from_topic(topic)
        if group_number is None:
            continue

        start_time_str = meeting.get("start_time", "")
        try:
            meeting_date = datetime.fromisoformat(
                start_time_str.replace("Z", "+00:00")
            ).date()
        except (ValueError, AttributeError):
            meeting_date = now.date()

        lecture_number = get_lecture_number(group_number, for_date=meeting_date)
        if lecture_number == 0:
            continue

        label = f"G{group_number} L{lecture_number}"

        if is_pipeline_done(group_number, lecture_number):
            actions["already_complete"].append(label)
            continue

        # Check if permanently failed
        record = retry_orchestrator.get_record(group_number, lecture_number)
        if record and record.status == PERMANENTLY_FAILED:
            actions["skipped_max_retries"].append(label)
            continue

        meeting_uuid = meeting.get("uuid", "")
        meeting_id = str(meeting.get("id", ""))
        poll_id = meeting_uuid or meeting_id
        if not poll_id:
            continue

        # Reset FAILED state if exists
        existing = load_state(group_number, lecture_number)
        if existing and existing.state == FAILED:
            reset_failed(group_number, lecture_number)

        retry_orchestrator.schedule_retry(
            group_number, lecture_number, poll_id,
            "Unprocessed recording found in nightly scan",
        )
        actions["retries_scheduled"].append(label)


async def _check_pinecone_gaps(
    actions: dict[str, list[str]],
    now: datetime,
) -> None:
    """Phase 3: Check Pinecone for lectures that should be indexed but aren't.

    Only checks lectures whose expected date has already passed.
    """
    import asyncio

    from tools.core.config import get_lecture_number
    from tools.core.pipeline_state import is_pipeline_done

    try:
        from tools.integrations.knowledge_indexer import get_pinecone_index
        index = await asyncio.to_thread(get_pinecone_index)
    except Exception as exc:
        logger.warning("[nightly] Pinecone not available — skipping gap check: %s", exc)
        return

    for group_number in GROUPS:
        # Determine how many lectures should have happened by now
        max_lecture = get_lecture_number(group_number, for_date=now.date())
        if max_lecture == 0:
            continue

        for lecture_number in range(1, max_lecture + 1):
            label = f"G{group_number} L{lecture_number}"

            # Already complete per pipeline state?
            if is_pipeline_done(group_number, lecture_number):
                actions["already_complete"].append(label)
                continue

            # Check Pinecone for vectors
            try:
                dummy_embedding = [0.0] * 3072
                result = await asyncio.to_thread(
                    lambda g=group_number, lec=lecture_number: index.query(
                        vector=dummy_embedding,
                        top_k=1,
                        filter={
                            "group_number": {"$eq": g},
                            "lecture_number": {"$eq": lec},
                        },
                    )
                )
                if result.get("matches"):
                    actions["already_complete"].append(label)
                    continue
            except Exception as exc:
                logger.warning(
                    "[nightly] Pinecone check failed for %s: %s", label, exc
                )
                continue

            # Not indexed — check retry status
            record = retry_orchestrator.get_record(group_number, lecture_number)
            if record and record.status == PERMANENTLY_FAILED:
                actions["skipped_max_retries"].append(label)
                continue

            # No meeting_id available from Pinecone scan — leave empty.
            # The retry execution will need to discover it from Zoom.
            logger.warning(
                "[nightly] %s missing from Pinecone — scheduling retry "
                "(no meeting_id available, will need manual /retry-lecture)",
                label,
            )
            # Only schedule if we have a meeting ID from the retry record
            if record and record.meeting_id:
                retry_orchestrator.schedule_retry(
                    group_number, lecture_number,
                    record.meeting_id,
                    "Missing from Pinecone index (nightly scan)",
                )
                actions["retries_scheduled"].append(label)
            else:
                actions["skipped_max_retries"].append(
                    f"{label} (no meeting_id)"
                )
