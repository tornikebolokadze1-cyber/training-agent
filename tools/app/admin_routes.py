"""Admin API endpoints for manual pipeline management.

Provides operator-level curl commands for:
- Retrying specific lectures
- Resetting stuck pipelines
- Viewing lecture status across all groups
- Forcing Google OAuth token refresh
- Generating WhatsApp-friendly system reports

All endpoints require WEBHOOK_SECRET auth.

Rate limits (per IP, enforced by slowapi):
- POST (write/mutating) endpoints: 5/minute
- GET (read-only) endpoints:       20/minute

Backfill size cap: ``/admin/backfill-deep-analysis`` accepts at most
``MAX_BACKFILL_ITEMS`` (default 15, env-configurable) lectures per call;
oversized requests return 400.  Prevents an API-bill DoS from a paste-
twice operator typo (e.g. a 500-item array would otherwise queue 500
Claude + Gemini backfills in the background).

Note: server.py internals (_processing_lock, _processing_tasks, _task_key,
verify_webhook_secret) are imported lazily inside each endpoint function
to avoid circular imports (server.py imports admin_router at module level).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from tools.app.server import limiter
from tools.core.config import GROUPS, TBILISI_TZ
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

MAX_LECTURES = 15

# Backfill DoS protection: cap total queued lectures per request.
# Default 15 lectures (one full cohort).  Env-overrideable for emergencies.
# Read lazily inside the endpoint so tests can monkeypatch the env var
# without re-importing the module.
MAX_BACKFILL_ITEMS = int(os.environ.get("MAX_BACKFILL_ITEMS", "15"))


def _max_backfill_items() -> int:
    """Return the current backfill size cap, re-reading env each call.

    Re-read on every call so tests can monkeypatch ``MAX_BACKFILL_ITEMS``
    via ``monkeypatch.setenv`` without re-importing the module.  Falls back
    to the module-level constant when the env var is unset.
    """
    raw = os.environ.get("MAX_BACKFILL_ITEMS")
    if raw is None:
        return MAX_BACKFILL_ITEMS
    try:
        return int(raw)
    except ValueError:
        return MAX_BACKFILL_ITEMS


def _configured_group_numbers() -> list[int]:
    """Return all configured training groups, including newer cohorts."""
    return sorted(GROUPS.keys()) if GROUPS else [1, 2]


def _group_label(group_number: int) -> str:
    """Return the user-facing cohort label for admin reports."""
    return GROUPS.get(group_number, {}).get("name") or f"Group {group_number}"


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
        if v not in _configured_group_numbers():
            raise ValueError(
                f"group_number must be one of {_configured_group_numbers()}"
            )
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


@limiter.limit("5/minute")
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


@limiter.limit("5/minute")
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

    # ----------------------------------------------------------------
    # Issue #45 — cleanup orphans BEFORE the state file is deleted.
    # The previous implementation deleted the state file and left every
    # downstream artifact dangling: the lecture's Drive summary doc,
    # the private analysis report, the Pinecone vectors, and (in some
    # historical cases) the uploaded video file.  Subsequent reruns
    # then double-indexed the lecture and the analytics dashboard
    # showed the same content twice.
    #
    # Cleanup is best-effort: failures are logged but never abort the
    # reset itself — an admin invoking reset wants the lecture re-runnable,
    # not blocked on a Drive API hiccup.  Drive files are moved to trash
    # (recoverable within 30 days) rather than hard-deleted, so an
    # operator who reset by mistake can still restore the documents.
    # ----------------------------------------------------------------
    orphan_cleanup: dict[str, Any] = {
        "drive_trashed": [],
        "pinecone_deleted": 0,
        "errors": [],
    }

    drive_ids_to_trash = [
        doc_id for doc_id in (
            existing.summary_doc_id,
            existing.report_doc_id,
            existing.drive_video_id,
        ) if doc_id
    ]
    if drive_ids_to_trash:
        try:
            from tools.integrations.gdrive_manager import get_drive_service

            svc = get_drive_service()
            for file_id in drive_ids_to_trash:
                try:
                    svc.files().update(
                        fileId=file_id, body={"trashed": True},
                    ).execute()
                    orphan_cleanup["drive_trashed"].append(file_id)
                except Exception as exc:
                    msg = f"drive trash failed for {file_id}: {exc}"
                    logger.warning(msg)
                    orphan_cleanup["errors"].append(msg)
        except Exception as exc:  # noqa: BLE001
            msg = f"drive service unavailable for orphan cleanup: {exc}"
            logger.warning(msg)
            orphan_cleanup["errors"].append(msg)

    try:
        from tools.integrations.knowledge_indexer import delete_lecture_vectors

        deleted = delete_lecture_vectors(group, lecture)
        orphan_cleanup["pinecone_deleted"] = deleted
    except Exception as exc:  # noqa: BLE001
        msg = f"pinecone cleanup failed: {exc}"
        logger.warning(msg)
        orphan_cleanup["errors"].append(msg)

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
        "Admin reset: G%d L%d (previous_state=%s, drive_trashed=%d, vectors_deleted=%d)",
        group,
        lecture,
        previous_state,
        len(orphan_cleanup["drive_trashed"]),
        orphan_cleanup["pinecone_deleted"],
    )

    return JSONResponse(
        content={
            "status": "reset",
            "group": group,
            "lecture": lecture,
            "previous_state": previous_state,
            "orphan_cleanup": orphan_cleanup,
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


@limiter.limit("20/minute")
@admin_router.get("/lecture-status")
async def lecture_status(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Full status for all lectures across all configured groups.

    Returns pipeline state, Pinecone indexing, Drive files,
    and error info for each lecture.
    """
    verify_webhook_secret = _server_internals()[0]
    verify_webhook_secret(authorization)

    results: dict[str, list[dict[str, Any]]] = {}

    for group_num in _configured_group_numbers():
        group_statuses: list[dict[str, Any]] = []
        for lec in range(1, MAX_LECTURES + 1):
            status = _get_lecture_status(group_num, lec)
            group_statuses.append(status)
        results[_group_label(group_num)] = group_statuses

    # Try to enrich with Qdrant vector counts (non-fatal)
    try:
        vector_counts = await _get_vector_counts()
        for lectures in results.values():
            for lec_status in lectures:
                pc_key = (lec_status["group"], lec_status["lecture"])
                if pc_key in vector_counts:
                    lec_status["pinecone_indexed"] = True
                    lec_status["pinecone_vectors"] = vector_counts[pc_key]
    except Exception as exc:
        logger.warning("Qdrant enrichment failed (non-fatal): %s", exc)

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


