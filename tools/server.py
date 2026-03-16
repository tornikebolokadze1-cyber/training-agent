"""FastAPI webhook server — bridge between n8n and Python tools."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from tools.config import (
    GROUPS,
    IS_RAILWAY,
    N8N_CALLBACK_URL,
    SERVER_HOST,
    SERVER_PORT,
    TBILISI_TZ,
    TMP_DIR,
    WEBHOOK_SECRET,
    ZOOM_WEBHOOK_SECRET_TOKEN,
    get_lecture_folder_name,
    get_lecture_number,
)
from tools.gdrive_manager import (
    ensure_folder,
    get_drive_service,
    upload_file,
)
from tools.transcribe_lecture import transcribe_and_index
from tools.whatsapp_sender import alert_operator

try:
    from tools.whatsapp_assistant import WhatsAppAssistant, IncomingMessage
    _assistant_available = True
except ImportError:
    _assistant_available = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-flight task tracking (deduplication + observability)
# ---------------------------------------------------------------------------
_processing_tasks: dict[str, datetime] = {}  # key: "g{group}_l{lecture}" -> start time
STALE_TASK_HOURS = 4  # Consider a task stale after 4 hours


def _task_key(group: int, lecture: int) -> str:
    return f"g{group}_l{lecture}"


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

app = FastAPI(
    title="Training Agent",
    description="Webhook server for Zoom recording processing and AI analysis",
    version="1.0.0",
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    openapi_url=None if IS_RAILWAY else "/openapi.json",
)

# Reject requests with Host headers from unknown origins.
# On Railway, the public hostname is dynamic (*.up.railway.app), so we
# must allow it.  RAILWAY_PUBLIC_DOMAIN is auto-set by Railway when a
# public domain is configured.
import os as _os

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
    return response


assistant: "WhatsAppAssistant | None" = WhatsAppAssistant() if _assistant_available else None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

_DRIVE_FOLDER_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{10,100}$")


class ProcessRecordingRequest(BaseModel):
    """Payload from n8n when a Zoom recording is ready."""

    download_url: str
    access_token: str
    group_number: int
    lecture_number: int
    drive_folder_id: str

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
        drive_recording_url = f"https://drive.google.com/file/d/{recording_file_id}/view"
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

        await _send_callback(CallbackPayload(
            status="error",
            group_number=group,
            lecture_number=lecture,
            error_message=str(e),
        ))

        # Last-resort alert — ensures Tornike knows even if n8n callback fails
        # Wrapped in to_thread because alert_operator makes blocking HTTP calls
        await asyncio.to_thread(
            alert_operator,
            f"Pipeline FAILED for Group {group}, Lecture #{lecture}.\n"
            f"Error: {e}",
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
    if type_message == "textMessage":
        text = message_data.get("textMessageData", {}).get("textMessage", "")
    elif type_message in ("extendedTextMessage", "quotedMessage"):
        text = message_data.get("extendedTextMessageData", {}).get("text", "")
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
        timestamp=body.get("timestamp", 0),
    )

    # Process in background
    background_tasks.add_task(_handle_assistant_message, incoming)

    return {"status": "accepted"}


async def _handle_assistant_message(message: "IncomingMessage") -> None:
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

    group_number = 0
    if "\u10ef\u10d2\u10e3\u10e4\u10d8 #1" in topic:
        group_number = 1
    elif "\u10ef\u10d2\u10e3\u10e4\u10d8 #2" in topic:
        group_number = 2

    if group_number == 0:
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


@app.post("/zoom-webhook")
@limiter.limit("10/minute")
async def zoom_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Receive Zoom webhook events (CRC validation + recording.completed).

    Authentication: Zoom HMAC-SHA256 signature (not WEBHOOK_SECRET).
    See CLAUDE.md for why this is an intentional exception.
    """
    raw_body = await request.body()
    body = await request.json()
    event = body.get("event", "")

    if event == "endpoint.url_validation":
        return _handle_zoom_crc(body)

    _verify_zoom_signature(raw_body, request)

    if event != "recording.completed":
        return {"status": "ignored", "event": event}

    ctx = _extract_recording_context(body)
    if ctx is None:
        return {"status": "ignored", "reason": "no valid recording or unknown group"}

    logger.info(
        "Zoom webhook: recording.completed — Group %d, Lecture #%d, topic=%s",
        ctx["group_number"], ctx["lecture_number"], ctx["topic"],
    )

    _evict_stale_tasks()
    key = _task_key(ctx["group_number"], ctx["lecture_number"])
    if key in _processing_tasks:
        return {"status": "duplicate", "message": f"{key} already processing"}
    _processing_tasks[key] = datetime.now()

    proc_payload = ProcessRecordingRequest(
        download_url=ctx["download_url"],
        access_token=ctx["access_token"],
        group_number=ctx["group_number"],
        lecture_number=ctx["lecture_number"],
        drive_folder_id=ctx["drive_folder_id"],
    )
    background_tasks.add_task(process_recording_task, proc_payload)

    return {"status": "accepted", "group": ctx["group_number"], "lecture": ctx["lecture_number"]}


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

    # Deduplication: reject if this group+lecture is already being processed.
    # Best-effort: the check-and-set is NOT locked, but since all async endpoint
    # code runs on a single event loop thread (no awaits between check and set),
    # concurrent requests are effectively serialised here.
    key = _task_key(payload.group_number, payload.lecture_number)
    if key in _processing_tasks:
        started = _processing_tasks[key]
        logger.warning(
            "Duplicate request rejected: Group %d, Lecture #%d (in progress since %s)",
            payload.group_number, payload.lecture_number, started.isoformat(),
        )
        raise HTTPException(
            status_code=409,
            detail=f"Recording for Group {payload.group_number}, Lecture #{payload.lecture_number} "
                   f"is already being processed (started {started.isoformat()})",
        )

    _processing_tasks[key] = datetime.now()

    logger.info(
        "Received recording request: Group %d, Lecture #%d",
        payload.group_number,
        payload.lecture_number,
    )

    background_tasks.add_task(process_recording_task, payload)

    return {
        "status": "accepted",
        "message": f"Processing started for Group {payload.group_number}, Lecture #{payload.lecture_number}",
    }


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
