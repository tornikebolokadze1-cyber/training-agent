"""FastAPI webhook server — bridge between n8n and Python tools."""

from __future__ import annotations

import hashlib
import hmac
import logging
import traceback
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
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
    create_google_doc,
    ensure_folder,
    get_drive_service,
    upload_file,
)
from tools.gemini_analyzer import analyze_lecture
from tools.whatsapp_sender import send_group_upload_notification, send_private_report

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Training Agent",
    description="Webhook server for Zoom recording processing and AI analysis",
    version="1.0.0",
)


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
    """Validate the webhook secret from the Authorization header."""
    if not WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET not configured — skipping auth")
        return

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    expected = f"Bearer {WEBHOOK_SECRET}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")


# ---------------------------------------------------------------------------
# Background Processing
# ---------------------------------------------------------------------------

async def process_recording_task(payload: ProcessRecordingRequest) -> None:
    """Background task: download → upload → analyze → callback.

    This runs asynchronously after the webhook returns 200.
    """
    group = payload.group_number
    lecture = payload.lecture_number
    lecture_folder_name = get_lecture_folder_name(lecture)

    logger.info(
        "Starting processing: Group %d, Lecture #%d", group, lecture
    )

    local_path = None
    try:
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

        # Step 2: Ensure lecture subfolder exists in Google Drive
        service = get_drive_service()
        lecture_folder_id = ensure_folder(
            service, lecture_folder_name, payload.drive_folder_id
        )

        # Step 3: Upload recording to Google Drive
        logger.info("Uploading recording to Google Drive...")
        recording_file_id = upload_file(local_path, lecture_folder_id)
        drive_recording_url = f"https://drive.google.com/file/d/{recording_file_id}/view"
        logger.info("Recording uploaded: %s", drive_recording_url)

        # Step 4: Analyze with Gemini (this is the long step)
        logger.info("Starting Gemini multimodal analysis...")
        analysis = analyze_lecture(str(local_path))

        # Step 5: Create summary document in Google Drive
        summary_title = f"{lecture_folder_name} — შეჯამება"
        summary_doc_id = create_google_doc(
            summary_title, analysis["summary"], lecture_folder_id
        )
        summary_doc_url = f"https://docs.google.com/document/d/{summary_doc_id}/edit"
        logger.info("Summary doc created: %s", summary_doc_url)

        # Step 6: Notify WhatsApp group that materials are uploaded
        try:
            send_group_upload_notification(
                group, lecture, drive_recording_url, summary_doc_url,
            )
            logger.info("WhatsApp group notified about uploaded materials")
        except Exception as exc:
            logger.error("WhatsApp group notification failed: %s", exc)

        # Step 7: Send gap analysis privately to Tornike via WhatsApp
        gap_header = (
            f"📊 ლექცია #{lecture} — ანალიზი\n"
            f"ჯგუფი: {group}\n"
            f"{'─' * 30}\n\n"
        )
        send_private_report(gap_header + analysis["gap_analysis"])
        logger.info("Gap analysis sent to Tornike via WhatsApp")

        # Step 7b: Send deep analysis if available
        deep = analysis.get("deep_analysis", "")
        if deep:
            deep_header = (
                f"🌍 ლექცია #{lecture} — ღრმა ანალიზი (გლობალური კონტექსტი)\n"
                f"ჯგუფი: {group}\n"
                f"{'━' * 30}\n\n"
            )
            send_private_report(deep_header + deep)
            logger.info("Deep analysis sent to Tornike via WhatsApp")

        # Step 8: Callback to n8n with success
        await _send_callback(CallbackPayload(
            status="success",
            group_number=group,
            lecture_number=lecture,
            summary_doc_url=summary_doc_url,
            drive_recording_url=drive_recording_url,
            gap_analysis_text=analysis["gap_analysis"],
        ))

        logger.info(
            "Processing complete: Group %d, Lecture #%d", group, lecture
        )

    except Exception as e:
        error_msg = f"Processing failed: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)

        # Callback to n8n with error
        await _send_callback(CallbackPayload(
            status="error",
            group_number=group,
            lecture_number=lecture,
            error_message=str(e),
        ))

    finally:
        # Clean up temporary file
        if local_path and local_path.exists():
            local_path.unlink()
            logger.info("Cleaned up temp file: %s", local_path)


async def _download_recording(url: str, access_token: str, dest: Path) -> None:
    """Download a Zoom recording with streaming for large files."""
    dest = Path(dest)
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30)) as client:
        async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
            response.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    f.write(chunk)


async def _send_callback(payload: CallbackPayload) -> None:
    """Send processing results back to n8n webhook."""
    if not N8N_CALLBACK_URL:
        logger.warning("N8N_CALLBACK_URL not configured — skipping callback")
        return

    headers = {}
    if WEBHOOK_SECRET:
        headers["Authorization"] = f"Bearer {WEBHOOK_SECRET}"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                N8N_CALLBACK_URL,
                json=payload.model_dump(),
                headers=headers,
            )
            response.raise_for_status()
            logger.info("Callback sent to n8n: status=%s", payload.status)
    except Exception as e:
        logger.error("Failed to send callback to n8n: %s", e)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "training-agent",
        "timestamp": datetime.now().isoformat(),
    }


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
