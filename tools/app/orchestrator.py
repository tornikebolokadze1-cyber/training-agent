"""Training Agent — unified orchestrator.

Starts the APScheduler and the FastAPI/uvicorn server together inside a single
asyncio event loop.  Both components share the same loop, so scheduled jobs can
safely interact with async FastAPI state without cross-thread coordination.

Run:
    python -m tools.orchestrator
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import logging.handlers
import signal
import sys
from datetime import datetime, timezone
from typing import Any

import uvicorn

from tools.app.server import app, verify_webhook_secret
from tools.core.config import (
    ANTHROPIC_API_KEY,
    GEMINI_API_KEY,
    GOOGLE_CREDENTIALS_PATH,
    GREEN_API_INSTANCE_ID,
    GREEN_API_TOKEN,
    N8N_CALLBACK_URL,
    PINECONE_API_KEY,
    PROJECT_ROOT,
    SERVER_HOST,
    SERVER_PORT,
    WEBHOOK_SECRET,
    ZOOM_ACCOUNT_ID,
    ZOOM_CLIENT_ID,
    ZOOM_CLIENT_SECRET,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Credential validation
# ---------------------------------------------------------------------------

# (env-var name, value-from-config, required?)
_CREDENTIALS: list[tuple[str, str, bool]] = [
    ("ZOOM_ACCOUNT_ID", ZOOM_ACCOUNT_ID, True),
    ("ZOOM_CLIENT_ID", ZOOM_CLIENT_ID, True),
    ("ZOOM_CLIENT_SECRET", ZOOM_CLIENT_SECRET, True),
    ("GEMINI_API_KEY", GEMINI_API_KEY, True),
    ("GREEN_API_INSTANCE_ID", GREEN_API_INSTANCE_ID, True),
    ("GREEN_API_TOKEN", GREEN_API_TOKEN, True),
    ("WEBHOOK_SECRET", WEBHOOK_SECRET, True),
    ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY, True),
    ("PINECONE_API_KEY", PINECONE_API_KEY, True),
    ("N8N_CALLBACK_URL", N8N_CALLBACK_URL, False),  # optional but warn
    ("GOOGLE_CREDENTIALS_PATH", GOOGLE_CREDENTIALS_PATH, False),
]


def validate_credentials() -> None:
    """Check that all required environment variables are present.

    Logs a warning for optional values that are missing and raises
    ``OSError`` immediately on the first missing required value.

    Raises:
        OSError: If one or more required credentials are absent.
    """
    missing_required: list[str] = []

    for name, value, required in _CREDENTIALS:
        if not value:
            if required:
                logger.error("Missing required credential: %s", name)
                missing_required.append(name)
            else:
                logger.warning("Optional credential not set: %s — some features may be disabled", name)
        else:
            logger.debug("Credential present: %s = [SET]", name)

    if missing_required:
        raise OSError(
            "Training Agent cannot start — the following required environment "
            f"variables are not set: {', '.join(missing_required)}\n"
            "Add them to your .env file and restart."
        )

    logger.info("All required credentials validated successfully.")


# ---------------------------------------------------------------------------
# Dedicated thread pool for pipeline work
# ---------------------------------------------------------------------------
# The default asyncio thread pool is shared with recording polling (which does
# long time.sleep calls). A dedicated executor prevents pipeline work from
# being starved when all default threads are blocked on polling sleeps.

PIPELINE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=3,
    thread_name_prefix="pipeline",
)


# ---------------------------------------------------------------------------
# DLQ handler registration and retry logic
# ---------------------------------------------------------------------------


def _register_dlq_handlers() -> None:
    """Register retry handlers for all DLQ operation types.

    Called once at startup. Each handler receives the payload dict from the
    DLQ entry and calls the real integration function. Exceptions propagate
    to the DLQ processor so it can track retry counts.
    """
    from tools.core.dlq import register_handler

    register_handler("drive_summary", _retry_drive_summary)
    register_handler("whatsapp_group", _retry_whatsapp_group)
    register_handler("pinecone_index", _retry_pinecone)
    logger.info("DLQ handlers registered: drive_summary, whatsapp_group, pinecone_index")


def _retry_drive_summary(payload: dict[str, Any]) -> None:
    """Retry uploading a lecture summary to Google Drive.

    Expected payload keys:
        title: str — Google Doc title
        content: str — summary text content
        folder_id: str — Drive folder ID to create the doc in
        group: int — group number (for logging)
        lecture: int — lecture number (for logging)
    """
    from tools.integrations.gdrive_manager import create_google_doc

    title = payload["title"]
    content = payload["content"]
    folder_id = payload["folder_id"]
    group = payload.get("group", "?")
    lecture = payload.get("lecture", "?")

    logger.info(
        "DLQ retry: Drive summary upload for G%s L%s — title=%r",
        group, lecture, title,
    )
    doc_id = create_google_doc(title, content, folder_id)
    logger.info(
        "DLQ retry SUCCESS: Drive summary uploaded — doc_id=%s (G%s L%s)",
        doc_id, group, lecture,
    )


def _retry_whatsapp_group(payload: dict[str, Any]) -> None:
    """Retry sending a WhatsApp group notification.

    Expected payload keys:
        group_number: int
        lecture_number: int
        drive_recording_url: str
        summary_doc_url: str
    """
    from tools.integrations.whatsapp_sender import send_group_upload_notification

    group_number = payload["group_number"]
    lecture_number = payload["lecture_number"]
    drive_recording_url = payload["drive_recording_url"]
    summary_doc_url = payload["summary_doc_url"]

    logger.info(
        "DLQ retry: WhatsApp group notification for G%d L%d",
        group_number, lecture_number,
    )
    send_group_upload_notification(
        group_number=group_number,
        lecture_number=lecture_number,
        drive_recording_url=drive_recording_url,
        summary_doc_url=summary_doc_url,
    )
    logger.info(
        "DLQ retry SUCCESS: WhatsApp group notification sent (G%d L%d)",
        group_number, lecture_number,
    )


def _retry_pinecone(payload: dict[str, Any]) -> None:
    """Retry indexing lecture content in Pinecone.

    Expected payload keys:
        group_number: int
        lecture_number: int
        content: str — text content to index
        content_type: str — e.g. "summary", "transcript", "report"
    """
    from tools.integrations.knowledge_indexer import index_lecture_content

    group_number = payload["group_number"]
    lecture_number = payload["lecture_number"]
    content = payload["content"]
    content_type = payload["content_type"]

    logger.info(
        "DLQ retry: Pinecone indexing for G%d L%d (%s, %d chars)",
        group_number, lecture_number, content_type, len(content),
    )
    count = index_lecture_content(
        group_number=group_number,
        lecture_number=lecture_number,
        content=content,
        content_type=content_type,
    )
    logger.info(
        "DLQ retry SUCCESS: Pinecone indexed %d vectors (G%d L%d, %s)",
        count, group_number, lecture_number, content_type,
    )


# ---------------------------------------------------------------------------
# /status endpoint — mounted on the imported FastAPI app
# ---------------------------------------------------------------------------

from fastapi import APIRouter, Header  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

_status_router = APIRouter()


@_status_router.get("/status", tags=["Orchestrator"])
async def status_endpoint(authorization: str | None = Header(None)) -> JSONResponse:
    """Return a unified health dashboard.

    Fields:
        uptime_seconds: Seconds since the orchestrator started.
        started_at: ISO-8601 UTC timestamp of orchestrator start.
        scheduler_state: "running" | "stopped" | "unavailable".
        scheduled_jobs: List of upcoming job details (id, name, next_run_time).
        last_execution_results: Most recent job execution records (if any).
        server: Basic server info.
    """
    verify_webhook_secret(authorization)
    state: dict[str, Any] = {}

    # --- uptime ---
    started_at: datetime | None = getattr(app.state, "started_at", None)
    if started_at is not None:
        now_utc = datetime.now(timezone.utc)
        state["uptime_seconds"] = round((now_utc - started_at).total_seconds(), 1)
        state["started_at"] = started_at.isoformat()
    else:
        state["uptime_seconds"] = None
        state["started_at"] = None

    # --- scheduler ---
    import tools.app.scheduler as _sched_mod

    scheduler = _sched_mod._scheduler_ref
    if scheduler is None:
        state["scheduler_state"] = "unavailable"
        state["scheduled_jobs"] = []
    else:
        state["scheduler_state"] = "running" if scheduler.running else "stopped"

        jobs_info: list[dict[str, Any]] = []
        for job in scheduler.get_jobs():
            next_run = job.next_run_time
            jobs_info.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run_time": next_run.isoformat() if next_run else None,
                    "trigger": str(job.trigger),
                }
            )
        # Sort: soonest first, None (date-triggered already fired) last
        jobs_info.sort(
            key=lambda j: (j["next_run_time"] is None, j["next_run_time"] or "")
        )
        state["scheduled_jobs"] = jobs_info

    # --- last execution results ---
    last_results: list[dict[str, Any]] = getattr(app.state, "last_execution_results", [])
    state["last_execution_results"] = last_results

    # --- server meta ---
    state["server"] = {
        "host": SERVER_HOST,
        "port": SERVER_PORT,
        "title": app.title,
        "version": app.version,
    }

    return JSONResponse(content=state)


app.include_router(_status_router)


# ---------------------------------------------------------------------------
# Startup / shutdown lifecycle hooks
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _on_startup() -> None:
    """Record startup time and clean up stale temp files from prior runs."""
    app.state.started_at = datetime.now(timezone.utc)
    app.state.last_execution_results = []

    # Detect whether .tmp/ survived the deploy (Railway persistent volume check)
    _check_tmp_persistence()

    # Clean up stale .tmp/ files from crashed/restarted pipelines
    _cleanup_stale_tmp_files()

    logger.info("FastAPI application started.")


def _check_tmp_persistence() -> None:
    """Warn if .tmp/ is ephemeral (Railway volume not configured).

    Writes a marker file on every startup.  If the marker already exists on
    the next startup it proves the directory survived the redeploy (i.e. a
    persistent volume is mounted).  If it is missing the budget counter and
    all pipeline state have been reset — which means daily budget limits are
    ineffective.
    """
    from datetime import datetime

    from tools.core.config import TMP_DIR

    marker = TMP_DIR / ".persistence_marker"
    if marker.exists():
        try:
            ts = marker.read_text(encoding="utf-8").strip()
            logger.info(
                "Persistent volume detected — .tmp/ survived since %s", ts
            )
        except Exception:
            pass
    else:
        logger.warning(
            ".tmp/ persistence marker not found — cost tracking and pipeline "
            "state may be LOST on redeploy. Configure a Railway volume mounted "
            "at /app/.tmp in Railway dashboard."
        )
        try:
            from tools.integrations.whatsapp_sender import alert_operator

            alert_operator(
                "\u26a0\ufe0f Railway .tmp/ volume not detected! Cost tracking resets "
                "on every deploy, making budget limits ineffective. "
                "Configure a persistent volume at /app/.tmp in Railway dashboard."
            )
        except Exception:
            pass

    # Always write/update the marker so the next startup can detect survival.
    try:
        marker.write_text(datetime.now().isoformat(), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not write .tmp/ persistence marker: %s", exc)


def _cleanup_stale_tmp_files() -> None:
    """Remove .mp4 files older than 6 hours from .tmp/ on startup.

    Prevents disk accumulation when Railway restarts mid-pipeline.
    """
    import time

    from tools.core.config import TMP_DIR

    stale_hours = 6
    cutoff = time.time() - stale_hours * 3600
    cleaned = 0

    for pattern in ("*.mp4", "*.chunk*.mp4"):
        for f in TMP_DIR.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    cleaned += 1
                    logger.info("Cleaned stale temp file: %s", f.name)
            except OSError as e:
                logger.warning("Failed to clean %s: %s", f.name, e)

    if cleaned:
        logger.info("Startup cleanup: removed %d stale temp file(s)", cleaned)


# ---------------------------------------------------------------------------
# Main orchestration entry point
# ---------------------------------------------------------------------------

async def _async_start() -> None:
    """Start APScheduler and uvicorn in the same asyncio event loop.

    Shutdown is triggered by SIGINT/SIGTERM — both components are shut down
    gracefully before the process exits.
    """
    # ---- DLQ handlers -------------------------------------------------------
    _register_dlq_handlers()

    # ---- Cleanup stale pipelines from prior crash/restart ------------------
    from tools.core.pipeline_state import cleanup_stale_pending

    stale_count = cleanup_stale_pending()
    if stale_count:
        logger.info(
            "Startup: marked %d stale pipeline(s) as FAILED.", stale_count
        )

    # ---- Cleanup orphaned Gemini files from prior crash/restart ----
    try:
        from tools.integrations.gemini_analyzer import cleanup_orphaned_gemini_files
        orphaned = cleanup_orphaned_gemini_files()
        if orphaned:
            logger.info("Startup: deleted %d orphaned Gemini file(s)", orphaned)
    except Exception as exc:
        logger.debug("Gemini file cleanup skipped: %s", exc)

    # ---- Cleanup old daily cost files (>30 days) -------------------------
    try:
        from tools.core.cost_tracker import cleanup_old_cost_files
        cleanup_old_cost_files(max_age_days=30)
    except Exception as exc:
        logger.debug("Cost file cleanup skipped: %s", exc)

    # ---- APScheduler --------------------------------------------------------
    from tools.app.scheduler import (
        start_scheduler,  # local import avoids circular ref at module level
    )

    logger.info("Starting APScheduler...")
    scheduler = start_scheduler()
    logger.info("APScheduler is running.")

    # ---- uvicorn ------------------------------------------------------------
    uvi_config = uvicorn.Config(
        app=app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        log_level="info",
        # Re-use the already-running event loop — critical so APScheduler and
        # uvicorn share the same loop.
        loop="none",
    )
    server = uvicorn.Server(uvi_config)

    # Install a clean shutdown handler so Ctrl-C / SIGTERM stops both.
    loop = asyncio.get_running_loop()

    def _handle_signal(sig: signal.Signals) -> None:
        logger.info("Received signal %s — initiating graceful shutdown...", sig.name)
        server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except (NotImplementedError, RuntimeError):
            # Windows or non-main-thread — fall back to default handling.
            pass

    logger.info(
        "Starting uvicorn on %s:%d ...",
        SERVER_HOST,
        SERVER_PORT,
    )

    try:
        await server.serve()
    finally:
        logger.info("uvicorn stopped. Shutting down scheduler...")
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped. Training Agent exiting.")


def start() -> None:
    """Validate credentials, then run the orchestrator.

    This is the primary public entry point.  It:
        1. Validates all required ``.env`` credentials (fail-fast).
        2. Launches APScheduler + FastAPI/uvicorn on a single asyncio event loop.

    Raises:
        OSError: If required credentials are missing.
        SystemExit: On unrecoverable startup failures.
    """
    _configure_logging()

    logger.info("=" * 60)
    logger.info("Training Agent — starting up")
    logger.info("=" * 60)

    try:
        validate_credentials()
    except OSError as exc:
        logger.critical("%s", exc)
        sys.exit(1)

    from tools.services.analytics import backfill_from_tmp, init_db, sync_from_pinecone
    init_db()
    backfill_result = backfill_from_tmp()
    if backfill_result["processed"] or backfill_result["failed"]:
        logger.info("Analytics backfill from .tmp/: %s", backfill_result)

    # Sync scores from Pinecone (persistent source of truth)
    sync_result = sync_from_pinecone(force=True)
    if sync_result.get("synced") or sync_result.get("failed"):
        logger.info("Pinecone sync on startup: %s", sync_result)

    try:
        asyncio.run(_async_start())
    except KeyboardInterrupt:
        # asyncio.run() surfaces KeyboardInterrupt after the loop exits;
        # shutdown was already handled inside _async_start via signal handler.
        logger.info("KeyboardInterrupt received. Goodbye.")


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """Set up structured logging for the entire application.

    Delegates to tools.core.logging_config which provides:
      - Local: human-readable format + rotating file handler
      - Railway: JSON lines on stdout for structured log ingestion
    """
    from tools.core.logging_config import configure_logging
    configure_logging(project_root=PROJECT_ROOT)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    start()
