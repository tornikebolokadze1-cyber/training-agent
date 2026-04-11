"""Admin API endpoints for manual pipeline management.

Provides operator-level curl commands for:
- Retrying specific lectures
- Resetting stuck pipelines
- Viewing lecture status across all groups
- Forcing Google OAuth token refresh
- Generating WhatsApp-friendly system reports

All endpoints require WEBHOOK_SECRET auth and are rate-limited to 5/min.

Note: server.py internals (_processing_lock, _processing_tasks, _task_key,
verify_webhook_secret) are imported lazily inside each endpoint function
to avoid circular imports (server.py imports admin_router at module level).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from tools.core.config import TBILISI_TZ
from tools.core.pipeline_state import (
    COMPLETE,
    FAILED,
    PENDING,
    create_pipeline,
    list_all_pipelines,
    load_state,
    mark_failed,
    reset_failed,
    state_file_path,
)

logger = logging.getLogger(__name__)

admin_router = APIRouter(prefix="/admin", tags=["Admin"])

NUM_GROUPS = 2
MAX_LECTURES = 15


def _server_internals() -> tuple:
    """Lazy import of server.py internals to avoid circular import.

    Returns:
        (verify_webhook_secret, _processing_lock, _processing_tasks, _task_key)
    """
    import tools.app.server as srv

    return (
        srv.verify_webhook_secret,
        srv._processing_lock,
        srv._processing_tasks,
        srv._task_key,
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class LectureRequest(BaseModel):
    """Request body identifying a specific group + lecture."""

    group_number: int
    lecture_number: int

    @field_validator("group_number")
    @classmethod
    def validate_group(cls, v: int) -> int:
        if v not in (1, 2):
            raise ValueError("group_number must be 1 or 2")
        return v

    @field_validator("lecture_number")
    @classmethod
    def validate_lecture(cls, v: int) -> int:
        if not 1 <= v <= MAX_LECTURES:
            raise ValueError(f"lecture_number must be between 1 and {MAX_LECTURES}")
        return v


# ---------------------------------------------------------------------------
# 1. POST /admin/retry-lecture
# ---------------------------------------------------------------------------


@admin_router.post("/retry-lecture")
async def retry_lecture(
    request: Request,
    body: LectureRequest,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Retry processing for a specific lecture.

    Resets the pipeline state (if FAILED or COMPLETE) and starts
    the pipeline in background. Returns immediately.
    """
    verify_webhook_secret, _processing_lock, _processing_tasks, _task_key = (
        _server_internals()
    )
    verify_webhook_secret(authorization)

    group = body.group_number
    lecture = body.lecture_number

    # Check current state
    existing = load_state(group, lecture)
    previous_state = existing.state if existing else "NONE"

    # Clear any existing state so pipeline can restart
    if existing:
        if existing.state == FAILED:
            reset_failed(group, lecture)
        elif existing.state == COMPLETE:
            path = state_file_path(group, lecture)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Failed to remove state file for retry: %s", exc)
        elif existing.state not in (FAILED, COMPLETE, PENDING):
            raise HTTPException(
                status_code=409,
                detail=f"Pipeline is currently active (state={existing.state}). "
                "Use /admin/reset-pipeline first to force-clear it.",
            )

    # Clear dedup key
    key = _task_key(group, lecture)
    with _processing_lock:
        _processing_tasks.pop(key, None)

    # Start pipeline in background
    from tools.app.scheduler import _run_post_meeting_pipeline

    loop = asyncio.get_running_loop()
    with _processing_lock:
        _processing_tasks[key] = datetime.now(tz=TBILISI_TZ)
        try:
            create_pipeline(group, lecture)
        except ValueError:
            pass  # State file already exists — fine

    loop.run_in_executor(
        None,
        lambda: _run_post_meeting_pipeline(
            group,
            lecture,
            "",
            skip_initial_delay=True,
        ),
    )

    logger.info(
        "Admin retry started: G%d L%d (previous_state=%s)",
        group,
        lecture,
        previous_state,
    )

    return JSONResponse(
        content={
            "status": "started",
            "group": group,
            "lecture": lecture,
            "previous_state": previous_state,
        }
    )


# ---------------------------------------------------------------------------
# 2. POST /admin/reset-pipeline
# ---------------------------------------------------------------------------


