"""Pre-meeting automation scheduler for Training Agent.

Orchestrates the full lifecycle for both training groups:
  - T-60 min: create Zoom meeting, send email + WhatsApp reminders
  - Post-meeting: download recording, upload to Drive, transcribe,
    generate summary Doc, run gap analysis, send private WhatsApp report.

Run:
    python -m tools.scheduler
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta
from typing import Any

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from tools.config import (
    GROUPS,
    TMP_DIR,
    TOTAL_LECTURES,
    get_lecture_folder_name,
    get_lecture_number,
)
from tools.whatsapp_sender import alert_operator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TBILISI_TZ = pytz.timezone("Asia/Tbilisi")
LECTURE_START_HOUR = 20       # 20:00 GMT+4
REMINDER_OFFSET_MINUTES = 60  # fire at T-60 (19:00)
REMINDER_HOUR = LECTURE_START_HOUR - (REMINDER_OFFSET_MINUTES // 60)  # 19
REMINDER_MINUTE = 0

# Recording polling: Zoom processes recordings 15–90 min after meeting ends.
# We wait RECORDING_INITIAL_DELAY before the first poll, then retry every
# RECORDING_POLL_INTERVAL for up to RECORDING_POLL_TIMEOUT seconds.
RECORDING_INITIAL_DELAY = 15 * 60       # 15 minutes
RECORDING_POLL_INTERVAL = 5 * 60        # 5 minutes between polls
RECORDING_POLL_TIMEOUT = 3 * 60 * 60    # 3 hours absolute deadline

# ---------------------------------------------------------------------------
# Lazy imports — zoom_manager and email_sender may not exist yet.
# All calls to these modules go through the helper below so the scheduler
# file is importable regardless.
# ---------------------------------------------------------------------------


def _import_zoom_manager():
    """Lazy-import tools.zoom_manager; raises ImportError with a clear message."""
    try:
        import tools.zoom_manager as zm
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


def check_recording_ready(meeting_id: str) -> dict[str, Any] | None:
    """Poll the Zoom API until a recording is available for *meeting_id*.

    Blocks the calling thread. Intended to be called via
    ``asyncio.get_event_loop().run_in_executor()``.

    Args:
        meeting_id: The Zoom meeting ID string.

    Returns:
        The first recording file dict from Zoom's API (contains
        ``download_url``, ``file_type``, etc.) or ``None`` if the timeout
        expires before a recording appears.

    Raises:
        ImportError: If ``tools.zoom_manager`` is not yet implemented.
    """
    zm = _import_zoom_manager()

    elapsed = 0
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
        except Exception as exc:
            logger.warning(
                "[recording] Poll error for meeting %s: %s — retrying in %d min",
                meeting_id,
                exc,
                RECORDING_POLL_INTERVAL // 60,
            )
            time.sleep(RECORDING_POLL_INTERVAL)
            elapsed += RECORDING_POLL_INTERVAL
            continue

        # Zoom returns a list of recording files; pick the main MP4.
        files: list[dict] = recordings.get("recording_files", [])
        mp4_files = [
            f for f in files
            if f.get("file_type", "").upper() in {"MP4", "VIDEO"}
            and f.get("status", "").upper() == "COMPLETED"
        ]

        if mp4_files:
            recording = mp4_files[0]
            logger.info(
                "[recording] Recording ready for meeting %s: %s",
                meeting_id,
                recording.get("download_url", "(no url)"),
            )
            return recording

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
    alert_operator(
        f"Recording NOT FOUND after {RECORDING_POLL_TIMEOUT // 60} min "
        f"for meeting {meeting_id}.\n"
        f"Check Zoom dashboard — manual processing may be needed."
    )
    return None


# ---------------------------------------------------------------------------
# Post-meeting pipeline (blocking — runs in thread executor)
# ---------------------------------------------------------------------------


def _run_post_meeting_pipeline(
    group_number: int,
    lecture_number: int,
    meeting_id: str,
) -> None:
    """Full post-meeting pipeline, executed in a background thread.

    Steps:
        1. Poll Zoom until recording is available.
        2. Download the recording to .tmp/.
        3. Upload recording to Google Drive (correct lecture folder).
        4. Delegate to transcribe_and_index() for the full analysis pipeline
           (transcribe → analyze → Drive summary + private report → WhatsApp → Pinecone).

    Args:
        group_number: 1 or 2.
        lecture_number: Ordinal lecture number (1–15).
        meeting_id: Zoom meeting ID used to poll for the recording.
    """
    from tools.gdrive_manager import ensure_folder, get_drive_service, upload_file
    from tools.transcribe_lecture import transcribe_and_index

    group = GROUPS[group_number]
    lecture_folder_name = get_lecture_folder_name(lecture_number)
    logger.info(
        "[post] Starting pipeline — Group %d, Lecture #%d, Meeting %s",
        group_number,
        lecture_number,
        meeting_id,
    )

    # ---- Step 1: Wait for recording ----------------------------------------
    recording = check_recording_ready(meeting_id)
    if not recording:
        logger.error(
            "[post] Aborting: no recording found for meeting %s", meeting_id
        )
        return

    # ---- Step 2: Download recording ----------------------------------------
    zm = _import_zoom_manager()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_filename = f"group{group_number}_lecture{lecture_number}_{timestamp}.mp4"
    local_path = TMP_DIR / local_filename

    logger.info("[post] Downloading recording to %s...", local_path)
    try:
        access_token = zm.get_access_token()
        download_url = recording.get("download_url", "")
        zm.download_recording(download_url, access_token, local_path)
    except Exception as exc:
        logger.error("[post] Download failed: %s", exc)
        alert_operator(
            f"Recording download FAILED for Group {group_number}, "
            f"Lecture #{lecture_number}.\nError: {exc}\n"
            f"Check Zoom dashboard for manual download."
        )
        return

    file_size_mb = local_path.stat().st_size / (1024 * 1024)
    logger.info("[post] Download complete: %.1f MB", file_size_mb)

    try:
        # ---- Step 3: Upload recording to Google Drive ----------------------
        logger.info("[post] Uploading recording to Google Drive...")
        service = get_drive_service()
        lecture_folder_id = ensure_folder(
            service,
            lecture_folder_name,
            group["drive_folder_id"],
        )
        upload_file(local_path, lecture_folder_id)
        logger.info("[post] Recording uploaded to Drive")

        # ---- Step 4: Full analysis pipeline --------------------------------
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
        # Always clean up the local temp file
        if local_path.exists():
            local_path.unlink()
            logger.info("[post] Cleaned up temp file: %s", local_path)


# ---------------------------------------------------------------------------
# Pre-meeting job (async — runs in the AsyncIOExecutor)
# ---------------------------------------------------------------------------


async def pre_meeting_job(group_number: int) -> None:
    """Create a Zoom meeting and send email + WhatsApp reminders.

    Fires at T-60 min (19:00 Tbilisi time) on each meeting day.
    Immediately schedules a post-meeting job on the scheduler for T+120 min
    (22:00) so recording polling starts right after the lecture ends.

    Args:
        group_number: 1 or 2.
    """
    today = date.today()
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
        logger.info(
            "[pre] Zoom meeting created: %s (ID: %s)",
            zoom_join_url,
            zoom_meeting_id,
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
        from tools.whatsapp_sender import send_group_reminder

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

    # ---- Schedule post-meeting job ------------------------------------------
    # Fire at 22:00 today (lecture end time) so that post-meeting processing
    # starts polling immediately after the session finishes.
    if zoom_meeting_id:
        _schedule_post_meeting(
            scheduler=_get_running_scheduler(),
            group_number=group_number,
            lecture_number=lecture_number,
            meeting_id=zoom_meeting_id,
            fire_at_hour=LECTURE_START_HOUR + 2,  # 22:00
        )
    else:
        logger.warning(
            "[pre] No meeting ID — post-meeting job will NOT be scheduled "
            "for Group %d, Lecture #%d",
            group_number,
            lecture_number,
        )


# ---------------------------------------------------------------------------
# Post-meeting job (bridges async scheduler → blocking pipeline thread)
# ---------------------------------------------------------------------------


async def post_meeting_job(group_number: int, meeting_id: str) -> None:
    """Kick off the post-meeting recording pipeline in a background thread.

    APScheduler calls this coroutine from the AsyncIOExecutor. We immediately
    hand off the blocking work to a ``ThreadPoolExecutor`` so the event loop
    stays free.

    Args:
        group_number: 1 or 2.
        meeting_id: Zoom meeting ID returned when the meeting was created.
    """
    today = date.today()
    lecture_number = get_lecture_number(group_number, for_date=today)

    logger.info(
        "[post] Dispatching post-meeting pipeline — Group %d, Lecture #%d, "
        "Meeting %s",
        group_number,
        lecture_number,
        meeting_id,
    )

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        _run_post_meeting_pipeline,
        group_number,
        lecture_number,
        meeting_id,
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
) -> None:
    """Add a one-shot post-meeting job to the running scheduler.

    The job fires today at *fire_at_hour*:00 Tbilisi time and is automatically
    removed after it runs (``misfire_grace_time`` of 30 min).

    Args:
        scheduler: The running ``AsyncIOScheduler`` instance.
        group_number: 1 or 2.
        lecture_number: Ordinal lecture number (for logging).
        meeting_id: Zoom meeting ID.
        fire_at_hour: Local hour (GMT+4) at which to fire.
    """
    now_tbilisi = datetime.now(TBILISI_TZ)
    fire_time = now_tbilisi.replace(
        hour=fire_at_hour, minute=0, second=0, microsecond=0
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
        args=[group_number, meeting_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=30 * 60,  # tolerate up to 30 min late fire
    )
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
        - Group 1: Tuesday (dow=tue) and Friday (dow=fri) at 19:00 Tbilisi
        - Group 2: Monday (dow=mon) and Thursday (dow=thu) at 19:00 Tbilisi

    Post-meeting jobs are added dynamically by ``pre_meeting_job()`` each time
    a meeting is created, so they carry the real Zoom meeting ID.

    Returns:
        The started ``AsyncIOScheduler`` (also stored in ``_scheduler_ref``).
    """
    global _scheduler_ref

    executors = {
        "default": AsyncIOExecutor(),
        "threadpool": ThreadPoolExecutor(max_workers=4),
    }
    job_defaults = {
        "coalesce": True,        # merge multiple misfired instances into one
        "max_instances": 1,      # never run the same job concurrently
        "misfire_grace_time": 10 * 60,  # 10 min tolerance for late fires
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

    logger.info("Scheduler started with %d recurring jobs:", len(scheduler.get_jobs()))
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
    """Synchronous entry point for ``python -m tools.scheduler``."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
