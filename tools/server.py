"""FastAPI webhook server — bridge between n8n and Python tools."""

from __future__ import annotations

import hmac
import logging
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from tools.config import (
    N8N_CALLBACK_URL,
    SERVER_HOST,
    SERVER_PORT,
    TMP_DIR,
    WEBHOOK_SECRET,
    get_lecture_folder_name,
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


def _task_key(group: int, lecture: int) -> str:
    return f"g{group}_l{lecture}"


app = FastAPI(
    title="Training Agent",
    description="Webhook server for Zoom recording processing and AI analysis",
    version="1.0.0",
)

# Reject requests with Host headers from unknown origins
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", f"localhost:{SERVER_PORT}",
                   f"127.0.0.1:{SERVER_PORT}"],
)

assistant: "WhatsAppAssistant | None" = WhatsAppAssistant() if _assistant_available else None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ProcessRecordingRequest(BaseModel):
    """Payload from n8n when a Zoom recording is ready."""

    download_url: str
    access_token: str
    group_number: int
    lecture_number: int
    drive_folder_id: str


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
        if not parsed.hostname or not parsed.hostname.endswith("zoom.us"):
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
    """
    dest = Path(dest)
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30)) as client:
        async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
            response.raise_for_status()

            # SSRF guard: validate final URL after redirects
            final_host = response.url.host or ""
            if not final_host.endswith("zoom.us") and not final_host.endswith("zoomgov.com"):
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
        except Exception as e:
            logger.error("Callback failed with non-retryable error: %s", e)
            return


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
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

    return {
        "status": overall,
        "service": "training-agent",
        "timestamp": datetime.now().isoformat(),
        "checks": checks,
    }


@app.post("/whatsapp-incoming")
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


@app.post("/process-recording")
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
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
