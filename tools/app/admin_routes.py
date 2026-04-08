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