@admin_router.post("/reset-pipeline")
async def reset_pipeline(
    request: Request,
    body: LectureRequest,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Reset a stuck pipeline without starting processing.

    Marks the pipeline as FAILED, clears the dedup key, and removes
    the state file. Does NOT start processing — just unblocks.
    """
    verify_webhook_secret, _processing_lock, _processing_tasks, _task_key = (
        _server_internals()
    )
    verify_webhook_secret(authorization)

    group = body.group_number
    lecture = body.lecture_number

    existing = load_state(group, lecture)
    if existing is None:
        return JSONResponse(
            content={
                "status": "no_pipeline",
                "group": group,
                "lecture": lecture,
                "message": "No pipeline state found for this lecture.",
            }
        )

    previous_state = existing.state

    # If not already terminal, mark as FAILED first
    if previous_state not in (FAILED, COMPLETE):
        mark_failed(existing, f"Admin force-reset from state={previous_state}")

    # Remove the state file
    path = state_file_path(group, lecture)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Failed to remove state file during reset: %s", exc)

    # Clear dedup key
    key = _task_key(group, lecture)
    with _processing_lock:
        _processing_tasks.pop(key, None)

    logger.info(
        "Admin reset: G%d L%d (previous_state=%s)",
        group,
        lecture,
        previous_state,
    )

    return JSONResponse(
        content={
            "status": "reset",
            "group": group,
            "lecture": lecture,
            "previous_state": previous_state,
        }
    )


# ---------------------------------------------------------------------------
# 3. GET /admin/lecture-status
# ---------------------------------------------------------------------------


def _get_lecture_status(group: int, lecture: int) -> dict[str, Any]:
    """Build status dict for a single lecture."""
    state = load_state(group, lecture)

    if state is None:
        return {
            "group": group,
            "lecture": lecture,
            "pipeline_state": "UNKNOWN",
            "pinecone_indexed": False,
            "pinecone_vectors": 0,
            "drive_video_id": "",
            "summary_doc_id": "",
            "report_doc_id": "",
            "last_error": "",
            "updated_at": "",
        }

    return {
        "group": group,
        "lecture": lecture,
        "pipeline_state": state.state,
        "pinecone_indexed": state.pinecone_indexed,
        "pinecone_vectors": 0,
        "drive_video_id": state.drive_video_id,
        "summary_doc_id": state.summary_doc_id,
        "report_doc_id": state.report_doc_id,
        "last_error": state.error,
        "updated_at": state.updated_at,
        "retry_count": state.retry_count,
        "cost_estimate_usd": state.cost_estimate_usd,
    }


@admin_router.get("/lecture-status")
async def lecture_status(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Full status for all lectures across both groups.

    Returns pipeline state, Pinecone indexing, Drive files,
    and error info for each lecture.
    """
    verify_webhook_secret = _server_internals()[0]
    verify_webhook_secret(authorization)

    results: dict[str, list[dict[str, Any]]] = {}

    for group_num in range(1, NUM_GROUPS + 1):
        group_statuses: list[dict[str, Any]] = []
        for lec in range(1, MAX_LECTURES + 1):
            status = _get_lecture_status(group_num, lec)
            group_statuses.append(status)
        results[f"group_{group_num}"] = group_statuses

    # Try to enrich with Pinecone vector counts (non-fatal)
    try:
        pinecone_counts = await _get_pinecone_counts()
        for lectures in results.values():
            for lec_status in lectures:
                pc_key = (lec_status["group"], lec_status["lecture"])
                if pc_key in pinecone_counts:
                    lec_status["pinecone_indexed"] = True
                    lec_status["pinecone_vectors"] = pinecone_counts[pc_key]
    except Exception as exc:
        logger.warning("Pinecone enrichment failed (non-fatal): %s", exc)

    # Summary counts
    all_lectures: list[dict[str, Any]] = []
    for lectures in results.values():
        all_lectures.extend(lectures)

    summary = {
        "total_lectures": len(all_lectures),
        "complete": sum(1 for s in all_lectures if s["pipeline_state"] == COMPLETE),
        "failed": sum(1 for s in all_lectures if s["pipeline_state"] == FAILED),
        "active": sum(
            1
            for s in all_lectures
            if s["pipeline_state"] not in (COMPLETE, FAILED, "UNKNOWN", PENDING)
        ),
        "pending": sum(1 for s in all_lectures if s["pipeline_state"] == PENDING),
        "unknown": sum(1 for s in all_lectures if s["pipeline_state"] == "UNKNOWN"),
    }

    return JSONResponse(
        content={
            "summary": summary,
            "groups": results,
            "timestamp": datetime.now(TBILISI_TZ).isoformat(),
        }
    )


async def _get_pinecone_counts() -> dict[tuple[int, int], int]:
    """Query Pinecone for vector counts per group+lecture.

    Returns dict mapping (group, lecture) -> vector count.
    Non-fatal: returns empty dict on any error.
    """
    counts: dict[tuple[int, int], int] = {}

    # Use get_lecture_vector_count (ID-prefix scan) instead of
    # index.query(vector=[0.0]*3072, filter=...) which returns 0 matches
    # with cosine metric — making the admin dashboard falsely show all
    # lectures as "missing".
    try:
        from tools.integrations.knowledge_indexer import get_lecture_vector_count

        for group_num in range(1, NUM_GROUPS + 1):
            for lec in range(1, MAX_LECTURES + 1):
                count = await asyncio.to_thread(
                    get_lecture_vector_count, group_num, lec,
                )
                if count:
                    counts[(group_num, lec)] = count
    except Exception as exc:
        logger.warning("Pinecone count query failed: %s", exc)

    return counts


# ---------------------------------------------------------------------------
# 4. POST /admin/force-refresh-token
# ---------------------------------------------------------------------------


@admin_router.post("/force-refresh-token")
async def force_refresh_token(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Force a Google OAuth2 token refresh.

    Calls get_drive_service() which triggers credential refresh internally.
    Useful when the cached token has expired.
    """
    verify_webhook_secret = _server_internals()[0]
    verify_webhook_secret(authorization)

    try:
        from tools.integrations.gdrive_manager import get_drive_service

        service = await asyncio.to_thread(get_drive_service)

        # Verify the refreshed service works
        about = await asyncio.to_thread(
            lambda: service.about().get(fields="user").execute()
        )
        user_email = about.get("user", {}).get("emailAddress", "unknown")

        logger.info("Admin: Google token refreshed (user=%s)", user_email)

        return JSONResponse(
            content={
                "status": "refreshed",
                "google_user": user_email,
                "message": "Google OAuth token refreshed and verified.",
            }
        )

    except Exception as exc:
        logger.error("Admin: Google token refresh failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=f"Token refresh failed: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# 5. GET /admin/system-report
# ---------------------------------------------------------------------------


@admin_router.get("/system-report")
async def system_report(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Generate a text report suitable for WhatsApp.

    Includes system uptime, active pipelines, per-lecture status,
    and recent errors.
    """
    verify_webhook_secret, _processing_lock, _processing_tasks, _ = _server_internals()
    verify_webhook_secret(authorization)

    import tools.app.server as srv

    lines: list[str] = []
    lines.append("=== Training Agent System Report ===")
    lines.append(f"Time: {datetime.now(TBILISI_TZ).strftime('%Y-%m-%d %H:%M')}")

    # Uptime
    started_at: datetime | None = getattr(srv.app.state, "started_at", None)
    if started_at is not None:
        uptime_sec = (datetime.now(timezone.utc) - started_at).total_seconds()
        hours = int(uptime_sec // 3600)
        minutes = int((uptime_sec % 3600) // 60)
        lines.append(f"Uptime: {hours}h {minutes}m")
    else:
        lines.append("Uptime: unknown")

    # Active pipelines
    all_pipelines = list_all_pipelines()
    active = [p for p in all_pipelines if p.state not in (COMPLETE, FAILED, "UNKNOWN")]
    lines.append(f"\nActive pipelines: {len(active)}")
    for p in active:
        lines.append(f"  G{p.group} L{p.lecture}: {p.state}")

    # In-flight tasks
    with _processing_lock:
        task_count = len(_processing_tasks)
    lines.append(f"In-flight dedup tasks: {task_count}")

    # Per-group lecture matrix
    for group_num in range(1, NUM_GROUPS + 1):
        lines.append(f"\n--- Group {group_num} ---")
        row: list[str] = []
        for lec in range(1, MAX_LECTURES + 1):
            state = load_state(group_num, lec)
            if state is None:
                row.append(f"L{lec}:--")
            elif state.state == COMPLETE:
                row.append(f"L{lec}:OK")
            elif state.state == FAILED:
                row.append(f"L{lec}:FAIL")
            else:
                row.append(f"L{lec}:{state.state[:4]}")
        for i in range(0, len(row), 5):
            lines.append("  " + " | ".join(row[i : i + 5]))

    # Recent errors (last 24h)
    recent_errors: list[str] = []
    now = datetime.now(tz=TBILISI_TZ)
    for p in all_pipelines:
        if p.state == FAILED and p.error:
            try:
                updated = datetime.fromisoformat(p.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=TBILISI_TZ)
                age_hours = (now - updated).total_seconds() / 3600.0
                if age_hours <= 24:
                    recent_errors.append(f"  G{p.group} L{p.lecture}: {p.error[:80]}")
            except (ValueError, TypeError):
                recent_errors.append(f"  G{p.group} L{p.lecture}: {p.error[:80]}")

    if recent_errors:
        lines.append(f"\nRecent errors ({len(recent_errors)}):")
        lines.extend(recent_errors)
    else:
        lines.append("\nNo errors in last 24h.")

    lines.append("\nWEBHOOK_SECRET: configured")
    lines.append("=== End Report ===")

    report_text = "\n".join(lines)

    return JSONResponse(
        content={
            "report": report_text,
            "timestamp": datetime.now(TBILISI_TZ).isoformat(),
            "active_pipelines": len(active),
            "recent_errors": len(recent_errors),
        }
    )


# ---------------------------------------------------------------------------
# POST /admin/backfill-deep-analysis
# ---------------------------------------------------------------------------


class BackfillRequest(BaseModel):
    """Optional request body for the backfill endpoint.

    lectures      — list of keys like ["g1_l1", "g2_l5"] to deep-analyse only
                    (skipped if deep_analysis already indexed).  Omit for auto-detect.
    reprocess     — list of keys like ["g2_l9"] to run FULL analysis on
                    (summary + gap + deep), overwriting whatever is already indexed.
    full_rebuild  — list of keys like ["g1_l3"] to download from Zoom, re-transcribe,
                    and regenerate ALL analysis (summary + gap + deep) without WhatsApp.
                    Use when Pinecone transcript is missing/empty (e.g. old lectures).
    """

    lectures: list[str] = []
    reprocess: list[str] = []
    full_rebuild: list[str] = []


def _parse_lecture_key(key: str) -> tuple[int, int]:
    """Parse 'g1_l3' into (group=1, lecture=3).

    Raises:
        ValueError: If the format is not 'g{int}_l{int}'.
    """
    try:
        parts = key.split("_")
        group = int(parts[0][1:])
        lecture = int(parts[1][1:])
        if group not in (1, 2) or not (1 <= lecture <= MAX_LECTURES):
            raise ValueError
        return group, lecture
    except (IndexError, ValueError) as exc:
        raise ValueError(
            f"Invalid lecture key '{key}'. Expected format: g1_l3 (group 1-2, lecture 1-15)"
        ) from exc


def _reconstruct_from_pinecone(
    idx: Any,
    group: int,
    lecture: int,
    content_type: str,
) -> str:
    """Fetch all chunks for a lecture+content_type from Pinecone and join them.

    Args:
        idx: A live Pinecone Index object.
        group: Group number (1 or 2).
        lecture: Lecture number (1-15).
        content_type: One of 'transcript', 'summary', 'gap_analysis', 'deep_analysis'.

    Returns:
        Reconstructed full text, or empty string if no chunks found.
    """
    prefix = f"g{group}_l{lecture}_{content_type}_"
    all_ids: list[str] = []
    try:
        for page in idx.list(prefix=prefix, limit=1000):
            if isinstance(page, dict):
                all_ids.extend(page.get("vectors", []))
            elif isinstance(page, list):
                all_ids.extend(page)
            else:
                # Some SDK versions yield individual ID strings
                all_ids.append(str(page))
    except Exception as exc:
        logger.warning(
            "_reconstruct_from_pinecone: list() failed for %s: %s", prefix, exc
        )
        return ""

    if not all_ids:
        logger.debug("No vectors found for prefix '%s'", prefix)
        return ""

    try:
        fetched = idx.fetch(ids=all_ids)
        raw_vectors = (
            fetched.get("vectors", {}) if isinstance(fetched, dict)
            else getattr(fetched, "vectors", {})
        )
    except Exception as exc:
        logger.warning(
            "_reconstruct_from_pinecone: fetch() failed for %s: %s", prefix, exc
        )
        return ""

    chunks: list[tuple[int, str]] = []
    for _vid, vec in raw_vectors.items():
        meta = (
            vec.get("metadata", {}) if isinstance(vec, dict)
            else getattr(vec, "metadata", {})
        )
        chunk_index = meta.get("chunk_index", 0)
        text = meta.get("text", "")
        chunks.append((chunk_index, text))

    chunks.sort(key=lambda x: x[0])
    return "\n".join(t for _, t in chunks if t)


def _read_lecture_context_from_drive(group: int, lecture: int) -> tuple[str, str]:
    """Read existing summary + gap_analysis from Google Drive for a lecture.

    Falls back to Drive-stored docs when a lecture was indexed before the
    ``"text"`` metadata field was added to Pinecone (G1 L1-4, G2 L1-5).

    Args:
        group: Group number (1 or 2).
        lecture: Lecture number (1-15).

    Returns:
        (summary_text, gap_analysis_text) — either may be empty if not found.
    """
    try:
        from tools.integrations.gdrive_manager import get_drive_service
        from tools.core.config import GROUPS

        svc = get_drive_service()

        # Locate the ლექცია #N subfolder inside the shared group folder
        group_folder_id = GROUPS[group]["drive_folder_id"]
        lecture_query = (
            f"'{group_folder_id}' in parents "
            f"and name contains 'ლექცია #{lecture}' "
            f"and mimeType='application/vnd.google-apps.folder' "
            f"and trashed=false"
        )
        folders = (
            svc.files()
            .list(q=lecture_query, fields="files(id, name)")
            .execute()
            .get("files", [])
        )

        if not folders:
            logger.warning(
                "[backfill] No lecture folder found for G%d L%d in Drive", group, lecture
            )
            return ("", "")

        lecture_folder_id = folders[0]["id"]

        # Find the summary Google Doc inside the lecture folder
        doc_query = (
            f"'{lecture_folder_id}' in parents "
            f"and mimeType='application/vnd.google-apps.document' "
            f"and trashed=false"
        )
        docs = (
            svc.files()
            .list(q=doc_query, fields="files(id, name)")
            .execute()
            .get("files", [])
        )

        summary_text = ""
        for doc in docs:
            doc_name = doc["name"].lower()
            if "შეჯამება" in doc["name"] or "summary" in doc_name:
                content = svc.files().export(
                    fileId=doc["id"], mimeType="text/plain"
                ).execute()
                if isinstance(content, bytes):
                    summary_text = content.decode("utf-8", errors="replace")
                else:
                    summary_text = str(content)
                logger.info(
                    "[backfill] Read summary doc '%s' for G%d L%d (%d chars)",
                    doc["name"], group, lecture, len(summary_text),
                )
                break

        # Gap analysis lives in the private analysis folder
        gap_text = ""
        private_folder_id = GROUPS[group]["analysis_folder_id"]
        if private_folder_id:
            gap_query = (
                f"'{private_folder_id}' in parents "
                f"and mimeType='application/vnd.google-apps.document' "
                f"and name contains 'ლექცია #{lecture}' "
                f"and trashed=false"
            )
            analysis_docs = (
                svc.files()
                .list(q=gap_query, fields="files(id, name)")
                .execute()
                .get("files", [])
            )
            for doc in analysis_docs:
                try:
                    content = svc.files().export(
                        fileId=doc["id"], mimeType="text/plain"
                    ).execute()
                    if isinstance(content, bytes):
                        gap_text = content.decode("utf-8", errors="replace")
                    else:
                        gap_text = str(content)
                    logger.info(
                        "[backfill] Read analysis doc '%s' for G%d L%d (%d chars)",
                        doc["name"], group, lecture, len(gap_text),
                    )
                    break
                except Exception as doc_exc:
                    logger.warning(
                        "[backfill] Could not export analysis doc '%s': %s",
                        doc["name"], doc_exc,
                    )
                    continue

        return (summary_text, gap_text)

    except Exception as exc:
        logger.warning(
            "[backfill] Failed to read Drive context for G%d L%d: %s", group, lecture, exc
        )
        return ("", "")


def _calculate_lecture_date(group: int, lecture: int):  # -> date | None
    """Calculate the calendar date when a lecture happened.

    Group 1: Tuesdays and Fridays, started 2026-03-13 (Friday).
    Group 2: Thursdays and Mondays, started 2026-03-12 (Thursday).

    Args:
        group: Group number (1 or 2).
        lecture: Lecture number (1-15).

    Returns:
        The date on which that lecture occurred, or None for invalid inputs.
    """
    from datetime import date, timedelta

    if group == 1:
        start = date(2026, 3, 13)  # First lecture: Friday
        weekdays = {4, 1}          # Fri=4, Tue=1
    elif group == 2:
        start = date(2026, 3, 12)  # First lecture: Thursday
        weekdays = {3, 0}          # Thu=3, Mon=0
    else:
        return None

    count = 0
    d = start
    while count < 15:
        if d.weekday() in weekdays:
            count += 1
            if count == lecture:
                return d
        d = d + timedelta(days=1)
    return None


def _download_zoom_recording_for_lecture(group: int, lecture: int):  # -> Path | None
    """Find and download the Zoom recording for a specific lecture.

    Queries Zoom recordings within ±1 day of the calculated lecture date, finds
    the MP4 file, and downloads it to the .tmp/ directory.

    Args:
        group: Group number (1 or 2).
        lecture: Lecture number (1-15).

    Returns:
        Path to the downloaded MP4 file, or None if the recording is not available.
    """
    from datetime import timedelta

    from tools.integrations.zoom_manager import (
        download_recording,
        get_access_token,
        list_user_recordings,
    )
    from tools.core.config import TMP_DIR

    try:
        lecture_date = _calculate_lecture_date(group, lecture)
        if not lecture_date:
            logger.warning(
                "[backfill] Could not calculate date for G%d L%d", group, lecture
            )
            return None

        from_date = (lecture_date - timedelta(days=1)).strftime("%Y-%m-%d")
        to_date = (lecture_date + timedelta(days=1)).strftime("%Y-%m-%d")

        recordings = list_user_recordings(from_date=from_date, to_date=to_date)
        if not recordings:
            logger.warning(
                "[backfill] No Zoom recordings found for G%d L%d (%s to %s)",
                group, lecture, from_date, to_date,
            )
            return None

        # Find a recording whose start date matches the lecture date
        lecture_date_str = lecture_date.strftime("%Y-%m-%d")
        for meeting in recordings:
            start_time = meeting.get("start_time", "")
            if lecture_date_str not in start_time:
                continue

            files = meeting.get("recording_files", [])
            mp4_file = next(
                (f for f in files if f.get("file_type") == "MP4"),
                None,
            )
            if not mp4_file:
                logger.debug(
                    "[backfill] Meeting %s has no MP4 file — skipping",
                    meeting.get("id"),
                )
                continue

            output_path = TMP_DIR / f"g{group}_l{lecture}_backfill.mp4"
            logger.info(
                "[backfill] Downloading Zoom recording for G%d L%d (meeting %s)...",
                group, lecture, meeting.get("id"),
            )
            token = get_access_token()
            download_recording(
                download_url=mp4_file["download_url"],
                access_token=token,
                dest_path=output_path,
            )
            size_mb = output_path.stat().st_size // (1024 * 1024)
            logger.info(
                "[backfill] Downloaded %d MB for G%d L%d", size_mb, group, lecture
            )
            return output_path

        logger.warning(
            "[backfill] No matching Zoom recording for G%d L%d on %s",
            group, lecture, lecture_date_str,
        )
        return None

    except Exception as exc:
        logger.error(
            "[backfill] Zoom download failed for G%d L%d: %s",
            group, lecture, exc, exc_info=True,
        )
        return None


def _run_backfill_sync(
    lectures_to_deep: list[str],
    lectures_to_reprocess: list[str],
    lectures_to_full_rebuild: list[str] | None = None,
) -> dict[str, Any]:
    """Execute backfill operations (synchronous — call via asyncio.to_thread).

    deep-only path   : reconstruct transcript → Claude deep_analysis only →
                       Gemini Georgian writing → Drive private report → Pinecone index.
    reprocess path   : reconstruct transcript → full Claude (summary+gap+deep) →
                       Gemini Georgian for all 3 → Drive report → Pinecone index.
    full-rebuild path: download recording from Zoom → run full transcribe_and_index
                       pipeline with silent=True (no WhatsApp) → clean up video file.

    NEVER sends WhatsApp messages.

    Args:
        lectures_to_deep: Keys like ['g1_l1'] that need deep_analysis only.
        lectures_to_reprocess: Keys like ['g2_l9'] that need full re-analysis.
        lectures_to_full_rebuild: Keys like ['g1_l3'] that require a full Zoom
            download + re-transcription + full analysis regeneration.

    Returns:
        Dict with 'results' list and summary counts.
    """
    from tools.integrations.knowledge_indexer import (
        get_pinecone_index,
        index_lecture_content,
        lecture_exists_in_index,
    )
    from tools.integrations.gemini_analyzer import (
        _claude_reason_all,
        _safe_gemini_write_georgian,
        _pipeline_key_var,
    )
    from tools.core.config import (
        DEEP_ANALYSIS_PROMPT,
        GAP_ANALYSIS_PROMPT,
        SUMMARIZATION_PROMPT,
    )
    from tools.services.transcribe_lecture import _upload_private_report_to_drive

    results: list[dict[str, Any]] = []
    ok_count = 0
    skip_count = 0
    fail_count = 0

    try:
        idx = get_pinecone_index()
    except Exception as exc:
        logger.error("[backfill] Cannot connect to Pinecone: %s", exc)
        return {
            "results": [{"status": "FATAL", "reason": f"Pinecone unavailable: {exc}"}],
            "ok": 0,
            "skipped": 0,
            "failed": 1,
        }

    # ------------------------------------------------------------------ #
    # 1. Deep-analysis-only path
    # ------------------------------------------------------------------ #
    for key in lectures_to_deep:
        try:
            group, lecture = _parse_lecture_key(key)
        except ValueError as exc:
            results.append({"lecture": key, "status": "FAIL", "reason": str(exc)})
            fail_count += 1
            continue

        logger.info("[backfill] deep-only: %s", key)

        # Skip if already indexed (use force=True in reprocess path instead)
        if lecture_exists_in_index(group, lecture, "deep_analysis"):
            logger.info("[backfill] %s already has deep_analysis — skipping", key)
            results.append({"lecture": key, "status": "SKIP", "reason": "already indexed"})
            skip_count += 1
            continue

        # Try Pinecone first (works for lectures with text metadata)
        transcript = _reconstruct_from_pinecone(idx, group, lecture, "transcript")

        # If transcript unavailable, fall back to Drive-based context
        use_drive_context = False
        if not transcript or len(transcript) < 500:
            logger.info(
                "[backfill] %s: transcript unavailable in Pinecone (%d chars), "
                "trying Drive context...",
                key, len(transcript),
            )
            summary_text, gap_text = _read_lecture_context_from_drive(group, lecture)
            if summary_text or gap_text:
                # Use summary + gap as context instead of full transcript
                transcript = (
                    f"[შეჯამება]\n{summary_text}\n\n[ხარვეზების ანალიზი]\n{gap_text}"
                )
                use_drive_context = True
                logger.info(
                    "[backfill] %s: using Drive context "
                    "(summary=%d chars, gap=%d chars, combined=%d chars)",
                    key, len(summary_text), len(gap_text), len(transcript),
                )
            else:
                reason = "Neither Pinecone transcript nor Drive context available"
                logger.warning("[backfill] %s: %s", key, reason)
                results.append({"lecture": key, "status": "SKIP", "reason": reason})
                skip_count += 1
                continue

        if use_drive_context:
            logger.info(
                "[backfill] %s: generating deep_analysis from Drive context", key
            )

        try:
            _pipeline_key_var.set(f"g{group}_l{lecture}")

            claude_sections = _claude_reason_all(transcript)
            deep_text_en = claude_sections.get("deep_analysis", "")
            if not deep_text_en:
                results.append({
                    "lecture": key,
                    "status": "FAIL",
                    "reason": "Claude returned empty deep_analysis section",
                })
                fail_count += 1
                continue

            georgian_deep = _safe_gemini_write_georgian(
                deep_text_en, DEEP_ANALYSIS_PROMPT, "deep analysis"
            )
            if not georgian_deep:
                results.append({
                    "lecture": key,
                    "status": "FAIL",
                    "reason": "Gemini Georgian writing returned empty string for deep_analysis",
                })
                fail_count += 1
                continue

            # Fetch existing gap analysis for the private Drive report.
            # When Drive context was used, the gap text was already read from
            # Drive; extract it from the combined transcript string to avoid a
            # redundant API call.  For Pinecone-sourced lectures, query Pinecone.
            if use_drive_context:
                # Extract gap portion from the Drive context we built earlier.
                # Format: "[შეჯამება]\n<summary>\n\n[ხარვეზების ანალიზი]\n<gap>"
                gap_marker = "[ხარვეზების ანალიზი]\n"
                gap_marker_pos = transcript.find(gap_marker)
                gap_text_existing = (
                    transcript[gap_marker_pos + len(gap_marker):]
                    if gap_marker_pos != -1
                    else ""
                )
            else:
                gap_text_existing = _reconstruct_from_pinecone(
                    idx, group, lecture, "gap_analysis"
                )

            # Upload combined gap+deep report to private Drive folder (no WhatsApp)
            try:
                _upload_private_report_to_drive(
                    group_number=group,
                    lecture_number=lecture,
                    gap_analysis=gap_text_existing or "(gap analysis not available)",
                    deep_analysis=georgian_deep,
                )
            except Exception as drive_exc:
                logger.warning(
                    "[backfill] %s: Drive upload failed (non-fatal): %s", key, drive_exc
                )

            # Index in Pinecone (force=True to overwrite any partial stale vectors)
            vec_count = index_lecture_content(
                group_number=group,
                lecture_number=lecture,
                content=georgian_deep,
                content_type="deep_analysis",
                force=True,
            )

            results.append({
                "lecture": key,
                "status": "OK",
                "mode": "deep_only",
                "vectors_indexed": vec_count,
            })
            ok_count += 1
            logger.info("[backfill] %s: deep_analysis done (%d vectors)", key, vec_count)

        except RuntimeError as exc:
            if "budget" in str(exc).lower():
                logger.error("[backfill] %s: budget exceeded — stopping", key)
                results.append({"lecture": key, "status": "FAIL", "reason": f"budget exceeded: {exc}"})
                fail_count += 1
                break  # Stop further processing — budget is shared
            logger.error("[backfill] %s: runtime error: %s", key, exc, exc_info=True)
            results.append({"lecture": key, "status": "FAIL", "reason": str(exc)})
            fail_count += 1
        except Exception as exc:
            logger.error("[backfill] %s: unexpected error: %s", key, exc, exc_info=True)
            results.append({"lecture": key, "status": "FAIL", "reason": str(exc)})
            fail_count += 1

    # ------------------------------------------------------------------ #
    # 2. Full reprocess path
    # ------------------------------------------------------------------ #
    for key in lectures_to_reprocess:
        try:
            group, lecture = _parse_lecture_key(key)
        except ValueError as exc:
            results.append({"lecture": key, "status": "FAIL", "reason": str(exc)})
            fail_count += 1
            continue

        logger.info("[backfill] full-reprocess: %s", key)

        # Reconstruct transcript from Pinecone
        transcript = _reconstruct_from_pinecone(idx, group, lecture, "transcript")
        if not transcript or len(transcript) < 500:
            reason = (
                "transcript too short or missing in Pinecone "
                f"({len(transcript)} chars)"
            )
            logger.warning("[backfill] %s: %s", key, reason)
            results.append({"lecture": key, "status": "SKIP", "reason": reason})
            skip_count += 1
            continue

        try:
            _pipeline_key_var.set(f"g{group}_l{lecture}")

            claude_sections = _claude_reason_all(transcript)

            analysis_configs = [
                ("summary", SUMMARIZATION_PROMPT, "summary"),
                ("gap_analysis", GAP_ANALYSIS_PROMPT, "gap analysis"),
                ("deep_analysis", DEEP_ANALYSIS_PROMPT, "deep analysis"),
            ]

            georgian_texts: dict[str, str] = {}
            for section_key, prompt, label in analysis_configs:
                en_text = claude_sections.get(section_key, "")
                if not en_text:
                    logger.warning(
                        "[backfill] %s: Claude returned empty section '%s'",
                        key, section_key,
                    )
                    georgian_texts[section_key] = ""
                    continue

                geo_text = _safe_gemini_write_georgian(en_text, prompt, label)
                georgian_texts[section_key] = geo_text

            vec_counts: dict[str, int] = {}
            for content_type, text in georgian_texts.items():
                if not text:
                    vec_counts[content_type] = 0
                    continue
                count = index_lecture_content(
                    group_number=group,
                    lecture_number=lecture,
                    content=text,
                    content_type=content_type,
                    force=True,
                )
                vec_counts[content_type] = count

            # Upload private report (gap + deep) to Drive — no WhatsApp
            gap_geo = georgian_texts.get("gap_analysis", "")
            deep_geo = georgian_texts.get("deep_analysis", "")
            if gap_geo or deep_geo:
                try:
                    _upload_private_report_to_drive(
                        group_number=group,
                        lecture_number=lecture,
                        gap_analysis=gap_geo or "(gap analysis generation failed)",
                        deep_analysis=deep_geo or "(deep analysis generation failed)",
                    )
                except Exception as drive_exc:
                    logger.warning(
                        "[backfill] %s: Drive upload failed (non-fatal): %s",
                        key, drive_exc,
                    )

            results.append({
                "lecture": key,
                "status": "OK",
                "mode": "full_reprocess",
                "vectors_indexed": vec_counts,
            })
            ok_count += 1
            logger.info(
                "[backfill] %s: full reprocess done — vectors: %s", key, vec_counts
            )

        except RuntimeError as exc:
            if "budget" in str(exc).lower():
                logger.error("[backfill] %s: budget exceeded — stopping", key)
                results.append({"lecture": key, "status": "FAIL", "reason": f"budget exceeded: {exc}"})
                fail_count += 1
                break
            logger.error("[backfill] %s: runtime error: %s", key, exc, exc_info=True)
            results.append({"lecture": key, "status": "FAIL", "reason": str(exc)})
            fail_count += 1
        except Exception as exc:
            logger.error("[backfill] %s: unexpected error: %s", key, exc, exc_info=True)
            results.append({"lecture": key, "status": "FAIL", "reason": str(exc)})
            fail_count += 1

    # ------------------------------------------------------------------ #
    # 3. Full-rebuild path (download from Zoom + full pipeline, no WhatsApp)
    # ------------------------------------------------------------------ #
    for key in (lectures_to_full_rebuild or []):
        try:
            group, lecture = _parse_lecture_key(key)
        except ValueError as exc:
            results.append({"lecture": key, "status": "FAIL", "reason": str(exc)})
            fail_count += 1
            continue

        logger.info("[backfill] full-rebuild: %s", key)

        # Download recording from Zoom
        video_path = _download_zoom_recording_for_lecture(group, lecture)
        if not video_path:
            reason = "Zoom recording not found or no longer available"
            logger.warning("[backfill] %s: %s", key, reason)
            results.append({"lecture": key, "status": "SKIP", "reason": reason})
            skip_count += 1
            continue

        try:
            from tools.services.transcribe_lecture import transcribe_and_index

            counts = transcribe_and_index(
                group_number=group,
                lecture_number=lecture,
                video_path=str(video_path),
                silent=True,
            )

            total_vectors = sum(counts.values()) if isinstance(counts, dict) else 0
            results.append({
                "lecture": key,
                "status": "OK",
                "mode": "full_rebuild",
                "vectors_indexed": total_vectors,
            })
            ok_count += 1
            logger.info(
                "[backfill] %s: full-rebuild done (%d vectors)", key, total_vectors
            )

        except RuntimeError as exc:
            if "budget" in str(exc).lower():
                logger.error("[backfill] %s: budget exceeded — stopping", key)
                results.append({
                    "lecture": key,
                    "status": "FAIL",
                    "reason": f"budget exceeded: {exc}",
                })
                fail_count += 1
                break  # Stop further processing — budget is shared
            logger.error("[backfill] %s: full-rebuild failed: %s", key, exc)
            results.append({"lecture": key, "status": "FAIL", "reason": str(exc)})
            fail_count += 1
        except Exception as exc:
            logger.error(
                "[backfill] %s: full-rebuild error: %s", key, exc, exc_info=True
            )
            results.append({"lecture": key, "status": "FAIL", "reason": str(exc)})
            fail_count += 1
        finally:
            # Always clean up the downloaded video file
            try:
                if video_path and video_path.exists():
                    video_path.unlink()
                    logger.info("[backfill] %s: cleaned up %s", key, video_path.name)
            except OSError as cleanup_exc:
                logger.warning(
                    "[backfill] %s: could not delete video file: %s",
                    key, cleanup_exc,
                )

    return {
        "results": results,
        "ok": ok_count,
        "skipped": skip_count,
        "failed": fail_count,
    }


def _auto_detect_missing_deep_analysis() -> list[str]:
    """Scan all group×lecture combinations and return keys missing deep_analysis.

    Uses lecture_exists_in_index() to check Pinecone.  Only checks lectures
    that have a transcript indexed (so we know there is something to analyse).

    Returns:
        List of keys like ['g1_l3', 'g2_l7'] that have a transcript but no
        deep_analysis vector in Pinecone.
    """
    from tools.integrations.knowledge_indexer import lecture_exists_in_index

    missing: list[str] = []
    for group in (1, 2):
        for lecture in range(1, MAX_LECTURES + 1):
            has_transcript = lecture_exists_in_index(group, lecture, "transcript")
            if not has_transcript:
                continue  # No transcript → nothing to analyse
            has_deep = lecture_exists_in_index(group, lecture, "deep_analysis")
            if not has_deep:
                missing.append(f"g{group}_l{lecture}")
    return missing


@admin_router.post("/backfill-deep-analysis")
async def backfill_deep_analysis(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Reconstruct transcripts from Pinecone and run deep analysis for missing lectures.

    Authentication: same WEBHOOK_SECRET Bearer token as all other admin endpoints.

    Request body (optional JSON):
        {
            "lectures":   ["g1_l1", "g2_l5"],   // deep_analysis only (auto-detect if omitted)
            "reprocess":  ["g2_l9"]              // full re-analysis (summary + gap + deep)
        }

    Behaviour:
    - ``lectures``  path: fetches transcript chunks from Pinecone, runs Claude
      deep_analysis only, writes Georgian via Gemini, uploads to Drive private
      folder, indexes in Pinecone.  Skips lectures already indexed.
    - ``reprocess`` path: same but runs all three analyses (summary + gap + deep)
      and force-overwrites Pinecone vectors.
    - NEVER sends WhatsApp notifications.

    The work runs as a FastAPI BackgroundTask; the endpoint returns 202 immediately
    with the list of queued lectures.
    """
    verify_webhook_secret, _, _, _ = _server_internals()
    verify_webhook_secret(authorization)

    body_bytes = await request.body()
    req_body = BackfillRequest()
    if body_bytes:
        try:
            import json as _json
            parsed = _json.loads(body_bytes)
            req_body = BackfillRequest(**parsed)
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"Invalid request body: {exc}"
            ) from exc

    # Validate all keys before starting background work
    all_keys = req_body.lectures + req_body.reprocess + req_body.full_rebuild
    for key in all_keys:
        try:
            _parse_lecture_key(key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Auto-detect if no explicit list provided
    lectures_to_deep = req_body.lectures
    if not lectures_to_deep and not req_body.reprocess and not req_body.full_rebuild:
        try:
            lectures_to_deep = await asyncio.to_thread(_auto_detect_missing_deep_analysis)
        except Exception as exc:
            logger.error("[backfill] Auto-detect failed: %s", exc)
            raise HTTPException(
                status_code=503, detail=f"Pinecone auto-detect failed: {exc}"
            ) from exc

    lectures_to_reprocess = req_body.reprocess
    lectures_to_full_rebuild = req_body.full_rebuild

    total_queued = (
        len(lectures_to_deep)
        + len(lectures_to_reprocess)
        + len(lectures_to_full_rebuild)
    )
    if total_queued == 0:
        return JSONResponse(
            content={
                "status": "nothing_to_do",
                "message": "No lectures need deep_analysis — all indexed or no transcripts found.",
                "queued": [],
            },
            status_code=200,
        )

    logger.info(
        "[backfill] Queuing: %d deep-only + %d reprocess + %d full-rebuild",
        len(lectures_to_deep),
        len(lectures_to_reprocess),
        len(lectures_to_full_rebuild),
    )

    # Schedule as an asyncio task so this endpoint returns 202 immediately.
    # _run_backfill_sync is a synchronous function (blocking Claude/Gemini/Pinecone
    # calls), so we run it in a thread pool via asyncio.to_thread to avoid
    # blocking the event loop.
    asyncio.create_task(
        asyncio.to_thread(
            _run_backfill_sync,
            lectures_to_deep,
            lectures_to_reprocess,
            lectures_to_full_rebuild,
        ),
        name=f"backfill_{datetime.now(timezone.utc).strftime('%H%M%S')}",
    )

    return JSONResponse(
        content={
            "status": "accepted",
            "queued_deep_only": lectures_to_deep,
            "queued_reprocess": lectures_to_reprocess,
            "queued_full_rebuild": lectures_to_full_rebuild,
            "total_queued": total_queued,
            "message": (
                "Backfill running in background. "
                "Check server logs for progress and per-lecture results."
            ),
        },
        status_code=202,
    )
