"""FastAPI webhook server — bridge between n8n and Python tools."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from tools.core.config import (
    GROUPS,
    IS_RAILWAY,
    MINIMUM_LECTURE_DURATION_MINUTES,
    N8N_CALLBACK_URL,
    SERVER_HOST,
    SERVER_PORT,
    TBILISI_TZ,
    TMP_DIR,
    WEBHOOK_SECRET,
    ZOOM_WEBHOOK_SECRET_TOKEN,
    extract_group_from_topic,
    get_drive_file_url,
    get_lecture_folder_name,
    get_lecture_number,
)
from tools.integrations.gdrive_manager import (
    ensure_folder,
    get_drive_service,
    upload_file,
)
from tools.integrations.whatsapp_sender import alert_operator
from tools.core.pipeline_state import (
    is_pipeline_active,
    is_pipeline_done,
    create_pipeline,
    list_active_pipelines,
    cleanup_completed,
    cleanup_stale_failed,
    reset_failed,
)
from tools.services.transcribe_lecture import transcribe_and_index

try:
    from tools.services.whatsapp_assistant import IncomingMessage, WhatsAppAssistant
    _assistant_available = True
except ImportError:
    _assistant_available = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-flight task tracking (deduplication + observability)
# ---------------------------------------------------------------------------
_processing_tasks: dict[str, datetime] = {}  # key: "g{group}_l{lecture}" -> start time
_processing_lock = threading.Lock()  # Prevent webhook+scheduler race condition
STALE_TASK_HOURS = 4  # Consider a task stale after 4 hours


def _task_key(group: int, lecture: int) -> str:
    return f"g{group}_l{lecture}"


def _rebuild_task_cache() -> None:
    """Rebuild in-memory task cache from persistent pipeline state files."""
    active = list_active_pipelines()
    with _processing_lock:
        for pipeline in active:
            key = _task_key(pipeline.group, pipeline.lecture)
            if key not in _processing_tasks:
                try:
                    _processing_tasks[key] = datetime.fromisoformat(pipeline.started_at)
                except (ValueError, TypeError):
                    _processing_tasks[key] = datetime.now()
    if active:
        logger.info("[dedup] Rebuilt task cache from %d active pipeline state files", len(active))


def _evict_stale_tasks() -> list[str]:
    """Remove tasks that have been running longer than STALE_TASK_HOURS.

    Returns list of evicted task keys (for logging).
    """
    now = datetime.now()
    stale = [
        key for key, started in _processing_tasks.items()
        if (now - started).total_seconds() > STALE_TASK_HOURS * 3600
    ]
    for key in stale:
        _processing_tasks.pop(key, None)
        logger.warning("Evicted stale task: %s (exceeded %dh timeout)", key, STALE_TASK_HOURS)
    if stale:
        try:
            alert_operator(
                f"Evicted {len(stale)} stale tasks: {', '.join(stale)}. "
                f"These ran for over {STALE_TASK_HOURS}h — check for hung pipelines."
            )
        except Exception as alert_err:
            logger.warning("alert_operator failed during stale task eviction: %s", alert_err)
    return stale


# Disable OpenAPI docs in production (Railway) to reduce attack surface
_docs_url = None if IS_RAILWAY else "/docs"
_redoc_url = None if IS_RAILWAY else "/redoc"

async def _eviction_loop() -> None:
    """Background task: evict stale processing tasks every 30 minutes."""
    while True:
        try:
            await asyncio.sleep(30 * 60)
            _evict_stale_tasks()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Stale task eviction loop error: %s", exc)


from contextlib import asynccontextmanager  # noqa: E402


async def _check_unprocessed_recordings() -> None:
    """Startup recovery: check Zoom for any unprocessed recordings from today.

    Uses GET /v2/users/me/recordings to list today's recordings, then checks
    if each one has already been processed (by looking for vectors in Pinecone
    with matching group + lecture number). If unprocessed recordings are found,
    starts the pipeline automatically.
    """
    from tools.app.scheduler import _run_post_meeting_pipeline

    try:
        zm = __import__("tools.integrations.zoom_manager", fromlist=["zoom_manager"])
    except ImportError:
        logger.warning("[startup-recovery] zoom_manager not available — skipping")
        return

    today = datetime.now(TBILISI_TZ).date()
    # Check last 3 days for missed recordings (not just today)
    from_date = (today - timedelta(days=2)).isoformat()
    today_str = today.isoformat()

    try:
        meetings = await asyncio.to_thread(
            zm.list_user_recordings, from_date, today_str,
        )
    except Exception as exc:
        logger.warning("[startup-recovery] Failed to list recordings: %s", exc)
        return

    if not meetings:
        logger.info("[startup-recovery] No recordings found for today (%s)", today_str)
        return

    logger.info("[startup-recovery] Found %d recording meeting(s) for today", len(meetings))

    for meeting in meetings:
        topic = meeting.get("topic", "")
        group_number = extract_group_from_topic(topic)
        if group_number is None:
            logger.debug("[startup-recovery] Skipping meeting with unknown topic: %s", topic[:60])
            continue

        # Determine lecture number from meeting start time
        start_time_str = meeting.get("start_time", "")
        try:
            meeting_date = datetime.fromisoformat(
                start_time_str.replace("Z", "+00:00")
            ).date()
        except (ValueError, AttributeError):
            meeting_date = today

        lecture_number = get_lecture_number(group_number, for_date=meeting_date)
        if lecture_number == 0:
            continue

        # Check if already processing
        key = _task_key(group_number, lecture_number)
        with _processing_lock:
            if key in _processing_tasks:
                logger.info("[startup-recovery] %s already processing — skipping", key)
                continue

        # Check pipeline state file first (cheaper than Pinecone query)
        if is_pipeline_done(group_number, lecture_number):
            logger.info("[startup-recovery] G%d L%d already COMPLETE per state file — skipping", group_number, lecture_number)
            continue
        if is_pipeline_active(group_number, lecture_number):
            logger.info("[startup-recovery] G%d L%d already active per state file — resuming handled by orchestrator", group_number, lecture_number)
            continue

        # Auto-reset FAILED pipelines so they can be retried on startup
        from tools.core.pipeline_state import load_state as _load_state, FAILED as _FAILED
        _existing = _load_state(group_number, lecture_number)
        if _existing and _existing.state == _FAILED:
            logger.info("[startup-recovery] G%d L%d was FAILED — resetting for retry", group_number, lecture_number)
            reset_failed(group_number, lecture_number)

        # Check if already indexed in Pinecone (any vectors for this lecture)
        already_indexed = False
        try:
            from tools.integrations.knowledge_indexer import get_pinecone_index
            index = await asyncio.to_thread(get_pinecone_index)
            # Query with a filter — if any vectors exist for this group+lecture, it's done
            dummy_embedding = [0.0] * 3072
            result = await asyncio.to_thread(
                lambda: index.query(
                    vector=dummy_embedding,
                    top_k=1,
                    filter={
                        "group_number": {"$eq": group_number},
                        "lecture_number": {"$eq": lecture_number},
                    },
                )
            )
            if result.get("matches"):
                already_indexed = True
        except Exception as exc:
            logger.warning(
                "[startup-recovery] Pinecone check failed for G%d L%d: %s — assuming not indexed",
                group_number, lecture_number, exc,
            )

        if already_indexed:
            logger.info(
                "[startup-recovery] G%d L%d already indexed in Pinecone — skipping",
                group_number, lecture_number,
            )
            continue

        # Not processed — start the pipeline
        meeting_uuid = meeting.get("uuid", "")
        meeting_id = str(meeting.get("id", ""))
        poll_id = meeting_uuid or meeting_id

        if not poll_id:
            logger.warning("[startup-recovery] No meeting ID for G%d L%d", group_number, lecture_number)
            continue

        with _processing_lock:
            # Re-check under lock
            if key in _processing_tasks or is_pipeline_active(group_number, lecture_number):
                continue
            _processing_tasks[key] = datetime.now()
            try:
                create_pipeline(group_number, lecture_number, meeting_id=str(poll_id))
            except ValueError:
                pass  # Pipeline state already exists — that's fine

        logger.info(
            "[startup-recovery] Starting pipeline for UNPROCESSED recording: "
            "Group %d, Lecture #%d, poll_id=%s",
            group_number, lecture_number, poll_id,
        )

        # Run in background thread (non-blocking)
        loop = asyncio.get_running_loop()
        loop.run_in_executor(
            None,
            _run_post_meeting_pipeline,
            group_number,
            lecture_number,
            poll_id,
        )


@asynccontextmanager
async def _lifespan(application: FastAPI):  # noqa: ARG001
    eviction_task = asyncio.create_task(_eviction_loop())

    # Startup recovery: check for unprocessed recordings
    try:
        await _check_unprocessed_recordings()
    except Exception as exc:
        logger.error("[startup-recovery] Unexpected error: %s", exc, exc_info=True)

    # Rebuild in-memory dedup cache from persistent pipeline state files
    _rebuild_task_cache()

    # Clean up old completed and stale failed pipeline state files
    cleanup_completed(max_age_hours=24)
    cleanup_stale_failed(max_age_hours=12)

    try:
        yield
    finally:
        eviction_task.cancel()
        try:
            await eviction_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Training Agent",
    description="Webhook server for Zoom recording processing and AI analysis",
    version="1.0.0",
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=None if IS_RAILWAY else "/openapi.json",
    lifespan=_lifespan,
)

# Reject requests with Host headers from unknown origins.
# On Railway, the public hostname is dynamic (*.up.railway.app), so we
# must allow it.  RAILWAY_PUBLIC_DOMAIN is auto-set by Railway when a
# public domain is configured.
import os as _os  # noqa: E402

_allowed_hosts = ["localhost", "127.0.0.1", f"localhost:{SERVER_PORT}",
                  f"127.0.0.1:{SERVER_PORT}"]
_railway_domain = _os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
if _railway_domain:
    _allowed_hosts.append(_railway_domain)
_server_public_url = _os.getenv("SERVER_PUBLIC_URL", "")
if _server_public_url:
    from urllib.parse import urlparse as _urlparse
    _parsed = _urlparse(_server_public_url)
    if _parsed.hostname:
        _allowed_hosts.append(_parsed.hostname)

# Allow Cloudflare quick tunnels (local development)
if not IS_RAILWAY:
    _allowed_hosts.append("*.trycloudflare.com")

# On Railway, the internal health checker and proxy use IP-based Host headers
# that can't be enumerated. Railway's own proxy already validates external
# hosts, so we use wildcard to avoid rejecting legitimate internal traffic.
if IS_RAILWAY:
    _allowed_hosts = ["*"]

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_allowed_hosts,
)

# Explicit CORS deny — this is a webhook API, not a browser-facing app.
# Adding this explicitly prevents accidental relaxation by future developers.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware, allow_origins=[], allow_methods=["POST", "GET"])

# Rate limiting
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Security + correlation ID headers middleware — must be defined before routes
@app.middleware("http")
async def add_security_headers(request: Request, call_next) -> JSONResponse:
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4())[:8])
    request.state.correlation_id = correlation_id
    logger.info("[%s] %s %s", correlation_id, request.method, request.url.path)
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
        "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    return response


assistant: WhatsAppAssistant | None = WhatsAppAssistant() if _assistant_available else None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

_DRIVE_FOLDER_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{10,100}$")


class ProcessRecordingRequest(BaseModel):
    """Payload from n8n when a Zoom recording is ready.

    download_url and access_token can be empty or "auto" — in that case, the
    /process-recording endpoint will use the Zoom polling pipeline
    (scheduler._run_post_meeting_pipeline) to auto-discover the recording.
    """

    download_url: str = ""
    access_token: str = ""
    group_number: int
    lecture_number: int
    drive_folder_id: str = ""

    def __init__(self, **data):  # type: ignore[override]
        super().__init__(**data)
        if self.drive_folder_id and not _DRIVE_FOLDER_ID_RE.match(self.drive_folder_id):
            raise ValueError("Invalid Drive folder ID format")


class CallbackPayload(BaseModel):
    """Payload sent back to n8n after processing."""

    status: str  # "success" or "error"
    group_number: int
    lecture_number: int
    summary_doc_url: str | None = None
    drive_recording_url: str | None = None
    gap_analysis_text: str | None = None
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

def verify_webhook_secret(authorization: str | None = Header(None)) -> None:
    """Validate the webhook secret from the Authorization header.

    Fails closed: if WEBHOOK_SECRET is not configured, all requests are
    rejected to prevent accidental open access in production.
    """
    if not WEBHOOK_SECRET:
        logger.error("WEBHOOK_SECRET not configured — rejecting request (fail closed)")
        raise HTTPException(
            status_code=503,
            detail="Server misconfigured: WEBHOOK_SECRET not set",
        )

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    expected = f"Bearer {WEBHOOK_SECRET}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")


# ---------------------------------------------------------------------------
# Background Processing
# ---------------------------------------------------------------------------

async def process_recording_task(payload: ProcessRecordingRequest) -> None:
    """Background task: download → upload to Drive → run full analysis pipeline.

    Delegates all analysis, Drive uploads, WhatsApp notifications, and Pinecone
    indexing to ``transcribe_and_index()`` from transcribe_lecture.py, avoiding
    pipeline duplication.

    Long-running sync calls (Drive upload, analysis pipeline) are wrapped in
    ``asyncio.to_thread()`` to avoid blocking the event loop.
    """
    import asyncio

    group = payload.group_number
    lecture = payload.lecture_number
    lecture_folder_name = get_lecture_folder_name(lecture)

    logger.info(
        "Starting processing: Group %d, Lecture #%d", group, lecture
    )

    local_path = None
    try:
        # Step 0: Validate download URL (SSRF prevention)
        parsed = urlparse(payload.download_url)
        if parsed.scheme != "https":
            raise ValueError(f"Only HTTPS download URLs allowed, got: {parsed.scheme}")
        hostname = (parsed.hostname or "").lower()
        if hostname != "zoom.us" and not hostname.endswith(".zoom.us"):
            raise ValueError(f"Download URL must be from zoom.us, got: {parsed.hostname}")

        # Step 1: Download the Zoom recording
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"group{group}_lecture{lecture}_{timestamp}.mp4"
        local_path = TMP_DIR / filename

        logger.info("Downloading recording to %s...", local_path)
        await _download_recording(
            payload.download_url, payload.access_token, local_path
        )
        file_size_mb = local_path.stat().st_size / (1024 * 1024)
        logger.info("Download complete: %.1f MB", file_size_mb)

        # Step 2: Ensure lecture subfolder exists and upload recording to Drive
        # Wrapped in to_thread() to avoid blocking the event loop
        service = await asyncio.to_thread(get_drive_service)
        lecture_folder_id = await asyncio.to_thread(
            ensure_folder, service, lecture_folder_name, payload.drive_folder_id
        )
        logger.info("Uploading recording to Google Drive...")
        recording_file_id = await asyncio.to_thread(upload_file, local_path, lecture_folder_id)
        drive_recording_url = get_drive_file_url(recording_file_id)
        logger.info("Recording uploaded: %s", drive_recording_url)

        # Step 3: Run the full analysis pipeline (transcribe → analyze →
        # Drive summary + private report → WhatsApp notifications → Pinecone)
        logger.info("Running full analysis pipeline...")
        index_counts = await asyncio.to_thread(transcribe_and_index, group, lecture, local_path)

        # Step 4: Callback to n8n with success
        await _send_callback(CallbackPayload(
            status="success",
            group_number=group,
            lecture_number=lecture,
            drive_recording_url=drive_recording_url,
        ))

        logger.info(
            "Processing complete: Group %d, Lecture #%d (%d vectors indexed)",
            group, lecture, sum(index_counts.values()),
        )

    except Exception as e:
        error_msg = f"Processing failed: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)

        # Mark pipeline as failed in state file
        try:
            from tools.core.pipeline_state import load_state as _load_ps, mark_failed as _mark_failed_ps
            _ps = _load_ps(group, lecture)
            if _ps and _ps.state not in ("COMPLETE", "FAILED"):
                _mark_failed_ps(_ps, str(e))
        except Exception as state_err:
            logger.warning("Failed to mark pipeline state as FAILED: %s", state_err)

        try:
            await _send_callback(CallbackPayload(
                status="error",
                group_number=group,
                lecture_number=lecture,
                error_message=str(e),
            ))
        except Exception as cb_err:
            logger.warning("n8n callback failed: %s", cb_err)

        # Last-resort alert — ensures Tornike knows even if n8n callback fails
        try:
            await asyncio.to_thread(
                alert_operator,
                f"Pipeline FAILED for Group {group}, Lecture #{lecture}.\n"
                f"Error: {e}",
            )
        except Exception as alert_err:
            logger.error(
                "CRITICAL: Both pipeline AND alert_operator failed for G%d L%d. "
                "Alert error: %s. Original error: %s",
                group, lecture, alert_err, e,
            )

    finally:
        key = _task_key(group, lecture)
        _processing_tasks.pop(key, None)

        if local_path and local_path.exists():
            local_path.unlink()
            logger.info("Cleaned up temp file: %s", local_path)


async def _download_recording(url: str, access_token: str, dest: Path) -> None:
    """Download a Zoom recording with streaming for large files.

    Validates the final URL after redirects to prevent SSRF via open redirects.
    Timeout of 1800s (30 min) accounts for Railway's variable network speed.
    """
    dest = Path(dest)
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(1800, connect=30)) as client:
        async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
            response.raise_for_status()

            # SSRF guard: validate final URL after redirects
            final_host = (response.url.host or "").lower()
            is_zoom = final_host == "zoom.us" or final_host.endswith(".zoom.us")
            is_zoomgov = final_host == "zoomgov.com" or final_host.endswith(".zoomgov.com")
            if not is_zoom and not is_zoomgov:
                raise ValueError(
                    f"Download redirected to untrusted host: {final_host}"
                )

            with open(dest, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)


async def _send_callback(payload: CallbackPayload) -> None:
    """Send processing results back to n8n webhook."""
    if not N8N_CALLBACK_URL:
        logger.warning("N8N_CALLBACK_URL not configured — skipping callback")
        return

    headers = {}
    if WEBHOOK_SECRET:
        headers["Authorization"] = f"Bearer {WEBHOOK_SECRET}"

    import asyncio

    for attempt in range(1, 4):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    N8N_CALLBACK_URL,
                    json=payload.model_dump(),
                    headers=headers,
                )
                response.raise_for_status()
                logger.info("Callback sent to n8n: status=%s", payload.status)
                return
        except (httpx.HTTPStatusError, httpx.TransportError, httpx.TimeoutException) as e:
            if attempt < 3:
                delay = 5 * attempt
                logger.warning(
                    "Callback attempt %d/3 failed: %s — retrying in %ds",
                    attempt, e, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error("Failed to send callback to n8n after 3 attempts: %s", e)
                try:
                    alert_operator(f"n8n callback failed after 3 retries: {e}")
                except Exception as alert_err:
                    logger.error("alert_operator also failed: %s", alert_err)
        except Exception as e:
            logger.error("Callback failed with non-retryable error: %s", e)
            try:
                alert_operator(f"n8n callback failed (non-retryable): {e}")
            except Exception as alert_err:
                logger.error("alert_operator also failed: %s", alert_err)
            return


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
@limiter.limit("60/minute")
async def health_check(request: Request):
    """Health check endpoint with basic dependency verification."""
    checks: dict[str, str] = {}

    # Check tmp directory is writable
    try:
        test_file = TMP_DIR / ".health_check"
        test_file.write_text("ok")
        test_file.unlink()
        checks["tmp_dir"] = "ok"
    except Exception as e:
        checks["tmp_dir"] = f"error: {e}"

    # Check critical env vars are present
    checks["webhook_secret"] = "configured" if WEBHOOK_SECRET else "MISSING"
    checks["n8n_callback"] = "configured" if N8N_CALLBACK_URL else "not set"

    # Report in-flight tasks
    checks["tasks_in_progress"] = str(len(_processing_tasks))

    overall = "healthy" if checks.get("tmp_dir") == "ok" and WEBHOOK_SECRET else "degraded"
    status_code = 200 if overall == "healthy" else 503

    return JSONResponse(
        content={
            "status": overall,
            "service": "training-agent",
            "timestamp": datetime.now().isoformat(),
            "checks": checks,
        },
        status_code=status_code,
    )


@app.get("/dashboard")
@limiter.limit("10/minute")
async def dashboard(request: Request):
    """Render the analytics dashboard as an HTML page."""
    try:
        from tools.services.analytics import get_dashboard_data, render_dashboard_html
        data = get_dashboard_data()
        html = render_dashboard_html(data)
        return HTMLResponse(content=html)
    except Exception as exc:
        logger.error("Dashboard render failed: %s", exc)
        return HTMLResponse(
            content=f"<h1>Dashboard Error</h1><pre>{exc}</pre>",
            status_code=500,
        )


@app.get("/dashboard/data")
@limiter.limit("10/minute")
async def dashboard_data(request: Request):
    """Return raw dashboard data as JSON (for custom frontends)."""
    try:
        from tools.services.analytics import get_dashboard_data
        return get_dashboard_data()
    except Exception as exc:
        logger.error("Dashboard data failed: %s", exc)
        return JSONResponse(content={"error": str(exc)}, status_code=500)


@app.post("/whatsapp-incoming")
@limiter.limit("30/minute")
async def whatsapp_incoming(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None),
):
    """Receive incoming WhatsApp messages from Green API webhook.

    Green API sends notifications for all incoming messages.
    We process them in the background to return 200 immediately.

    Authentication: same Bearer token as /process-recording.
    Configure Green API webhookUrlToken to send this header.
    """
    verify_webhook_secret(authorization)

    # Parse the raw JSON (Green API format varies)
    body = await request.json()

    # Only process incoming text messages
    type_webhook = body.get("typeWebhook")
    if type_webhook != "incomingMessageReceived":
        return {"status": "ignored", "reason": f"type: {type_webhook}"}

    message_data = body.get("messageData", {})
    type_message = message_data.get("typeMessage")

    # Extract text from different message types
    sender_data = body.get("senderData", {})
    text = ""
    quoted_text = ""
    if type_message == "textMessage":
        text = message_data.get("textMessageData", {}).get("textMessage", "")
    elif type_message in ("extendedTextMessage", "quotedMessage"):
        ext_data = message_data.get("extendedTextMessageData", {})
        text = ext_data.get("text", "")
        # Extract quoted/replied-to message text for context
        quoted_text = ext_data.get("quotedMessage", {}).get("textMessage", "") if ext_data.get("quotedMessage") else ""
    else:
        return {"status": "ignored", "reason": f"message type: {type_message}"}

    if not text.strip():
        return {"status": "ignored", "reason": "empty text"}

    # Skip messages sent by the bot itself (prevents infinite loops)
    if message_data.get("fromMe", False):
        return {"status": "ignored", "reason": "own message"}

    if not _assistant_available or assistant is None:
        logger.warning("WhatsApp assistant not available — ignoring incoming message")
        return {"status": "ignored", "reason": "assistant not available"}

    incoming = IncomingMessage(
        chat_id=sender_data.get("chatId", ""),
        sender_id=sender_data.get("sender", ""),
        sender_name=sender_data.get("senderName", ""),
        text=text,
        quoted_text=quoted_text,
        timestamp=body.get("timestamp", 0),
    )

    # Process in background
    background_tasks.add_task(_handle_assistant_message, incoming)

    return {"status": "accepted"}


async def _handle_assistant_message(message: IncomingMessage) -> None:
    """Background task: run the assistant pipeline."""
    try:
        result = await assistant.handle_message(message)
        if result:
            logger.info("Assistant responded in %s", message.chat_id[:20])
        else:
            logger.debug(
                "Assistant chose not to respond to message in %s",
                message.chat_id[:20],
            )
    except Exception as e:
        logger.error("Assistant error: %s", e, exc_info=True)
        try:
            alert_operator(f"WhatsApp assistant crashed: {e}")
        except Exception as alert_err:
            logger.error("alert_operator also failed: %s", alert_err)


def _handle_zoom_crc(body: dict) -> dict:
    """Handle Zoom endpoint.url_validation (CRC challenge-response)."""
    plain_token = body.get("payload", {}).get("plainToken", "")
    if not plain_token or len(plain_token) > 256:
        raise HTTPException(status_code=400, detail="Invalid plainToken")
    if not ZOOM_WEBHOOK_SECRET_TOKEN:
        raise HTTPException(status_code=503, detail="ZOOM_WEBHOOK_SECRET_TOKEN not configured")
    encrypted_token = hmac.new(
        ZOOM_WEBHOOK_SECRET_TOKEN.encode(),
        plain_token.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {"plainToken": plain_token, "encryptedToken": encrypted_token}


def _verify_zoom_signature(raw_body: bytes, request: Request) -> None:
    """Verify Zoom HMAC-SHA256 signature and reject stale timestamps."""
    if not ZOOM_WEBHOOK_SECRET_TOKEN:
        raise HTTPException(status_code=503, detail="ZOOM_WEBHOOK_SECRET_TOKEN not configured")

    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Zoom signature headers")

    try:
        ts_age = abs(time.time() - int(timestamp))
        if ts_age > 300:
            logger.warning("Zoom webhook timestamp too old: %s seconds", ts_age)
            raise HTTPException(status_code=401, detail="Zoom webhook timestamp expired")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid timestamp header")

    message = f"v0:{timestamp}:{raw_body.decode()}"
    expected_sig = "v0=" + hmac.new(
        ZOOM_WEBHOOK_SECRET_TOKEN.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        logger.warning("Zoom webhook signature mismatch — rejecting request")
        raise HTTPException(status_code=401, detail="Invalid Zoom webhook signature")


def _extract_recording_context(body: dict) -> dict | None:
    """Extract group, lecture, and recording info from recording.completed event.

    Returns None if the event should be ignored (no MP4, unknown group).
    """
    payload = body.get("payload", {})
    obj = payload.get("object", {})
    topic = obj.get("topic", "")
    recordings = obj.get("recording_files", [])

    video = next(
        (r for r in recordings
         if r.get("file_type") == "MP4"
         and r.get("recording_type") == "shared_screen_with_speaker_view"),
        None,
    ) or next(
        (r for r in recordings if r.get("file_type") == "MP4"),
        None,
    )
    if not video:
        logger.warning("Zoom webhook: no MP4 recording found in event")
        return None

    group_number = extract_group_from_topic(topic)
    if group_number is None:
        logger.warning("Zoom webhook: could not determine group from topic: %s", topic)
        return None

    start_time = obj.get("start_time", "")
    try:
        meeting_date = datetime.fromisoformat(start_time.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        meeting_date = datetime.now(TBILISI_TZ).date()

    lecture_number = get_lecture_number(group_number, for_date=meeting_date)
    if lecture_number == 0:
        lecture_number = 1

    return {
        "group_number": group_number,
        "lecture_number": lecture_number,
        "download_url": video.get("download_url", ""),
        "access_token": body.get("download_token", ""),
        "drive_folder_id": GROUPS[group_number].get("drive_folder_id", ""),
        "topic": topic,
    }


def _handle_recording_completed_via_polling(
    body: dict,
    ctx: dict,
    background_tasks: BackgroundTasks,
) -> dict:
    """Fallback when recording.completed has an empty download_url.

    Uses the meeting.ended-style polling pipeline to discover and download
    the recording via Zoom API instead of the direct download URL.
    """
    from tools.app.scheduler import _run_post_meeting_pipeline

    group_number = ctx["group_number"]
    lecture_number = ctx["lecture_number"]

    # Try to extract meeting ID from the webhook payload
    payload_obj = body.get("payload", {}).get("object", {})
    meeting_uuid = str(payload_obj.get("uuid", ""))
    meeting_id = str(payload_obj.get("id", ""))
    poll_id = meeting_uuid or meeting_id

    if not poll_id:
        # Last resort: use configured meeting ID for the group
        group_cfg = GROUPS.get(group_number, {})
        poll_id = group_cfg.get("zoom_meeting_id", "")

    if not poll_id:
        logger.error(
            "recording.completed fallback: no meeting ID for G%d L%d — cannot poll",
            group_number, lecture_number,
        )
        return {"status": "error", "reason": "no meeting ID and empty download URL"}

    _evict_stale_tasks()
    with _processing_lock:
        key = _task_key(group_number, lecture_number)
        if key in _processing_tasks or is_pipeline_active(group_number, lecture_number):
            return {"status": "duplicate", "message": f"{key} already processing"}
        _processing_tasks[key] = datetime.now()
        try:
            create_pipeline(group_number, lecture_number, meeting_id=poll_id)
        except ValueError:
            pass

    logger.info(
        "recording.completed fallback → polling pipeline: G%d L%d (poll_id=%s)",
        group_number, lecture_number, poll_id,
    )

    def _run_and_cleanup() -> None:
        try:
            _run_post_meeting_pipeline(group_number, lecture_number, poll_id)
        finally:
            _processing_tasks.pop(key, None)

    background_tasks.add_task(_run_and_cleanup)

    return {
        "status": "accepted",
        "mode": "polling-fallback",
        "group": group_number,
        "lecture": lecture_number,
    }


def _handle_meeting_ended(body: dict, background_tasks: BackgroundTasks) -> dict:
    """Handle meeting.ended event with duration-based processing gate.

    User requirement: if meeting lasted ≥2 hours → start processing.
    If <2 hours → ignore (temporary disconnect, they'll rejoin).
    """
    payload = body.get("payload", {})
    obj = payload.get("object", {})

    meeting_id = str(obj.get("id", ""))
    meeting_uuid = str(obj.get("uuid", ""))
    topic = obj.get("topic", "")
    start_time_str = obj.get("start_time", "")
    duration_minutes = obj.get("duration", 0)

    # Determine group from topic
    group_number = extract_group_from_topic(topic)
    if group_number is None:
        logger.warning("[meeting.ended] Unknown group from topic: %s", topic)
        return {"status": "ignored", "reason": "unknown group"}

    # Calculate actual duration from Zoom's start_time and end_time
    end_time_str = obj.get("end_time", "")
    actual_duration = duration_minutes
    try:
        start_dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        if end_time_str:
            end_dt = datetime.fromisoformat(end_time_str.replace("Z", "+00:00"))
        else:
            end_dt = datetime.now(start_dt.tzinfo)
        actual_duration = (end_dt - start_dt).total_seconds() / 60
    except (ValueError, AttributeError):
        logger.warning("[meeting.ended] Could not parse timestamps, using Zoom duration: %d min", duration_minutes)

    logger.info(
        "[meeting.ended] Meeting %s ended — Group %d, topic='%s', duration=%.0f min",
        meeting_id, group_number, topic, actual_duration,
    )

    # DURATION GATE
    if actual_duration < MINIMUM_LECTURE_DURATION_MINUTES:
        logger.info(
            "[meeting.ended] Duration %.0f min < %d min — temporary disconnect, NOT processing",
            actual_duration, MINIMUM_LECTURE_DURATION_MINUTES,
        )
        return {
            "status": "ignored",
            "reason": "duration_below_threshold",
            "duration_minutes": round(actual_duration),
            "threshold_minutes": MINIMUM_LECTURE_DURATION_MINUTES,
        }

    # Duration ≥ 2 hours — real lecture end, start pipeline
    try:
        meeting_date = datetime.fromisoformat(start_time_str.replace("Z", "+00:00")).date()
    except (ValueError, AttributeError):
        meeting_date = datetime.now(TBILISI_TZ).date()

    lecture_number = get_lecture_number(group_number, for_date=meeting_date)
    if lecture_number == 0:
        lecture_number = 1

    _evict_stale_tasks()
    with _processing_lock:
        key = _task_key(group_number, lecture_number)
        if key in _processing_tasks or is_pipeline_active(group_number, lecture_number):
            logger.info("[meeting.ended] %s already processing — skipping", key)
            return {"status": "duplicate", "message": f"{key} already processing"}
        _processing_tasks[key] = datetime.now()
        try:
            create_pipeline(group_number, lecture_number, meeting_id=str(meeting_uuid or meeting_id))
        except ValueError:
            pass  # Pipeline state already exists — that's fine

    # Use meeting UUID for polling (Zoom recordings API needs UUID, not numeric ID)
    poll_id = meeting_uuid if meeting_uuid else meeting_id

    logger.info(
        "[meeting.ended] Duration %.0f min ≥ %d min — starting post-meeting pipeline "
        "for Group %d, Lecture #%d (poll_id=%s)",
        actual_duration, MINIMUM_LECTURE_DURATION_MINUTES,
        group_number, lecture_number, poll_id,
    )

    # Run post-meeting pipeline in background, clean up dedup key on completion
    from tools.app.scheduler import _run_post_meeting_pipeline

    def _run_and_cleanup() -> None:
        try:
            _run_post_meeting_pipeline(group_number, lecture_number, poll_id)
        finally:
            _processing_tasks.pop(key, None)

    background_tasks.add_task(_run_and_cleanup)

    return {
        "status": "accepted",
        "trigger": "meeting.ended",
        "group": group_number,
        "lecture": lecture_number,
        "duration_minutes": round(actual_duration),
    }


@app.post("/zoom-webhook")
@limiter.limit("10/minute")
async def zoom_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Receive Zoom webhook events (CRC + meeting.ended + recording.completed).

    Authentication: Zoom HMAC-SHA256 signature (not WEBHOOK_SECRET).
    See CLAUDE.md for why this is an intentional exception.
    """
    raw_body = await request.body()
    body = await request.json()
    event = body.get("event", "")

    if event == "endpoint.url_validation":
        return _handle_zoom_crc(body)

    _verify_zoom_signature(raw_body, request)

    # Handle meeting.ended → duration gate → start pipeline if ≥2hr
    if event == "meeting.ended":
        return _handle_meeting_ended(body, background_tasks)

    # Handle recording.completed → direct download processing
    if event == "recording.completed":
        ctx = _extract_recording_context(body)
        if ctx is None:
            return {"status": "ignored", "reason": "no valid recording or unknown group"}

        # Validate download_url BEFORE registering in dedup tracker
        download_url = ctx["download_url"]
        if not download_url or not download_url.strip():
            logger.warning(
                "Zoom webhook: recording.completed has EMPTY download_url — "
                "falling back to polling pipeline (Group %d, Lecture #%d)",
                ctx["group_number"], ctx["lecture_number"],
            )
            # Fall through to meeting.ended-style polling instead of failing silently
            return _handle_recording_completed_via_polling(
                body, ctx, background_tasks,
            )

        logger.info(
            "Zoom webhook: recording.completed — Group %d, Lecture #%d, topic=%s",
            ctx["group_number"], ctx["lecture_number"], ctx["topic"],
        )

        _evict_stale_tasks()
        with _processing_lock:
            key = _task_key(ctx["group_number"], ctx["lecture_number"])
            if key in _processing_tasks or is_pipeline_active(ctx["group_number"], ctx["lecture_number"]):
                return {"status": "duplicate", "message": f"{key} already processing"}
            _processing_tasks[key] = datetime.now()
            try:
                create_pipeline(ctx["group_number"], ctx["lecture_number"], meeting_id="")
            except ValueError:
                pass  # Pipeline state already exists — that's fine

        proc_payload = ProcessRecordingRequest(
            download_url=download_url,
            access_token=ctx["access_token"],
            group_number=ctx["group_number"],
            lecture_number=ctx["lecture_number"],
            drive_folder_id=ctx["drive_folder_id"],
        )
        background_tasks.add_task(process_recording_task, proc_payload)

        return {"status": "accepted", "group": ctx["group_number"], "lecture": ctx["lecture_number"]}

    return {"status": "ignored", "event": event}


@app.post("/process-recording")
@limiter.limit("5/minute")
async def process_recording(
    request: Request,
    payload: ProcessRecordingRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None),
):
    """Receive recording details from n8n and start async processing.

    Returns immediately with 200 OK. Processing happens in background.
    Results are sent back to n8n via callback webhook.
    """
    verify_webhook_secret(authorization)

    # Input validation
    if payload.group_number not in (1, 2):
        raise HTTPException(status_code=422, detail=f"Invalid group_number: {payload.group_number}")
    if not (1 <= payload.lecture_number <= 15):
        raise HTTPException(status_code=422, detail=f"Invalid lecture_number: {payload.lecture_number}")

    # Evict stale tasks before checking (prevents permanent blocking from hung pipelines)
    _evict_stale_tasks()

    key = _task_key(payload.group_number, payload.lecture_number)
    with _processing_lock:
        if key in _processing_tasks or is_pipeline_active(payload.group_number, payload.lecture_number):
            started = _processing_tasks.get(key)
            started_str = started.isoformat() if started else "unknown"
            logger.warning(
                "Duplicate request rejected: Group %d, Lecture #%d (in progress since %s)",
                payload.group_number, payload.lecture_number, started_str,
            )
            raise HTTPException(
                status_code=409,
                detail=f"Recording for Group {payload.group_number}, Lecture #{payload.lecture_number} "
                       f"is already being processed (started {started_str})",
            )
        _processing_tasks[key] = datetime.now()
        try:
            create_pipeline(payload.group_number, payload.lecture_number, meeting_id="")
        except ValueError:
            pass  # Pipeline state already exists — that's fine

    logger.info(
        "Received recording request: Group %d, Lecture #%d",
        payload.group_number,
        payload.lecture_number,
    )

    # If download_url is empty or "auto", use Zoom polling pipeline instead of direct download
    is_auto = not payload.download_url or payload.download_url.strip().lower() == "auto"
    if is_auto:
        from tools.app.scheduler import _run_post_meeting_pipeline

        # Need a meeting ID to poll Zoom — use the group's configured meeting ID
        group_cfg = GROUPS.get(payload.group_number, {})
        meeting_id = group_cfg.get("zoom_meeting_id", "")
        if not meeting_id:
            _processing_tasks.pop(key, None)
            raise HTTPException(
                status_code=422,
                detail=f"No zoom_meeting_id configured for Group {payload.group_number} "
                       f"— cannot auto-discover recording",
            )

        logger.info(
            "Auto-discovery mode: using Zoom polling pipeline (meeting_id=%s)",
            meeting_id,
        )

        def _run_auto(gn: int, ln: int, mid: str) -> None:
            try:
                _run_post_meeting_pipeline(gn, ln, mid)
            finally:
                _processing_tasks.pop(_task_key(gn, ln), None)

        # add_task expects a sync callable — _run_auto runs in a thread pool
        # Do NOT wrap in asyncio.to_thread — BackgroundTasks handles threading
        background_tasks.add_task(
            _run_auto,
            payload.group_number,
            payload.lecture_number,
            meeting_id,
        )

        return {
            "status": "accepted",
            "mode": "auto-discovery",
            "message": f"Auto-discovery pipeline started for Group {payload.group_number}, "
                       f"Lecture #{payload.lecture_number} (polling meeting {meeting_id})",
        }

    background_tasks.add_task(process_recording_task, payload)

    return {
        "status": "accepted",
        "message": f"Processing started for Group {payload.group_number}, Lecture #{payload.lecture_number}",
    }


