"""Paperclip HTTP bridge — exposes on-demand task dispatch to this agent.

This module registers three FastAPI routes under the /paperclip prefix:
  - POST /paperclip/task   — receive a structured task from Paperclip, run it, return JSON
  - GET  /paperclip/health — liveness probe
  - GET  /paperclip/status — agent status, uptime, last-task summary, env-key presence

NOTE: POST /paperclip/task with the legacy PaperclipTaskPayload schema is already
registered directly on the ``app`` object in tools/app/server.py.  The router
defined here adds health + status endpoints, and also exposes the newer
PaperclipTask / PaperclipResponse schema for POST /paperclip/task.

Register this router in your main FastAPI app:
    from tools.app.paperclip_bridge import router as paperclip_router
    app.include_router(paperclip_router)

If a route already exists in the FastAPI app (e.g. /paperclip/task), FastAPI will
keep the first-registered route; the router registration therefore effectively
adds only the NEW routes (health and status) without overriding existing ones.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level uptime reference
# ---------------------------------------------------------------------------

_BRIDGE_START_TIME: float = time.time()
_last_task_summary: dict[str, Any] = {}

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PaperclipTask(BaseModel):
    """Structured task payload sent by Paperclip to this agent."""

    task_id: str
    title: str
    description: str
    priority: str = "medium"
    context: dict = {}


class PaperclipResponse(BaseModel):
    """Uniform response returned to Paperclip after processing a task."""

    ok: bool
    task_id: str
    summary: str          # human-readable result
    artifacts: list = []  # optional links / file paths
    cost_cents: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_paperclip_secret() -> str:
    """Return PAPERCLIP_WEBHOOK_SECRET from env (or fall back to WEBHOOK_SECRET)."""
    return os.environ.get("PAPERCLIP_WEBHOOK_SECRET") or os.environ.get("WEBHOOK_SECRET", "")


def _verify_bearer(authorization: str | None) -> None:
    """Validate the Paperclip bearer token.  Raises 401/503 on failure."""
    secret = _get_paperclip_secret()
    if not secret:
        logger.error("PAPERCLIP_WEBHOOK_SECRET not set — rejecting request (fail closed)")
        raise HTTPException(
            status_code=503,
            detail="Server misconfigured: PAPERCLIP_WEBHOOK_SECRET not set",
        )
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    import hmac
    expected = f"Bearer {secret}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Invalid bearer token")


def _env_key_present(name: str) -> bool:
    """Return True if the env var is set and non-empty."""
    return bool(os.environ.get(name, "").strip())


def _route_task(task: PaperclipTask) -> PaperclipResponse:
    """Dispatch a task by inspecting its description for routing keywords.

    Current routing:
      - schedule / zoom          → stub: next meeting info not yet implemented
      - transcribe / analyze     → stub: transcription is schedule-driven
      - status / health / check / readiness → real status summary
      - (fallback)               → acknowledged, routing not yet wired

    Long-running operations are acknowledged only — they are not queued here.
    """
    haystack = task.description.lower()

    try:
        # --- Status / readiness check -----------------------------------------
        if any(kw in haystack for kw in ("status", "health", "check", "readiness")):
            api_keys_present = {
                "ANTHROPIC_API_KEY": _env_key_present("ANTHROPIC_API_KEY"),
                "GEMINI_API_KEY": _env_key_present("GEMINI_API_KEY"),
                "ZOOM_ACCOUNT_ID": _env_key_present("ZOOM_ACCOUNT_ID"),
                "PINECONE_API_KEY": _env_key_present("PINECONE_API_KEY"),
                "PAPERCLIP_WEBHOOK_SECRET": _env_key_present("PAPERCLIP_WEBHOOK_SECRET"),
            }
            try:
                import tools.app.scheduler as _sched_mod
                scheduler = _sched_mod._scheduler_ref
                if scheduler and scheduler.running:
                    next_jobs = []
                    for job in scheduler.get_jobs():
                        nrt = job.next_run_time
                        next_jobs.append({
                            "id": job.id,
                            "name": job.name,
                            "next_run_time": nrt.isoformat() if nrt else None,
                        })
                    next_jobs.sort(key=lambda j: (j["next_run_time"] is None, j["next_run_time"] or ""))
                    scheduler_info = {
                        "state": "running",
                        "next_jobs": next_jobs[:3],  # top-3 upcoming
                    }
                else:
                    scheduler_info = {"state": "stopped" if scheduler else "unavailable"}
            except Exception as exc:  # noqa: BLE001
                scheduler_info = {"state": "error", "detail": str(exc)}

            summary = (
                f"Agent online. Keys: {sum(api_keys_present.values())}/{len(api_keys_present)} present. "
                f"Scheduler: {scheduler_info.get('state', 'unknown')}."
            )
            return PaperclipResponse(
                ok=True,
                task_id=task.task_id,
                summary=summary,
                artifacts=[{"api_keys": api_keys_present, "scheduler": scheduler_info}],
            )

        # --- Schedule / Zoom stubs -------------------------------------------
        if any(kw in haystack for kw in ("schedule", "zoom")):
            return PaperclipResponse(
                ok=True,
                task_id=task.task_id,
                summary=(
                    "Bridge is live — schedule/Zoom queries acknowledged. "
                    "Direct meeting-status lookup via Paperclip task is not yet implemented; "
                    "the scheduler-driven pipeline runs autonomously."
                ),
            )

        # --- Transcribe / Analyze stubs --------------------------------------
        if any(kw in haystack for kw in ("transcribe", "analyze", "analysis")):
            return PaperclipResponse(
                ok=True,
                task_id=task.task_id,
                summary=(
                    "Bridge is live — transcription/analysis tasks acknowledged. "
                    "The transcription pipeline is schedule-driven (APScheduler). "
                    "Direct task-based invocation is not yet wired."
                ),
            )

        # --- Fallback / acknowledged -----------------------------------------
        return PaperclipResponse(
            ok=True,
            task_id=task.task_id,
            summary=(
                f"Task '{task.title[:80]}' received and acknowledged. "
                "Bridge is live; specific routing for this task type is not yet implemented."
            ),
        )

    except Exception as exc:  # noqa: BLE001
        logger.error("[paperclip-bridge] Routing error for task %s: %s", task.task_id, exc)
        return PaperclipResponse(
            ok=False,
            task_id=task.task_id,
            summary="Internal routing error — see server logs.",
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["Paperclip"])


@router.post("/paperclip/task", response_model=PaperclipResponse)
async def paperclip_task_bridge(
    task: PaperclipTask,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Accept a structured task from Paperclip, route it, and return a result.

    Authentication: ``Authorization: Bearer <PAPERCLIP_WEBHOOK_SECRET>``

    This endpoint uses the newer ``PaperclipTask`` schema.  If a legacy
    ``/paperclip/task`` route is already registered on the app, FastAPI will
    serve the first-registered route; this route is effectively available when
    the legacy one is absent.
    """
    _verify_bearer(authorization)

    logger.info(
        "[paperclip-bridge] Incoming task id=%s title=%r priority=%s",
        task.task_id,
        task.title[:80],
        task.priority,
    )

    response = _route_task(task)

    # Record last-task summary for /paperclip/status
    global _last_task_summary
    _last_task_summary = {
        "task_id": task.task_id,
        "title": task.title[:120],
        "priority": task.priority,
        "ok": response.ok,
        "summary": response.summary[:200],
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    return JSONResponse(content=response.model_dump(), status_code=200)


@router.get("/paperclip/health")
async def paperclip_health() -> JSONResponse:
    """Liveness probe — no auth required."""
    return JSONResponse(content={"ok": True, "agent": "training-ops-lead"})


@router.get("/paperclip/status")
async def paperclip_status(
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Agent status: uptime, scheduler state, API key presence, last task.

    Authentication: ``Authorization: Bearer <PAPERCLIP_WEBHOOK_SECRET>``
    """
    _verify_bearer(authorization)

    uptime_seconds = round(time.time() - _BRIDGE_START_TIME, 1)

    # --- Scheduler state ---
    try:
        import tools.app.scheduler as _sched_mod
        scheduler = _sched_mod._scheduler_ref
        if scheduler and scheduler.running:
            jobs = []
            for job in scheduler.get_jobs():
                nrt = job.next_run_time
                jobs.append({
                    "id": job.id,
                    "name": job.name,
                    "next_run_time": nrt.isoformat() if nrt else None,
                    "trigger": str(job.trigger),
                })
            jobs.sort(key=lambda j: (j["next_run_time"] is None, j["next_run_time"] or ""))
            scheduler_state = {"state": "running", "job_count": len(jobs), "next_jobs": jobs[:3]}
        else:
            scheduler_state = {"state": "stopped" if scheduler else "unavailable"}
    except Exception as exc:  # noqa: BLE001
        scheduler_state = {"state": "error", "detail": str(exc)}

    # --- API key presence (no values, just booleans) ---
    api_keys = {
        "ANTHROPIC_API_KEY": _env_key_present("ANTHROPIC_API_KEY"),
        "GEMINI_API_KEY": _env_key_present("GEMINI_API_KEY"),
        "ZOOM_ACCOUNT_ID": _env_key_present("ZOOM_ACCOUNT_ID"),
        "PINECONE_API_KEY": _env_key_present("PINECONE_API_KEY"),
        "GREEN_API_TOKEN": _env_key_present("GREEN_API_TOKEN"),
        "PAPERCLIP_WEBHOOK_SECRET": _env_key_present("PAPERCLIP_WEBHOOK_SECRET"),
    }

    return JSONResponse(content={
        "agent": "training-ops-lead",
        "ok": True,
        "uptime_seconds": uptime_seconds,
        "scheduler": scheduler_state,
        "api_keys_present": api_keys,
        "last_task": _last_task_summary or None,
    })