async def _get_vector_counts() -> dict[tuple[int, int], int]:
    """Query Qdrant for vector counts per group+lecture.

    Returns dict mapping (group, lecture) -> vector count.
    Non-fatal: returns empty dict on any error.

    Migrated from Pinecone to Qdrant on 2026-05-20.  Uses
    ``knowledge_indexer.get_lecture_vector_count``, which now wraps
    ``QdrantClient.count(collection, filter=...)`` under the hood.
    """
    counts: dict[tuple[int, int], int] = {}

    try:
        from tools.integrations.knowledge_indexer import get_lecture_vector_count

        for group_num in _configured_group_numbers():
            for lec in range(1, MAX_LECTURES + 1):
                count = await asyncio.to_thread(
                    get_lecture_vector_count, group_num, lec,
                )
                if count:
                    counts[(group_num, lec)] = count
    except Exception as exc:
        logger.warning("Qdrant count query failed: %s", exc)

    return counts


async def _get_pinecone_counts() -> dict[tuple[int, int], int]:
    """Backward-compatible alias for ``_get_vector_counts``.

    Kept so any in-flight code paths (tests, background tasks) that still
    reference the old name continue to work. New code should call
    ``_get_vector_counts`` directly.
    """
    return await _get_vector_counts()


# ---------------------------------------------------------------------------
# 4. POST /admin/force-refresh-token
# ---------------------------------------------------------------------------


@limiter.limit("5/minute")
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