# ---------------------------------------------------------------------------
# Manual trigger — operator use only
# ---------------------------------------------------------------------------


@app.post("/trigger-pre-meeting")
@limiter.limit("2/minute")
async def trigger_pre_meeting(
    request: Request,
    authorization: str | None = Header(None),
    group: int = 2,
):
    """Manually trigger a pre-meeting job for a group.

    Operator-only endpoint: requires WEBHOOK_SECRET.
    """
    verify_webhook_secret(authorization)
    if group not in GROUPS:
        raise HTTPException(status_code=422, detail=f"Invalid group: {group}")

    import asyncio

    from tools.app.scheduler import pre_meeting_job
    asyncio.ensure_future(pre_meeting_job(group_number=group))

    return {"status": "triggered", "group": group}


@app.post("/retry-latest")
@limiter.limit("2/minute")
async def retry_latest(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None),
):
    """Auto-discover and process the latest unprocessed recording.

    Queries Zoom for recent recordings, checks Pinecone to find ones not yet
    indexed, and starts the pipeline for the most recent unprocessed one.
    No parameters needed — fully automatic discovery.

    Operator-only endpoint: requires WEBHOOK_SECRET.
    """
    verify_webhook_secret(authorization)

    from tools.app.scheduler import _run_post_meeting_pipeline

    try:
        zm = __import__("tools.integrations.zoom_manager", fromlist=["zoom_manager"])
    except ImportError:
        raise HTTPException(status_code=503, detail="zoom_manager not available")

    # Search the last 3 days to catch weekend recordings
    today = datetime.now(TBILISI_TZ).date()
    from datetime import timedelta as _td
    from_date = (today - _td(days=3)).isoformat()
    to_date = today.isoformat()

    try:
        meetings = await asyncio.to_thread(
            zm.list_user_recordings, from_date, to_date,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Zoom API error: {exc}")

    if not meetings:
        return {"status": "no_recordings", "message": "No recordings found in the last 3 days"}

    # Sort by start_time descending (most recent first)
    meetings.sort(key=lambda m: m.get("start_time", ""), reverse=True)

    # Find the first unprocessed one
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
            meeting_date = today

        lecture_number = get_lecture_number(group_number, for_date=meeting_date)
        if lecture_number == 0:
            continue

        # Check dedup (in-memory cache + persistent pipeline state)
        key = _task_key(group_number, lecture_number)
        with _processing_lock:
            if key in _processing_tasks:
                continue
        if is_pipeline_active(group_number, lecture_number) or is_pipeline_done(group_number, lecture_number):
            continue

        # Check Pinecone
        already_indexed = False
        try:
            from tools.integrations.knowledge_indexer import get_pinecone_index
            index = await asyncio.to_thread(get_pinecone_index)
            dummy_embedding = [0.0] * 3072
            result = await asyncio.to_thread(
                lambda: index.query(
                    vector=dummy_embedding,
                    top_k=1,
                    filter={
                        "group_number": {"$eq": group_number},
                        "lecture_number": {"$eq": lecture_number},
                    },
                )
            )
            if result.get("matches"):
                already_indexed = True
        except Exception as exc:
            logger.warning("[retry-latest] Pinecone check failed: %s", exc)

        if already_indexed:
            continue

        # Found an unprocessed recording — start pipeline
        meeting_uuid = meeting.get("uuid", "")
        meeting_id_val = str(meeting.get("id", ""))
        poll_id = meeting_uuid or meeting_id_val

        if not poll_id:
            continue

        _evict_stale_tasks()
        with _processing_lock:
            if key in _processing_tasks or is_pipeline_active(group_number, lecture_number):
                continue
            _processing_tasks[key] = datetime.now()
            try:
                create_pipeline(group_number, lecture_number, meeting_id=str(poll_id))
            except ValueError:
                pass  # Pipeline state already exists — that's fine

        logger.info(
            "[retry-latest] Starting pipeline for Group %d, Lecture #%d (poll_id=%s)",
            group_number, lecture_number, poll_id,
        )

        def _run_retry(gn: int, ln: int, pid: str) -> None:
            try:
                _run_post_meeting_pipeline(gn, ln, pid)
            finally:
                _processing_tasks.pop(_task_key(gn, ln), None)

        # add_task expects a sync callable — do NOT wrap in asyncio.to_thread
        background_tasks.add_task(
            _run_retry, group_number, lecture_number, poll_id,
        )

        return {
            "status": "accepted",
            "group": group_number,
            "lecture": lecture_number,
            "poll_id": poll_id,
            "topic": topic,
        }

    return {
        "status": "all_processed",
        "message": "All recent recordings are already processed",
        "recordings_checked": len(meetings),
    }


class ManualTriggerRequest(BaseModel):
    """Payload for manual pipeline trigger from Google Drive file."""

    group_number: int
    lecture_number: int
    drive_file_id: str


async def _manual_pipeline_task(
    group_number: int,
    lecture_number: int,
    drive_file_id: str,
) -> None:
    """Background task: download from Drive → run full analysis pipeline."""
    from tools.integrations.gdrive_manager import download_file

    local_path = None
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"group{group_number}_lecture{lecture_number}_{timestamp}.mp4"
        local_path = TMP_DIR / filename

        logger.info(
            "Manual trigger: downloading Drive file %s for Group %d, Lecture #%d...",
            drive_file_id, group_number, lecture_number,
        )
        await asyncio.to_thread(download_file, drive_file_id, local_path)
        file_size_mb = local_path.stat().st_size / (1024 * 1024)
        logger.info("Manual trigger: download complete (%.1f MB)", file_size_mb)

        logger.info("Manual trigger: running full analysis pipeline...")
        index_counts = await asyncio.to_thread(
            transcribe_and_index, group_number, lecture_number, local_path,
        )
        logger.info(
            "Manual trigger: pipeline complete for Group %d, Lecture #%d (%d vectors)",
            group_number, lecture_number, sum(index_counts.values()),
        )
    except Exception as e:
        error_msg = f"Manual pipeline failed: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)
        await asyncio.to_thread(
            alert_operator,
            f"Manual pipeline FAILED for Group {group_number}, Lecture #{lecture_number}.\n"
            f"Error: {e}",
        )
    finally:
        key = _task_key(group_number, lecture_number)
        _processing_tasks.pop(key, None)
        if local_path and local_path.exists():
            local_path.unlink()
            logger.info("Cleaned up temp file: %s", local_path)


@app.post("/manual-trigger")
@limiter.limit("2/minute")
async def manual_trigger(
    request: Request,
    payload: ManualTriggerRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(None),
):
    """Manually trigger the analysis pipeline from a Google Drive recording.

    Operator-only endpoint: requires WEBHOOK_SECRET.
    Downloads the recording from Drive, then runs the full pipeline
    (transcribe → analyze → Drive upload → WhatsApp → Pinecone).
    """
    verify_webhook_secret(authorization)

    if payload.group_number not in (1, 2):
        raise HTTPException(status_code=422, detail=f"Invalid group_number: {payload.group_number}")
    if not (1 <= payload.lecture_number <= 15):
        raise HTTPException(status_code=422, detail=f"Invalid lecture_number: {payload.lecture_number}")
    if not _DRIVE_FOLDER_ID_RE.match(payload.drive_file_id):
        raise HTTPException(status_code=422, detail="Invalid Drive file ID format")

    _evict_stale_tasks()

    key = _task_key(payload.group_number, payload.lecture_number)
    with _processing_lock:
        if key in _processing_tasks or is_pipeline_active(payload.group_number, payload.lecture_number):
            started = _processing_tasks.get(key)
            started_str = started.isoformat() if started else "unknown"
            raise HTTPException(
                status_code=409,
                detail=f"Group {payload.group_number}, Lecture #{payload.lecture_number} "
                       f"is already being processed (started {started_str})",
            )
        _processing_tasks[key] = datetime.now()
        try:
            create_pipeline(payload.group_number, payload.lecture_number, meeting_id="")
        except ValueError:
            pass  # Pipeline state already exists — that's fine

    logger.info(
        "Manual trigger: Group %d, Lecture #%d, Drive file: %s",
        payload.group_number, payload.lecture_number, payload.drive_file_id,
    )

    background_tasks.add_task(
        _manual_pipeline_task,
        payload.group_number,
        payload.lecture_number,
        payload.drive_file_id,
    )

    return {
        "status": "accepted",
        "message": f"Manual pipeline started for Group {payload.group_number}, "
                   f"Lecture #{payload.lecture_number} (Drive file: {payload.drive_file_id})",
    }


# ---------------------------------------------------------------------------
# Analytics dashboard
# ---------------------------------------------------------------------------

_dashboard_cache: tuple[float, str] | None = None


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("30/minute")
async def analytics_dashboard(
    request: Request,
    authorization: str | None = Header(None),
):
    """Serve the analytics dashboard HTML.

    Operator-only endpoint: requires WEBHOOK_SECRET.
    """
    verify_webhook_secret(authorization)
    import time as _time
    # HTML cache: 5-min TTL (data changes at most twice per day)
    global _dashboard_cache
    now = _time.monotonic()
    if _dashboard_cache and (now - _dashboard_cache[0]) < 300:
        return HTMLResponse(content=_dashboard_cache[1])
    from tools.services.analytics import (
        get_dashboard_data,
        render_dashboard_html,
        sync_from_pinecone,
    )
    await asyncio.to_thread(sync_from_pinecone)
    data = await asyncio.to_thread(get_dashboard_data)
    html = render_dashboard_html(data)
    _dashboard_cache = (now, html)
    return HTMLResponse(content=html)


@app.get("/api/scores", include_in_schema=False)
@limiter.limit("60/minute")
async def api_scores(
    request: Request,
    authorization: str | None = Header(None),
    group: int | None = None,
):
    """Return raw lecture scores as JSON.

    Query param: group=1|2 (optional, omit for all groups).
    """
    verify_webhook_secret(authorization)
    if group is not None and group not in GROUPS:
        raise HTTPException(status_code=422, detail=f"Invalid group: {group}")

    from tools.services.analytics import get_all_scores
    rows = await asyncio.to_thread(get_all_scores, group)
    return {"scores": rows, "total": len(rows)}


@app.get("/api/stats", include_in_schema=False)
@limiter.limit("60/minute")
async def api_stats(
    request: Request,
    authorization: str | None = Header(None),
    group: int | None = None,
):
    """Return statistical analysis per dimension as JSON.

    Query param: group=1|2 (optional, omit for both groups).
    """
    verify_webhook_secret(authorization)
    if group is not None and group not in GROUPS:
        raise HTTPException(status_code=422, detail=f"Invalid group: {group}")

    from tools.services.analytics import get_dashboard_data
    data = await asyncio.to_thread(get_dashboard_data)

    if group is not None:
        return {
            "group": group,
            "lecture_count": data["groups"][group]["lecture_count"],
            "stats": data["groups"][group]["stats"],
            "best_lecture": data["groups"][group]["best_lecture"],
            "worst_lecture": data["groups"][group]["worst_lecture"],
        }

    return {
        "groups": {
            str(gn): {
                "lecture_count": data["groups"][gn]["lecture_count"],
                "stats": data["groups"][gn]["stats"],
                "best_lecture": data["groups"][gn]["best_lecture"],
                "worst_lecture": data["groups"][gn]["worst_lecture"],
            }
            for gn in (1, 2)
        },
        "cross_group": data["cross_group"],
        "generated_at": data["generated_at"],
    }


@app.post("/api/backfill-scores", include_in_schema=False)
@limiter.limit("2/minute")
async def api_backfill_scores(
    request: Request,
    authorization: str | None = Header(None),
):
    """Trigger backfill of scores from .tmp/ deep analysis files.

    Processes any existing deep_analysis text files that are not yet
    indexed in the analytics DB. Safe to call repeatedly — skips
    already-indexed lectures.
    """
    verify_webhook_secret(authorization)
    from tools.services.analytics import backfill_from_tmp
    result = await asyncio.to_thread(backfill_from_tmp)
    logger.info("Manual score backfill triggered: %s", result)
    return {"status": "ok", **result}


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # Railway injects PORT env var; fall back to configured SERVER_PORT
    port = int(_os.getenv("PORT", str(SERVER_PORT)))
    uvicorn.run(app, host=SERVER_HOST, port=port)
