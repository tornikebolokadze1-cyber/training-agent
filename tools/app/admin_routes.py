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

    lectures  — list of keys like ["g1_l1", "g2_l5"] to deep-analyse only
                (skipped if deep_analysis already indexed).  Omit for auto-detect.
    reprocess — list of keys like ["g2_l9"] to run FULL analysis on
                (summary + gap + deep), overwriting whatever is already indexed.
    """

    lectures: list[str] = []
    reprocess: list[str] = []


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


def _run_backfill_sync(
    lectures_to_deep: list[str],
    lectures_to_reprocess: list[str],
) -> dict[str, Any]:
    """Execute backfill operations (synchronous — call via asyncio.to_thread).

    deep-only path  : reconstruct transcript → Claude deep_analysis only →
                      Gemini Georgian writing → Drive private report → Pinecone index.
    reprocess path  : reconstruct transcript → full Claude (summary+gap+deep) →
                      Gemini Georgian for all 3 → Drive report → Pinecone index.

    NEVER sends WhatsApp messages.

    Args:
        lectures_to_deep: Keys like ['g1_l1'] that need deep_analysis only.
        lectures_to_reprocess: Keys like ['g2_l9'] that need full re-analysis.

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

            # Fetch existing gap analysis text for the private Drive report
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
    all_keys = req_body.lectures + req_body.reprocess
    for key in all_keys:
        try:
            _parse_lecture_key(key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Auto-detect if no explicit list provided
    lectures_to_deep = req_body.lectures
    if not lectures_to_deep and not req_body.reprocess:
        try:
            lectures_to_deep = await asyncio.to_thread(_auto_detect_missing_deep_analysis)
        except Exception as exc:
            logger.error("[backfill] Auto-detect failed: %s", exc)
            raise HTTPException(
                status_code=503, detail=f"Pinecone auto-detect failed: {exc}"
            ) from exc

    lectures_to_reprocess = req_body.reprocess

    total_queued = len(lectures_to_deep) + len(lectures_to_reprocess)
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
        "[backfill] Queuing: %d deep-only + %d reprocess",
        len(lectures_to_deep),
        len(lectures_to_reprocess),
    )

    # Schedule as an asyncio task so this endpoint returns 202 immediately.
    # _run_backfill_sync is a synchronous function (blocking Claude/Gemini/Pinecone
    # calls), so we run it in a thread pool via asyncio.to_thread to avoid
    # blocking the event loop.
    asyncio.create_task(
        asyncio.to_thread(_run_backfill_sync, lectures_to_deep, lectures_to_reprocess),
        name=f"backfill_{datetime.now(timezone.utc).strftime('%H%M%S')}",
    )

    return JSONResponse(
        content={
            "status": "accepted",
            "queued_deep_only": lectures_to_deep,
            "queued_reprocess": lectures_to_reprocess,
            "total_queued": total_queued,
            "message": (
                "Backfill running in background. "
                "Check server logs for progress and per-lecture results."
            ),
        },
        status_code=202,
    )