@limiter.limit("20/minute")
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
        lines.append(f"  {_group_label(p.group)} L{p.lecture}: {p.state}")

    # In-flight tasks
    with _processing_lock:
        task_count = len(_processing_tasks)
    lines.append(f"In-flight dedup tasks: {task_count}")

    # Per-group lecture matrix
    for group_num in _configured_group_numbers():
        lines.append(f"\n--- {_group_label(group_num)} ---")
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
                    recent_errors.append(
                        f"  {_group_label(p.group)} L{p.lecture}: {p.error[:80]}"
                    )
            except (ValueError, TypeError):
                recent_errors.append(
                    f"  {_group_label(p.group)} L{p.lecture}: {p.error[:80]}"
                )

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
        ValueError: If the format is not 'g{int}_l{int}' or the group/lecture
        falls outside the configured range.
    """
    try:
        parts = key.split("_")
        group = int(parts[0][1:])
        lecture = int(parts[1][1:])
        configured = _configured_group_numbers()
        if group not in configured or not (1 <= lecture <= MAX_LECTURES):
            raise ValueError
        return group, lecture
    except (IndexError, ValueError) as exc:
        configured = _configured_group_numbers()
        valid_range = (
            f"{min(configured)}-{max(configured)}" if configured else "1-2"
        )
        raise ValueError(
            f"Invalid lecture key '{key}'. Expected format: g{{N}}_l{{M}} "
            f"(group {valid_range}, lecture 1-{MAX_LECTURES})"
        ) from exc


def _reconstruct_from_qdrant(
    client: Any,
    group: int,
    lecture: int,
    content_type: str,
) -> str:
    """Fetch all chunks for a lecture+content_type from Qdrant and join them.

    Scrolls points whose payload matches the (group, lecture, content_type)
    filter, then concatenates the ``text`` field in ``chunk_index`` order
    to reconstruct the full text.

    Migrated from Pinecone to Qdrant on 2026-05-20.  The Pinecone version
    relied on ``index.list(prefix=...)`` + ``index.fetch(ids=...)``; the
    Qdrant equivalent is a single ``client.scroll(collection, filter=...)``
    paginated loop, which is more efficient and avoids the two-call dance.

    Args:
        client: A live Qdrant client (as returned by
            ``knowledge_indexer.get_qdrant_client``).  The historical name
            ``idx`` is preserved at the alias below.
        group: Group number (1, 2, 3, ...).
        lecture: Lecture number (1-15).
        content_type: One of 'transcript', 'summary', 'gap_analysis',
            'deep_analysis'.

    Returns:
        Reconstructed full text, or empty string if no chunks found.
    """
    try:
        from qdrant_client.http import models as qmodels
    except ImportError:  # pragma: no cover — qdrant-client always ships http models
        from qdrant_client import models as qmodels  # type: ignore[no-redef]

    from tools.core.config import QDRANT_COLLECTION_NAME

    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="group_number",
                match=qmodels.MatchValue(value=group),
            ),
            qmodels.FieldCondition(
                key="lecture_number",
                match=qmodels.MatchValue(value=lecture),
            ),
            qmodels.FieldCondition(
                key="content_type",
                match=qmodels.MatchValue(value=content_type),
            ),
        ]
    )

    chunks: list[tuple[int, str]] = []
    offset: Any = None
    page_size = 256

    try:
        while True:
            points, offset = client.scroll(
                collection_name=QDRANT_COLLECTION_NAME,
                scroll_filter=flt,
                limit=page_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for p in points or []:
                payload = getattr(p, "payload", None) or {}
                if isinstance(payload, dict):
                    chunk_index = int(payload.get("chunk_index", 0) or 0)
                    text = payload.get("text", "") or ""
                    if text:
                        chunks.append((chunk_index, text))
            if offset is None:
                break
    except Exception as exc:
        logger.warning(
            "_reconstruct_from_qdrant: scroll() failed for g%d l%d %s: %s",
            group, lecture, content_type, exc,
        )
        return ""

    if not chunks:
        logger.debug(
            "No vectors found for g%d l%d %s", group, lecture, content_type
        )
        return ""

    chunks.sort(key=lambda x: x[0])
    return "\n".join(t for _, t in chunks if t)


def _reconstruct_from_pinecone(
    idx: Any,
    group: int,
    lecture: int,
    content_type: str,
) -> str:
    """Backward-compatible alias for ``_reconstruct_from_qdrant``.

    Kept so any in-flight code paths (tests, helper scripts) that still
    reference the old name continue to work. ``idx`` is forwarded as the
    Qdrant client — callers that already obtain ``get_pinecone_index()``
    (which now returns a QdrantClient) work without changes.
    """
    return _reconstruct_from_qdrant(idx, group, lecture, content_type)


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

    Reads ``start_date`` and ``meeting_days`` from ``GROUPS[group]`` so it
    works for every configured cohort (March #1/#2 and the May cohorts loaded
    via ``_load_optional_groups``).

    Args:
        group: Group number from GROUPS (1, 2, 3, 4, ...).
        lecture: Lecture number (1-15).

    Returns:
        The date on which that lecture occurred, or None when the group is
        not configured or the lecture index falls outside the 15-lecture
        sequence.
    """
    from datetime import timedelta

    from tools.core.config import EXCLUDED_DATES, GROUPS

    cfg = GROUPS.get(group)
    if not cfg:
        return None

    start = cfg.get("start_date")
    weekdays = set(cfg.get("meeting_days") or [])
    if start is None or not weekdays:
        return None

    count = 0
    d = start
    while count < 15:
        if d.weekday() in weekdays and d not in EXCLUDED_DATES:
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
        logger.error("[backfill] Cannot connect to Qdrant: %s", exc)
        return {
            "results": [{"status": "FATAL", "reason": f"Qdrant unavailable: {exc}"}],
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

        # Try Qdrant first (works for lectures with text payload)
        transcript = _reconstruct_from_qdrant(idx, group, lecture, "transcript")

        # If transcript unavailable, fall back to Drive-based context
        use_drive_context = False
        if not transcript or len(transcript) < 500:
            logger.info(
                "[backfill] %s: transcript unavailable in Qdrant (%d chars), "
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
                reason = "Neither Qdrant transcript nor Drive context available"
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
            # redundant API call.  For Qdrant-sourced lectures, query Qdrant.
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
                gap_text_existing = _reconstruct_from_qdrant(
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

            # Index in Qdrant (force=True to overwrite any partial stale vectors)
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

        # Reconstruct transcript from Qdrant
        transcript = _reconstruct_from_qdrant(idx, group, lecture, "transcript")
        if not transcript or len(transcript) < 500:
            reason = (
                "transcript too short or missing in Qdrant "
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


@limiter.limit("5/minute")
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

    # US-020: size cap on explicit-list path BEFORE any background work or
    # auto-detect.  Prevents an API-bill DoS from a paste-twice operator typo
    # (a 500-item array would otherwise queue 500 Claude+Gemini backfills).
    cap = _max_backfill_items()
    explicit_total = len(all_keys)
    if explicit_total > cap:
        raise HTTPException(
            status_code=400,
            detail=(
                f"მოთხოვნა აღემატება მაქსიმუმს: {explicit_total} ლექცია > "
                f"MAX_BACKFILL_ITEMS={cap}. დაყავი მცირე ბუნდულებად."
            ),
        )

    # Auto-detect if no explicit list provided.
    # Choice: TRUNCATE the auto-detected set to the cap (with a warning log)
    # rather than rejecting outright.  Rationale: auto-detect is the "just
    # backfill whatever is missing" convenience path used in operator runbooks
    # — outright rejection would force the operator to manually paginate, which
    # defeats the auto-detect feature.  Truncation still bounds API spend per
    # call, and the operator can simply re-invoke until empty.
    lectures_to_deep = req_body.lectures
    auto_detect_truncated = False
    if not lectures_to_deep and not req_body.reprocess and not req_body.full_rebuild:
        try:
            lectures_to_deep = await asyncio.to_thread(_auto_detect_missing_deep_analysis)
        except Exception as exc:
            logger.error("[backfill] Auto-detect failed: %s", exc)
            raise HTTPException(
                status_code=503, detail=f"Pinecone auto-detect failed: {exc}"
            ) from exc
        if len(lectures_to_deep) > cap:
            logger.warning(
                "[backfill] Auto-detect found %d missing lectures, truncating "
                "to MAX_BACKFILL_ITEMS=%d. Re-run the endpoint to backfill the rest.",
                len(lectures_to_deep),
                cap,
            )
            lectures_to_deep = lectures_to_deep[:cap]
            auto_detect_truncated = True

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

    response_payload: dict[str, Any] = {
        "status": "accepted",
        "queued_deep_only": lectures_to_deep,
        "queued_reprocess": lectures_to_reprocess,
        "queued_full_rebuild": lectures_to_full_rebuild,
        "total_queued": total_queued,
        "max_backfill_items": cap,
        "message": (
            "Backfill running in background. "
            "Check server logs for progress and per-lecture results."
        ),
    }
    if auto_detect_truncated:
        response_payload["auto_detect_truncated"] = True
        response_payload["message"] = (
            f"Auto-detect found more than MAX_BACKFILL_ITEMS={cap} missing lectures; "
            f"truncated to {cap}. Re-invoke the endpoint to backfill the rest. "
            + response_payload["message"]
        )

    return JSONResponse(content=response_payload, status_code=202)


# ---------------------------------------------------------------------------
# WhatsApp webhook configuration inspection / repair
# ---------------------------------------------------------------------------


@limiter.limit("20/minute")
@admin_router.get("/whatsapp-webhook-status")
async def whatsapp_webhook_status(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Inspect the live Green API webhook configuration.

    Reports whether incoming WhatsApp messages are actually being
    delivered to this server: the configured ``webhookUrl`` on the
    Green API side, whether ``incomingWebhook`` is enabled, and whether
    that URL matches what the running container expects (derived from
    ``SERVER_PUBLIC_URL`` or ``RAILWAY_PUBLIC_DOMAIN``).

    A mismatch is the typical cause of a "live trigger ignored" symptom
    that the catch-up loop has to recover from — this endpoint confirms
    the live channel is healthy and tells you whether to call
    ``/admin/whatsapp-webhook-repair``.

    Authentication: ``Authorization: Bearer ${WEBHOOK_SECRET}``.
    """
    verify_webhook_secret, _, _, _ = _server_internals()
    verify_webhook_secret(authorization)

    import os

    from tools.integrations.whatsapp_sender import get_webhook_settings

    try:
        settings = await asyncio.to_thread(get_webhook_settings)
    except Exception as exc:
        logger.error("[admin] whatsapp-webhook-status failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Green API call failed: {exc}")

    webhook_url = str(settings.get("webhookUrl") or "").strip()
    incoming_flag = str(settings.get("incomingWebhook") or "").lower()
    incoming_enabled = incoming_flag in ("yes", "true", "1")

    public_url_env = os.environ.get("SERVER_PUBLIC_URL", "").strip()
    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if public_url_env:
        expected_base = public_url_env.rstrip("/")
    elif public_domain:
        expected_base = f"https://{public_domain}"
    else:
        expected_base = ""
    expected_url = f"{expected_base}/whatsapp-incoming" if expected_base else ""
    matches_expected = bool(expected_url) and webhook_url == expected_url
    token_set = bool(str(settings.get("webhookUrlToken") or "").strip())

    return JSONResponse(
        content={
            "status": "ok",
            "incoming_enabled": incoming_enabled,
            "webhook_url_configured": webhook_url,
            "webhook_url_token_set": token_set,
            "expected_url": expected_url,
            "matches_expected": matches_expected,
            "raw_settings_keys": sorted(list(settings.keys())),
        },
        status_code=200,
    )


@limiter.limit("5/minute")
@admin_router.post("/whatsapp-webhook-repair")
async def whatsapp_webhook_repair(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Re-point the Green API webhook at this server.

    Computes the expected webhook URL from ``SERVER_PUBLIC_URL`` (or
    ``RAILWAY_PUBLIC_DOMAIN``) and calls Green API's ``setSettings`` so
    incoming messages are delivered here with the correct Bearer token.

    Use after a Railway URL change, an env-var rotation, or whenever
    ``/whatsapp-webhook-status`` reports ``matches_expected = false``.

    Authentication: ``Authorization: Bearer ${WEBHOOK_SECRET}``.
    """
    verify_webhook_secret, _, _, _ = _server_internals()
    verify_webhook_secret(authorization)

    import os

    from tools.integrations.whatsapp_sender import configure_webhook

    public_url_env = os.environ.get("SERVER_PUBLIC_URL", "").strip()
    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if public_url_env:
        base = public_url_env.rstrip("/")
    elif public_domain:
        base = f"https://{public_domain}"
    else:
        raise HTTPException(
            status_code=503,
            detail=(
                "Cannot derive public URL — SERVER_PUBLIC_URL and "
                "RAILWAY_PUBLIC_DOMAIN are both empty"
            ),
        )

    target_url = f"{base}/whatsapp-incoming"

    try:
        result = await asyncio.to_thread(configure_webhook, target_url)
    except Exception as exc:
        logger.error("[admin] whatsapp-webhook-repair failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Green API call failed: {exc}")

    return JSONResponse(
        content={
            "status": "ok",
            "configured_url": target_url,
            "green_api_response": result,
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# WhatsApp assistant catch-up — manual replay of missed triggers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /admin/groups-debug
# ---------------------------------------------------------------------------
# Diagnostic endpoint exposing the live GROUPS configuration as the running
# process sees it.  Returns each group's name, course_completed flag,
# meeting_days, and a partially-masked chat_id so that the operator can
# verify on production whether Railway env vars route messages to the
# expected WhatsApp chats.  Full chat IDs are never returned — masking keeps
# the response safe to share in support channels.
#
# Mask format: first 6 chars + "…" + last 6 chars of the chat ID.  Example:
# ``120363425514041539@g.us`` becomes ``120363…41539@g.us``.  Enough to
# distinguish the four chats without exposing the full identifier.


def _mask_chat_id(chat_id: str | None) -> str:
    """Mask a WhatsApp chat ID so the operator can identify it without leakage."""
    if not chat_id:
        return ""
    if "@" in chat_id:
        local, _, suffix = chat_id.partition("@")
    else:
        local, suffix = chat_id, ""
    if len(local) <= 14:
        masked_local = local
    else:
        masked_local = f"{local[:6]}…{local[-6:]}"
    return f"{masked_local}@{suffix}" if suffix else masked_local


@limiter.limit("20/minute")
@admin_router.get("/groups-debug")
async def groups_debug(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Return the running GROUPS config (masked chat IDs) for diagnostics.

    Authentication: ``Authorization: Bearer ${WEBHOOK_SECRET}``.

    Used to verify on Railway whether WHATSAPP_GROUP{N}_ID and related env
    vars are pointing to the expected cohort chats.  Specifically catches
    the case where an env var rotation accidentally maps a completed cohort
    (March) onto the active one (May) or vice versa.
    """
    verify_webhook_secret = _server_internals()[0]
    verify_webhook_secret(authorization)

    payload: list[dict[str, Any]] = []
    for group_num in sorted(GROUPS.keys()):
        cfg = GROUPS[group_num]
        payload.append({
            "group_number": group_num,
            "name": cfg.get("name", ""),
            "course_completed": bool(cfg.get("course_completed", False)),
            "meeting_days": list(cfg.get("meeting_days", [])),
            "start_date": cfg.get("start_date").isoformat() if cfg.get("start_date") else None,
            "chat_id_masked": _mask_chat_id(cfg.get("whatsapp_chat_id", "")),
            "chat_id_set": bool(cfg.get("whatsapp_chat_id")),
            "drive_folder_id_set": bool(cfg.get("drive_folder_id")),
            "analysis_folder_id_set": bool(cfg.get("analysis_folder_id")),
            "zoom_meeting_id_set": bool(cfg.get("zoom_meeting_id")),
        })

    # Cross-reference: duplicate chat IDs across groups are a red flag —
    # they're the most common cause of "messages going to old groups too".
    seen: dict[str, list[int]] = {}
    for group_num, cfg in GROUPS.items():
        chat = cfg.get("whatsapp_chat_id", "")
        if chat:
            seen.setdefault(chat, []).append(group_num)
    duplicates = {
        _mask_chat_id(chat): groups for chat, groups in seen.items() if len(groups) > 1
    }

    return JSONResponse(
        content={
            "groups": payload,
            "duplicate_chat_ids": duplicates,
            "timestamp": datetime.now(TBILISI_TZ).isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# GET /admin/recent-outgoing
# ---------------------------------------------------------------------------
# Diagnostic-only: ask Green API which chats our bot actually sent to in the
# last N hours.  This isolates "is the duplicate-reminder coming from our
# Python sender?" from "is something else (legacy n8n, a forwarding rule)
# delivering it?".  Returns masked chat IDs + the first 80 chars of each
# message body, never full chat IDs and never the full text.  Auth: same
# WEBHOOK_SECRET bearer as the rest of /admin/*.


@limiter.limit("20/minute")
@admin_router.get("/recent-outgoing")
async def recent_outgoing(
    request: Request,
    authorization: str | None = Header(None),
    minutes: int = 1440,
) -> JSONResponse:
    """Return the bot's recent outgoing WhatsApp messages from Green API.

    Calls Green API's ``lastOutgoingMessages`` endpoint with the configured
    instance credentials, filters to the window ``minutes`` minutes back
    (max 7 days), and returns a redacted view: masked chat IDs, message
    preview (first 80 chars), timestamp, and idMessage. Useful to verify
    whether duplicate reminders observed in March chats are coming from
    our Python sender or from a different source (legacy n8n, etc.).
    """
    verify_webhook_secret = _server_internals()[0]
    verify_webhook_secret(authorization)

    if minutes < 1 or minutes > 10080:
        raise HTTPException(
            status_code=400, detail="minutes must be between 1 and 10080 (7 days)"
        )

    instance_id = os.environ.get("GREEN_API_INSTANCE_ID", "")
    token = os.environ.get("GREEN_API_TOKEN", "")
    if not instance_id or not token:
        raise HTTPException(status_code=503, detail="Green API not configured")

    import httpx

    url = (
        f"https://api.green-api.com/waInstance{instance_id}"
        f"/lastOutgoingMessages/{token}?minutes={minutes}"
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Green API call failed: {exc}")

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Green API HTTP {resp.status_code}: {resp.text[:200]}",
        )

    try:
        raw = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="Green API non-JSON response")

    if not isinstance(raw, list):
        return JSONResponse(content={"messages": [], "count": 0})

    redacted: list[dict[str, Any]] = []
    per_chat: dict[str, int] = {}
    for msg in raw:
        chat_id = str(msg.get("chatId") or "")
        text = (
            str(msg.get("textMessage") or "")
            or str((msg.get("extendedTextMessage") or {}).get("text") or "")
        )
        ts = msg.get("timestamp")
        masked = _mask_chat_id(chat_id)
        redacted.append({
            "chat_id_masked": masked,
            "type_message": msg.get("typeMessage"),
            "preview": text[:500],
            "timestamp": ts,
            "id_message": msg.get("idMessage"),
        })
        per_chat[masked] = per_chat.get(masked, 0) + 1

    return JSONResponse(
        content={
            "count": len(redacted),
            "messages_per_chat": per_chat,
            "messages": redacted[:50],
            "window_minutes": minutes,
            "timestamp": datetime.now(TBILISI_TZ).isoformat(),
        }
    )


@limiter.limit("5/minute")
@admin_router.post("/whatsapp-catchup")
async def whatsapp_catchup(
    request: Request,
    authorization: str | None = Header(None),
    since_minutes: int = 120,
) -> JSONResponse:
    """Manually trigger a WhatsApp catch-up run.

    Pulls the last ~100 messages of each allowed chat from Green API and
    runs the assistant on any trigger message that has no bot reply
    within 3 min after it. Idempotent — already-handled IDs are kept in
    ``.tmp/whatsapp_responded.json`` so re-running is safe.

    Authentication: ``Authorization: Bearer ${WEBHOOK_SECRET}``.

    Query parameters:
        since_minutes: How far back to look (default 120, max 1440).

    Returns:
        ``{"status": "ok", "result": {"checked": N, "replied": N, ...}}``
    """
    verify_webhook_secret, _, _, _ = _server_internals()
    verify_webhook_secret(authorization)

    if since_minutes < 1 or since_minutes > 24 * 60:
        raise HTTPException(
            status_code=400, detail="since_minutes must be between 1 and 1440",
        )

    import tools.app.server as _srv

    if not _srv._assistant_available or _srv.assistant is None:
        raise HTTPException(status_code=503, detail="Assistant not available")

    from tools.services.whatsapp_catchup import replay_recent

    try:
        result = await replay_recent(_srv.assistant, since_minutes=since_minutes)
    except Exception as exc:
        logger.error("[admin] whatsapp-catchup failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"catchup failed: {exc}")

    return JSONResponse(content={"status": "ok", "result": result}, status_code=200)
