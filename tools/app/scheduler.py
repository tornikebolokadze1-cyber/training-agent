"""Pre-meeting automation scheduler for Training Agent.

Orchestrates the full lifecycle for both training groups:
  - T-120 min: create Zoom meeting, send email + WhatsApp reminders
  - Post-meeting: download recording, upload to Drive, transcribe,
    generate summary Doc, run gap analysis, send private WhatsApp report.

Run:
    python -m tools.scheduler
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from tools.core.config import (
    GROUPS,
    TBILISI_TZ,
    TMP_DIR,
    TOTAL_LECTURES,
    get_lecture_folder_name,
    get_lecture_number,
)
from tools.core.pipeline_state import (
    is_pipeline_active,
    is_pipeline_done,
)
from tools.integrations.whatsapp_sender import alert_operator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LECTURE_START_HOUR = 20       # 20:00 GMT+4
REMINDER_OFFSET_MINUTES = 120  # fire at T-120 (18:00)
REMINDER_HOUR = LECTURE_START_HOUR - (REMINDER_OFFSET_MINUTES // 60)  # 18
REMINDER_MINUTE = 0

# Recording polling: Zoom processes recordings 15–90 min after meeting ends.
# We wait RECORDING_INITIAL_DELAY before the first poll, then retry every
# RECORDING_POLL_INTERVAL for up to RECORDING_POLL_TIMEOUT seconds.
RECORDING_INITIAL_DELAY = 15 * 60       # 15 minutes
RECORDING_POLL_INTERVAL = 5 * 60        # 5 minutes between polls
RECORDING_POLL_TIMEOUT = 3 * 60 * 60    # 3 hours absolute deadline

# ---------------------------------------------------------------------------
# Lazy imports — zoom_manager may not be configured yet.
# All calls to these modules go through the helper below so the scheduler
# file is importable regardless.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Persistent post-meeting job store (survives Railway deploys / restarts)
# ---------------------------------------------------------------------------
_PENDING_JOBS_FILE = Path(TMP_DIR) / "pending_post_meeting_jobs.json"


def _save_pending_job(group_number: int, lecture_number: int, meeting_id: str,
                      fire_time_iso: str) -> None:
    """Persist a pending post-meeting job to disk so it survives restarts."""
    jobs: list[dict] = []
    if _PENDING_JOBS_FILE.exists():
        try:
            jobs = json.loads(_PENDING_JOBS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # Replace existing entry for same group+lecture
    jobs = [j for j in jobs if not (j["group"] == group_number and j["lecture"] == lecture_number)]
    jobs.append({
        "group": group_number,
        "lecture": lecture_number,
        "meeting_id": meeting_id,
        "fire_time": fire_time_iso,
    })
    tmp_path = _PENDING_JOBS_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(jobs, indent=2))
    tmp_path.rename(_PENDING_JOBS_FILE)
    logger.info("[persist] Saved pending post-meeting job: G%d L%d -> %s",
                group_number, lecture_number, fire_time_iso)


def _remove_pending_job(group_number: int, lecture_number: int) -> None:
    """Remove a completed/consumed post-meeting job from the persistent store."""
    if not _PENDING_JOBS_FILE.exists():
        return
    try:
        jobs = json.loads(_PENDING_JOBS_FILE.read_text())
        jobs = [j for j in jobs if not (j["group"] == group_number and j["lecture"] == lecture_number)]
        _PENDING_JOBS_FILE.write_text(json.dumps(jobs, indent=2))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[persist] Failed to remove pending job: %s", exc)


def _restore_pending_jobs(scheduler: AsyncIOScheduler) -> int:
    """Re-schedule any persisted post-meeting jobs that are still valid.

    Called once at startup. Jobs with fire_time in the past (but within
    misfire_grace_time of 30 min) are fired immediately.

    Returns:
        Number of jobs restored.
    """
    if not _PENDING_JOBS_FILE.exists():
        return 0
    try:
        jobs = json.loads(_PENDING_JOBS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return 0

    now = datetime.now(TBILISI_TZ)
    restored = 0
    for entry in jobs:
        try:
            fire_time = datetime.fromisoformat(entry["fire_time"])
        except (ValueError, KeyError):
            continue
        # Skip jobs more than 2 hours in the past (too stale)
        if fire_time < now - timedelta(hours=2):
            logger.info("[persist] Skipping stale job: G%d L%d (fire_time=%s)",
                        entry.get("group"), entry.get("lecture"), entry.get("fire_time"))
            continue

        _schedule_post_meeting(
            scheduler=scheduler,
            group_number=entry["group"],
            lecture_number=entry["lecture"],
            meeting_id=entry["meeting_id"],
            fire_at_hour=fire_time.hour,
            fire_at_minute=fire_time.minute,
        )
        restored += 1

    logger.info("[persist] Restored %d post-meeting jobs from disk", restored)
    return restored


def _import_zoom_manager():
    """Lazy-import tools.integrations.zoom_manager; raises ImportError with a clear message."""
    try:
        import tools.integrations.zoom_manager as zm
        return zm
    except ImportError as exc:
        raise ImportError(
            "tools/zoom_manager.py is not yet created. "
            "Implement create_meeting(), get_meeting_recordings(), "
            "and download_recording() before running the scheduler."
        ) from exc


# ---------------------------------------------------------------------------
# Recording readiness check (blocking — runs in thread executor)
# ---------------------------------------------------------------------------


def check_recording_ready(meeting_id: str) -> list[dict[str, Any]]:
    """Poll the Zoom API until all recording segments are available.

    When a host disconnects and rejoins, Zoom creates multiple recording
    segments under the same meeting ID. This function waits for at least one
    completed MP4, then does one extra poll to catch late-arriving segments.

    Blocks the calling thread. Intended to be called via
    ``asyncio.get_event_loop().run_in_executor()``.

    Args:
        meeting_id: The Zoom meeting ID string.

    Returns:
        A list of completed MP4 recording file dicts (each contains
        ``download_url``, ``file_type``, etc.), or an empty list if the
        timeout expires before any recording appears.

    Raises:
        ImportError: If ``tools.integrations.zoom_manager`` is not yet implemented.
    """
    zm = _import_zoom_manager()

    elapsed = 0
    consecutive_404s = 0  # Track "still processing" 404s for adaptive backoff
    logger.info(
        "[recording] Waiting %d min before first poll for meeting %s...",
        RECORDING_INITIAL_DELAY // 60,
        meeting_id,
    )
    time.sleep(RECORDING_INITIAL_DELAY)
    elapsed += RECORDING_INITIAL_DELAY

    while elapsed < RECORDING_POLL_TIMEOUT:
        try:
            recordings = zm.get_meeting_recordings(meeting_id)
            consecutive_404s = 0  # Reset on success
        except Exception as exc:
            # Distinguish transient (network) vs non-transient (auth) errors
            error_str = str(exc).lower()
            is_auth_error = any(kw in error_str for kw in ("401", "403", "unauthorized", "forbidden"))
            if is_auth_error:
                logger.error(
                    "[recording] Non-transient auth error for meeting %s: %s — aborting",
                    meeting_id, exc,
                )
                try:
                    alert_operator(
                        f"Zoom auth error polling recording for meeting {meeting_id}: {exc}"
                    )
                except Exception:
                    logger.error("[recording] alert_operator also failed for meeting %s", meeting_id)
                return []

            # Adaptive backoff for Zoom "still processing" 404s (common for large lectures)
            is_still_processing = "404" in error_str and (
                "3301" in error_str or "processing" in error_str or "still" in error_str
            )
            if is_still_processing:
                consecutive_404s += 1
                # Exponential backoff: 5, 10, 20, 30, 30... minutes (cap at 30 min)
                backoff = min(RECORDING_POLL_INTERVAL * (2 ** (consecutive_404s - 1)), 30 * 60)
                logger.warning(
                    "[recording] Zoom still processing meeting %s (attempt %d) "
                    "— backing off %d min",
                    meeting_id, consecutive_404s, backoff // 60,
                )
                time.sleep(backoff)
                elapsed += backoff
                continue

            logger.warning(
                "[recording] Transient error for meeting %s: %s — retrying in %d min",
                meeting_id, exc, RECORDING_POLL_INTERVAL // 60,
            )
            time.sleep(RECORDING_POLL_INTERVAL)
            elapsed += RECORDING_POLL_INTERVAL
            continue

        # Zoom returns a list of recording files; collect all completed MP4s.
        files: list[dict] = recordings.get("recording_files", [])
        mp4_files = [
            f for f in files
            if f.get("file_type", "").upper() in {"MP4", "VIDEO"}
            and f.get("status", "").upper() == "COMPLETED"
        ]

        if mp4_files:
            logger.info(
                "[recording] Found %d completed MP4 segment(s) for meeting %s "
                "— waiting one more poll to catch late segments...",
                len(mp4_files),
                meeting_id,
            )
            # Wait one extra cycle — a second segment may still be processing
            time.sleep(RECORDING_POLL_INTERVAL)
            try:
                recordings = zm.get_meeting_recordings(meeting_id)
                files = recordings.get("recording_files", [])
                mp4_files = [
                    f for f in files
                    if f.get("file_type", "").upper() in {"MP4", "VIDEO"}
                    and f.get("status", "").upper() == "COMPLETED"
                ]
            except Exception as exc:
                logger.warning(
                    "[recording] Extra poll failed: %s — proceeding with %d segment(s)",
                    exc, len(mp4_files),
                )

            logger.info(
                "[recording] Final count: %d MP4 segment(s) for meeting %s",
                len(mp4_files),
                meeting_id,
            )
            for i, seg in enumerate(mp4_files, 1):
                logger.info(
                    "[recording]   Segment %d: %s",
                    i, seg.get("download_url", "(no url)"),
                )
            return mp4_files

        logger.info(
            "[recording] Not ready yet for meeting %s (elapsed %d min) — "
            "retrying in %d min...",
            meeting_id,
            elapsed // 60,
            RECORDING_POLL_INTERVAL // 60,
        )
        time.sleep(RECORDING_POLL_INTERVAL)
        elapsed += RECORDING_POLL_INTERVAL

    logger.error(
        "[recording] Timed out waiting for recording of meeting %s after %d min",
        meeting_id,
        RECORDING_POLL_TIMEOUT // 60,
    )
    try:
        alert_operator(
            f"Recording NOT FOUND after {RECORDING_POLL_TIMEOUT // 60} min "
            f"for meeting {meeting_id}.\n"
            f"Check Zoom dashboard — manual processing may be needed."
        )
    except Exception:
        logger.error("[recording] alert_operator also failed for meeting %s", meeting_id)
    return []


# ---------------------------------------------------------------------------
# Post-meeting pipeline (blocking — runs in thread executor)
# ---------------------------------------------------------------------------


def _concatenate_segments(segment_paths: list[Path], output_path: Path) -> None:
    """Concatenate multiple MP4 segments into a single file using ffmpeg.

    Uses the "concat demuxer" which is lossless and fast (no re-encoding).

    Args:
        segment_paths: Ordered list of MP4 file paths to join.
        output_path: Destination path for the merged file.

    Raises:
        RuntimeError: If ffmpeg exits with a non-zero return code.
    """
    import subprocess

    concat_list = output_path.parent / f"{output_path.stem}_segments.txt"
    concat_list.write_text(
        "\n".join(f"file '{p}'" for p in segment_paths),
        encoding="utf-8",
    )

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(output_path),
    ]
    logger.info("[post] Concatenating %d segments with ffmpeg...", len(segment_paths))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    concat_list.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[:500]}")

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info("[post] Concatenation complete: %.1f MB", size_mb)


def _run_post_meeting_pipeline(
    group_number: int,
    lecture_number: int,
    meeting_id: str,
) -> None:
    """Full post-meeting pipeline, executed in a background thread.

    Steps:
        1. Poll Zoom until recording segments are available.
        2. Download all segments to .tmp/.
        3. If multiple segments, concatenate with ffmpeg (lossless).
        4. Upload recording to Google Drive (correct lecture folder).
        5. Delegate to transcribe_and_index() for the full analysis pipeline
           (transcribe → analyze → Drive summary + private report → WhatsApp → Pinecone).

    IMPORTANT: This function is called from both the meeting.ended webhook
    (server.py) and the scheduler fallback (post_meeting_job). The dedup key
    in ``_processing_tasks`` MUST be cleaned up in ALL exit paths — including
    early returns — to prevent stale keys from blocking future processing.

    Args:
        group_number: 1 or 2.
        lecture_number: Ordinal lecture number (1–15).
        meeting_id: Zoom meeting ID used to poll for the recording.
    """
    from tools.integrations.gdrive_manager import (
        ensure_folder,
        get_drive_service,
        upload_file,
    )
    from tools.services.transcribe_lecture import transcribe_and_index

    # Helper to clean up dedup key on ALL exit paths (including early returns)
    def _cleanup_dedup() -> None:
        try:
            from tools.app.server import _processing_tasks, _task_key
            key = _task_key(group_number, lecture_number)
            _processing_tasks.pop(key, None)
            logger.info("[post] Dedup key %s removed from _processing_tasks", key)
        except (ImportError, ValueError, RuntimeError) as exc:
            # ValueError: WhatsAppAssistant init may fail during import
            # RuntimeError: circular import edge cases
            logger.debug("[post] Could not import server for dedup cleanup: %s", exc)

    # Disk space check — abort if less than 2GB free
    disk_usage = shutil.disk_usage(str(TMP_DIR))
    free_gb = disk_usage.free / (1024 ** 3)
    if free_gb < 2.0:
        logger.error("[post] Insufficient disk space: %.1f GB free (need 2+ GB). Aborting.", free_gb)
        _cleanup_dedup()
        try:
            alert_operator(f"Disk space critically low: {free_gb:.1f} GB. Pipeline for G{group_number} L{lecture_number} aborted.")
        except Exception:
            pass
        return
    logger.info("[post] Disk space check: %.1f GB free — OK", free_gb)

    group = GROUPS[group_number]
    lecture_folder_name = get_lecture_folder_name(lecture_number)
    temp_files: list[Path] = []

    logger.info(
        "[post] Starting pipeline — Group %d, Lecture #%d, Meeting %s",
        group_number,
        lecture_number,
        meeting_id,
    )

    try:
        # ---- Step 1: Wait for recording segments ---------------------------
        recordings = check_recording_ready(meeting_id)
        if not recordings:
            logger.error(
                "[post] Aborting: no recording found for meeting %s", meeting_id
            )
            _cleanup_dedup()
            try:
                alert_operator(
                    f"Pipeline ABORTED for Group {group_number}, Lecture #{lecture_number}: "
                    f"no recording found for meeting {meeting_id}. "
                    f"Check Zoom — the recording may not have been saved."
                )
            except Exception:
                pass
            return

        # ---- Step 2: Download all segments ---------------------------------
        zm = _import_zoom_manager()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        segment_paths: list[Path] = []

        logger.info("[post] Downloading %d recording segment(s)...", len(recordings))
        for i, rec in enumerate(recordings):
            seg_filename = f"group{group_number}_lecture{lecture_number}_{timestamp}_seg{i}.mp4"
            seg_path = TMP_DIR / seg_filename

            try:
                access_token = zm.get_access_token()
                zm.download_recording(rec["download_url"], access_token, seg_path)
                segment_paths.append(seg_path)
                temp_files.append(seg_path)
            except Exception as exc:
                logger.error("[post] Download failed for segment %d: %s", i, exc)
                _cleanup_dedup()
                alert_operator(
                    f"Recording download FAILED for Group {group_number}, "
                    f"Lecture #{lecture_number} (segment {i}).\nError: {exc}\n"
                    f"Check Zoom dashboard for manual download."
                )
                return

        # ---- Step 3: Concatenate if multiple segments ----------------------
        local_filename = f"group{group_number}_lecture{lecture_number}_{timestamp}.mp4"
        local_path = TMP_DIR / local_filename

        if len(segment_paths) == 1:
            # Single segment — just rename
            segment_paths[0].rename(local_path)
            temp_files = [local_path]
        else:
            try:
                _concatenate_segments(segment_paths, local_path)
                temp_files.append(local_path)
            except Exception as exc:
                logger.error("[post] Segment concatenation failed: %s", exc)
                _cleanup_dedup()
                alert_operator(
                    f"ffmpeg concat FAILED for Group {group_number}, "
                    f"Lecture #{lecture_number}.\nError: {exc}\n"
                    f"Segments are in .tmp/ for manual merge."
                )
                return

        file_size_mb = local_path.stat().st_size / (1024 * 1024)
        logger.info(
            "[post] Recording ready: %.1f MB (%d segment(s))",
            file_size_mb,
            len(segment_paths),
        )

        # ---- Step 4: Upload recording to Google Drive ----------------------
        logger.info("[post] Uploading recording to Google Drive...")
        service = get_drive_service()
        lecture_folder_id = ensure_folder(
            service,
            lecture_folder_name,
            group["drive_folder_id"],
        )
        upload_file(local_path, lecture_folder_id)
        logger.info("[post] Recording uploaded to Drive")

        # ---- Step 5: Full analysis pipeline --------------------------------
        # Delegates all analysis, Drive uploads, WhatsApp notifications,
        # and Pinecone indexing to the single source of truth.
        logger.info("[post] Running full analysis pipeline...")
        index_counts = transcribe_and_index(group_number, lecture_number, local_path)
        logger.info(
            "[post] Pipeline complete — Group %d, Lecture #%d (%d vectors indexed)",
            group_number,
            lecture_number,
            sum(index_counts.values()),
        )

    except Exception as exc:
        logger.exception(
            "[post] Pipeline failed for Group %d, Lecture #%d: %s",
            group_number,
            lecture_number,
            exc,
        )
        alert_operator(
            f"Pipeline FAILED for Group {group_number}, Lecture #{lecture_number}.\n"
            f"Error: {exc}"
        )
    finally:
        # Clean up in-memory cache — always, even if already cleaned in early return
        _cleanup_dedup()
        _remove_pending_job(group_number, lecture_number)

        # Clean up all temp files (segments + merged)
        for p in temp_files:
            if p.exists():
                p.unlink()
                logger.info("[post] Cleaned up temp file: %s", p.name)


# ---------------------------------------------------------------------------
# Pre-meeting job (async — runs in the AsyncIOExecutor)
# ---------------------------------------------------------------------------


async def pre_meeting_job(group_number: int) -> None:
    """Create a Zoom meeting and send email + WhatsApp reminders.

    Fires at T-120 min (18:00 Tbilisi time) on each meeting day.
    Immediately schedules a post-meeting job on the scheduler for T+120 min
    (22:00) so recording polling starts right after the lecture ends.

    Args:
        group_number: 1 or 2.
    """
    today = datetime.now(TBILISI_TZ).date()
    lecture_number = get_lecture_number(group_number, for_date=today)

    if lecture_number == 0:
        logger.info(
            "[pre] Group %d: no lecture scheduled today (%s) — skipping",
            group_number,
            today.isoformat(),
        )
        return

    if lecture_number > TOTAL_LECTURES:
        logger.info(
            "[pre] Group %d: all %d lectures completed — skipping",
            group_number,
            TOTAL_LECTURES,
        )
        return

    logger.info(
        "[pre] Group %d, Lecture #%d — creating Zoom meeting...",
        group_number,
        lecture_number,
    )

    # ---- Create Zoom meeting ------------------------------------------------
    loop = asyncio.get_running_loop()
    zoom_join_url: str = ""
    zoom_meeting_id: str = ""
    zoom_meeting_uuid: str = ""

    try:
        zm = _import_zoom_manager()
        # Build the start_time for today at 20:00 Tbilisi time
        today_start = datetime.now(TBILISI_TZ).replace(
            hour=LECTURE_START_HOUR, minute=0, second=0, microsecond=0,
        )
        meeting_info: dict = await loop.run_in_executor(
            None,
            lambda: zm.create_meeting(group_number, lecture_number, today_start),
        )
        zoom_join_url = meeting_info.get("join_url", "")
        zoom_meeting_id = str(meeting_info.get("id", ""))
        zoom_meeting_uuid = str(meeting_info.get("uuid", ""))
        logger.info(
            "[pre] Zoom meeting created: %s (ID: %s, UUID: %s)",
            zoom_join_url,
            zoom_meeting_id,
            zoom_meeting_uuid,
        )
    except ImportError as exc:
        logger.error("[pre] zoom_manager not available: %s", exc)
        # Continue — still attempt notifications with a placeholder link
        zoom_join_url = "(Zoom link unavailable — see instructor)"
    except Exception as exc:
        logger.error("[pre] Failed to create Zoom meeting: %s", exc)
        zoom_join_url = "(Zoom meeting creation failed)"
        alert_operator(
            f"Zoom meeting creation FAILED for Group {group_number}, "
            f"Lecture #{lecture_number}.\nError: {exc}\n"
            f"Create the meeting manually."
        )

    # NOTE: Email invitations are handled automatically by Zoom when a meeting
    # is created with attendee emails in the settings. No separate email step
    # is needed.

    # ---- WhatsApp reminder --------------------------------------------------
    try:
        from tools.integrations.whatsapp_sender import send_group_reminder

        await loop.run_in_executor(
            None,
            send_group_reminder,
            group_number,
            zoom_join_url,
            lecture_number,
        )
        logger.info(
            "[pre] WhatsApp reminder sent for Group %d, Lecture #%d",
            group_number,
            lecture_number,
        )
    except Exception as exc:
        logger.error("[pre] WhatsApp reminder failed: %s", exc)
        try:
            alert_operator(
                f"WhatsApp reminder FAILED for Group {group_number}, "
                f"Lecture #{lecture_number}.\nError: {exc}"
            )
        except Exception:
            logger.error("[pre] alert_operator also failed for Group %d", group_number)

    # ---- Schedule post-meeting fallback job ----------------------------------
    # The primary trigger is now the meeting.ended webhook (in server.py).
    # This scheduler job is a SAFETY NET — it fires at 23:30 (T+210 min)
    # and only runs if the webhook didn't already start processing.
    poll_id = zoom_meeting_uuid or zoom_meeting_id
    if poll_id:
        _schedule_post_meeting(
            scheduler=_get_running_scheduler(),
            group_number=group_number,
            lecture_number=lecture_number,
            meeting_id=poll_id,
            fire_at_hour=23,
            fire_at_minute=30,  # 23:30 — safety net (webhook is primary)
        )
    else:
        logger.warning(
            "[pre] No meeting ID/UUID — post-meeting job will NOT be scheduled "
            "for Group %d, Lecture #%d",
            group_number,
            lecture_number,
        )


# ---------------------------------------------------------------------------
# Post-meeting job (bridges async scheduler → blocking pipeline thread)
# ---------------------------------------------------------------------------


async def post_meeting_job(group_number: int, lecture_number: int, meeting_id: str) -> None:
    """Kick off the post-meeting recording pipeline (FALLBACK path).

    This is a safety net — the primary trigger is the meeting.ended webhook
    in server.py. This job only fires at 23:30 and skips if the webhook
    already started processing.

    Args:
        group_number: 1 or 2.
        lecture_number: Lecture ordinal (passed from schedule time, not re-derived).
        meeting_id: Zoom meeting UUID (preferred) or numeric ID.
    """
    # Check if webhook already started processing this lecture.
    # Evict stale tasks first (mirrors server.py behavior) so a crashed
    # pipeline from >4 hours ago doesn't permanently block the fallback.
    try:
        from tools.app.server import _evict_stale_tasks, _processing_lock, _processing_tasks, _task_key
        _evict_stale_tasks()
        key = _task_key(group_number, lecture_number)
        with _processing_lock:
            if key in _processing_tasks:
                logger.info(
                    "[post] Scheduler FALLBACK skipped — %s already processing "
                    "(webhook handled it)",
                    key,
                )
                return
            # Also check persistent pipeline state
            if is_pipeline_active(group_number, lecture_number) or is_pipeline_done(group_number, lecture_number):
                logger.info("[post] Pipeline already active/complete for G%d L%d — skipping", group_number, lecture_number)
                return
            # CRITICAL: Set the dedup key BEFORE dispatching to the executor.
            # Atomic check-and-set under lock prevents webhook+scheduler race.
            _processing_tasks[key] = datetime.now()
        logger.info("[post] Scheduler FALLBACK claimed dedup key %s", key)
    except ImportError:
        pass  # server module not available (standalone scheduler mode)

    logger.info(
        "[post] Scheduler FALLBACK firing — webhook did not handle "
        "Group %d, Lecture #%d, Meeting %s",
        group_number,
        lecture_number,
        meeting_id,
    )

    loop = asyncio.get_running_loop()
    await asyncio.wait_for(
        loop.run_in_executor(
            None,
            _run_post_meeting_pipeline,
            group_number,
            lecture_number,
            meeting_id,
        ),
        timeout=4 * 3600,  # 4-hour absolute cap — prevents indefinite hang
    )


# ---------------------------------------------------------------------------
# Scheduler wiring helpers
# ---------------------------------------------------------------------------

# Module-level reference so pre_meeting_job can retrieve the running scheduler
# without receiving it as a parameter (APScheduler passes no extra args).
_scheduler_ref: AsyncIOScheduler | None = None


def _get_running_scheduler() -> AsyncIOScheduler:
    """Return the module-level scheduler reference.

    Raises:
        RuntimeError: If called before ``start_scheduler()`` is running.
    """
    if _scheduler_ref is None:
        raise RuntimeError("Scheduler has not been started yet.")
    return _scheduler_ref


def _schedule_post_meeting(
    scheduler: AsyncIOScheduler,
    group_number: int,
    lecture_number: int,
    meeting_id: str,
    fire_at_hour: int,
    fire_at_minute: int = 0,
) -> None:
    """Add a one-shot post-meeting job to the running scheduler.

    The job fires today at *fire_at_hour*:*fire_at_minute* Tbilisi time and is
    automatically removed after it runs (``misfire_grace_time`` of 30 min).

    Args:
        scheduler: The running ``AsyncIOScheduler`` instance.
        group_number: 1 or 2.
        lecture_number: Ordinal lecture number (for logging).
        meeting_id: Zoom meeting ID.
        fire_at_hour: Local hour (GMT+4) at which to fire.
        fire_at_minute: Local minute at which to fire (default 0).
    """
    now_tbilisi = datetime.now(TBILISI_TZ)
    fire_time = now_tbilisi.replace(
        hour=fire_at_hour, minute=fire_at_minute, second=0, microsecond=0
    )

    # If the fire time is in the past (e.g. scheduler started late), fire in
    # RECORDING_INITIAL_DELAY seconds from now instead.
    if fire_time <= now_tbilisi:
        fire_time = now_tbilisi + timedelta(seconds=RECORDING_INITIAL_DELAY)
        logger.warning(
            "[sched] Post-meeting fire time was in the past — rescheduled to %s",
            fire_time.isoformat(),
        )

    job_id = f"post_g{group_number}_l{lecture_number}_{meeting_id}"

    scheduler.add_job(
        post_meeting_job,
        trigger="date",
        run_date=fire_time,
        args=[group_number, lecture_number, meeting_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=30 * 60,  # tolerate up to 30 min late fire
    )
    # Persist to disk so restarts don't lose the scheduled job
    _save_pending_job(group_number, lecture_number, meeting_id, fire_time.isoformat())
    logger.info(
        "[sched] Post-meeting job '%s' scheduled at %s",
        job_id,
        fire_time.strftime("%Y-%m-%d %H:%M %Z"),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def start_scheduler() -> AsyncIOScheduler:
    """Build and start the AsyncIOScheduler with all recurring pre-meeting jobs.

    Recurring cron jobs:
        - Group 1: Tuesday (dow=tue) and Friday (dow=fri) at 18:00 Tbilisi
        - Group 2: Monday (dow=mon) and Thursday (dow=thu) at 18:00 Tbilisi

    Post-meeting jobs are added dynamically by ``pre_meeting_job()`` each time
    a meeting is created, so they carry the real Zoom meeting ID.

    Returns:
        The started ``AsyncIOScheduler`` (also stored in ``_scheduler_ref``).
    """
    global _scheduler_ref

    executors = {
        "default": AsyncIOExecutor(),
        "threadpool": ThreadPoolExecutor(max_workers=6),
    }
    job_defaults = {
        "coalesce": True,        # merge multiple misfired instances into one
        "max_instances": 1,      # never run the same job concurrently
        "misfire_grace_time": 55 * 60,  # 55 min — survive Railway restarts without silently dropping lectures
    }

    scheduler = AsyncIOScheduler(
        executors=executors,
        job_defaults=job_defaults,
        timezone=TBILISI_TZ,
    )

    # ------------------------------------------------------------------ #
    #  Group 1 — Tuesday (dow=1) and Friday (dow=4)                       #
    # ------------------------------------------------------------------ #
    scheduler.add_job(
        pre_meeting_job,
        trigger=CronTrigger(
            day_of_week="tue",
            hour=REMINDER_HOUR,
            minute=REMINDER_MINUTE,
            timezone=TBILISI_TZ,
        ),
        args=[1],
        id="pre_group1_tuesday",
        name="Pre-meeting: Group 1 (Tuesday)",
        replace_existing=True,
    )
    scheduler.add_job(
        pre_meeting_job,
        trigger=CronTrigger(
            day_of_week="fri",
            hour=REMINDER_HOUR,
            minute=REMINDER_MINUTE,
            timezone=TBILISI_TZ,
        ),
        args=[1],
        id="pre_group1_friday",
        name="Pre-meeting: Group 1 (Friday)",
        replace_existing=True,
    )

    # ------------------------------------------------------------------ #
    #  Group 2 — Monday (dow=0) and Thursday (dow=3)                      #
    # ------------------------------------------------------------------ #
    scheduler.add_job(
        pre_meeting_job,
        trigger=CronTrigger(
            day_of_week="mon",
            hour=REMINDER_HOUR,
            minute=REMINDER_MINUTE,
            timezone=TBILISI_TZ,
        ),
        args=[2],
        id="pre_group2_monday",
        name="Pre-meeting: Group 2 (Monday)",
        replace_existing=True,
    )
    scheduler.add_job(
        pre_meeting_job,
        trigger=CronTrigger(
            day_of_week="thu",
            hour=REMINDER_HOUR,
            minute=REMINDER_MINUTE,
            timezone=TBILISI_TZ,
        ),
        args=[2],
        id="pre_group2_thursday",
        name="Pre-meeting: Group 2 (Thursday)",
        replace_existing=True,
    )

    scheduler.start()
    _scheduler_ref = scheduler

    # Restore any post-meeting jobs that were lost during restart
    restored = _restore_pending_jobs(scheduler)

    logger.info("Scheduler started with %d jobs (%d restored from disk):",
                len(scheduler.get_jobs()), restored)
    for job in scheduler.get_jobs():
        logger.info("  [%s] %s — next: %s", job.id, job.name, job.next_run_time)

    return scheduler


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


async def _async_main() -> None:
    """Async main: start scheduler and block until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("Training Agent Scheduler starting...")
    logger.info("Timezone: Asia/Tbilisi (GMT+4)")
    logger.info("Pre-meeting jobs fire at %02d:%02d", REMINDER_HOUR, REMINDER_MINUTE)

    scheduler = start_scheduler()

    # Print a summary of upcoming job fires
    now = datetime.now(TBILISI_TZ)
    logger.info("Current Tbilisi time: %s", now.strftime("%Y-%m-%d %H:%M %Z"))

    try:
        # Keep the event loop alive; APScheduler drives itself via asyncio.
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler shutting down...")
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


def main() -> None:
    """Synchronous entry point for ``python -m tools.app.scheduler``."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
