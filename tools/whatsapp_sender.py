"""WhatsApp messaging via Green API.

Green API connects to WhatsApp Web via QR code scan and provides a REST API
for sending messages to individual chats and groups. Free developer plan
supports 1 instance with unlimited messages.

Setup:
    1. Register at green-api.com
    2. Create an instance → get Instance ID + API Token
    3. Scan QR code with your WhatsApp
    4. Add credentials to .env
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from tools.config import (
    GREEN_API_INSTANCE_ID,
    GREEN_API_TOKEN,
    WEBHOOK_SECRET,
    WHATSAPP_GROUP1_ID,
    WHATSAPP_GROUP2_ID,
    WHATSAPP_TORNIKE_PHONE,
    GROUPS,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds
MESSAGE_MAX_LENGTH = 4096  # WhatsApp message character limit

# Group ID mapping
_GROUP_CHAT_IDS = {
    1: WHATSAPP_GROUP1_ID,
    2: WHATSAPP_GROUP2_ID,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Build the Green API base URL for the configured instance."""
    return f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE_ID}"


def _send_request(method: str, payload: dict[str, Any], purpose: str) -> dict[str, Any]:
    """Send a request to Green API with retry logic.

    Args:
        method: API method name (e.g. 'sendMessage', 'sendFileByUrl').
        payload: Request body.
        purpose: Human-readable description for logging.

    Returns:
        Green API response dict.

    Raises:
        RuntimeError: If all retries are exhausted.
    """
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        raise ValueError(
            "Green API not configured. Set GREEN_API_INSTANCE_ID and "
            "GREEN_API_TOKEN in .env"
        )

    url = f"{_base_url()}/{method}/{GREEN_API_TOKEN}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(url, json=payload)
        except Exception as e:
            # Network-level error (timeout, connection refused, etc.) — retryable
            logger.warning(
                "%s attempt %d failed: %s",
                purpose, attempt, e,
            )
        else:
            if response.status_code == 200:
                data = response.json()
                logger.info("%s sent successfully: %s", purpose, data.get("idMessage", "ok"))
                return data

            error_text = response.text

            # Don't retry on client errors (except 429 rate limit)
            if 400 <= response.status_code < 500 and response.status_code != 429:
                raise RuntimeError(
                    f"{purpose} failed with HTTP {response.status_code}: {error_text}"
                )

            logger.warning(
                "%s attempt %d failed: HTTP %d — %s",
                purpose, attempt, response.status_code, error_text,
            )

        if attempt < MAX_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            time.sleep(delay)
        else:
            raise RuntimeError(f"{purpose} failed after {MAX_RETRIES} attempts")

    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_message_to_chat(chat_id: str, message: str) -> dict[str, Any]:
    """Send a text message to a WhatsApp chat (individual or group).

    Args:
        chat_id: WhatsApp chat ID.
            - Individual: '995XXXXXXXXX@c.us' (country code + number)
            - Group: 'XXXXXXXXXX-XXXXXXXXXX@g.us'
        message: The text message to send.

    Returns:
        Green API response dict.
    """
    chunks = _split_message(message)

    result: dict[str, Any] = {}
    for i, chunk in enumerate(chunks):
        payload = {
            "chatId": chat_id,
            "message": chunk,
        }
        result = _send_request(
            "sendMessage",
            payload,
            f"Message part {i + 1}/{len(chunks)} to {chat_id[:20]}",
        )
        if i < len(chunks) - 1:
            time.sleep(1)  # Brief pause between chunks

    return result


def send_group_reminder(group_number: int, zoom_link: str, lecture_number: int) -> dict[str, Any]:
    """Send a meeting reminder with Zoom link to a group's WhatsApp chat.

    Args:
        group_number: 1 or 2.
        zoom_link: The Zoom meeting join URL.
        lecture_number: Current lecture number.

    Returns:
        Green API response dict.
    """
    group = GROUPS[group_number]
    chat_id = _GROUP_CHAT_IDS.get(group_number)

    if not chat_id:
        raise ValueError(
            f"No WhatsApp group ID configured for Group {group_number}. "
            "Set WHATSAPP_GROUP1_ID / WHATSAPP_GROUP2_ID in .env"
        )

    message = (
        f"🎓 შეხსენება — ლექცია #{lecture_number}\n\n"
        f"ჯგუფი: {group['name']}\n"
        f"დრო: 20:00 - 22:00\n\n"
        f"Zoom ლინკი:\n{zoom_link}\n\n"
        f"გელით ლექციაზე! 🚀"
    )

    logger.info(
        "Sending reminder for Group %d, Lecture #%d to WhatsApp group",
        group_number, lecture_number,
    )
    return send_message_to_chat(chat_id, message)


def send_group_upload_notification(
    group_number: int,
    lecture_number: int,
    drive_recording_url: str,
    summary_doc_url: str,
) -> dict[str, Any]:
    """Notify the training group's WhatsApp chat that recording + summary are uploaded.

    Args:
        group_number: 1 or 2.
        lecture_number: Current lecture number.
        drive_recording_url: Google Drive URL of the uploaded recording.
        summary_doc_url: Google Docs URL of the lecture summary.

    Returns:
        Green API response dict.
    """
    group = GROUPS[group_number]
    chat_id = _GROUP_CHAT_IDS.get(group_number)

    if not chat_id:
        logger.warning(
            "No WhatsApp group ID for Group %d — sending to Tornike only",
            group_number,
        )
        chat_id = f"{WHATSAPP_TORNIKE_PHONE}@c.us"

    message = (
        f"✅ ლექცია #{lecture_number} — მასალა ატვირთულია!\n\n"
        f"ჯგუფი: {group['name']}\n"
        f"{'─' * 30}\n\n"
        f"📹 ჩანაწერი:\n{drive_recording_url}\n\n"
        f"📝 შეჯამება:\n{summary_doc_url}\n\n"
        f"წარმატებებს გისურვებთ! 🚀"
    )

    return send_message_to_chat(chat_id, message)


def send_private_report(report_text: str) -> dict[str, Any]:
    """Send the gap/deep analysis report privately to Tornike via WhatsApp.

    Args:
        report_text: The full analysis text in Georgian.

    Returns:
        Green API response dict.
    """
    if not WHATSAPP_TORNIKE_PHONE:
        raise ValueError("WHATSAPP_TORNIKE_PHONE not configured in .env")

    chat_id = f"{WHATSAPP_TORNIKE_PHONE}@c.us"
    logger.info("Sending private report to Tornike...")
    return send_message_to_chat(chat_id, report_text)


def alert_operator(message: str) -> None:
    """Last-resort alert to Tornike when automated systems fail.

    Tries to send a WhatsApp message. If that also fails, logs at CRITICAL
    level (which goes to both console and the rotating log file). This
    function NEVER raises — it is the safety net, not another failure point.

    Args:
        message: Plain-text alert (keep it short and actionable).
    """
    prefix = "⚠️ Training Agent ALERT\n\n"
    try:
        if WHATSAPP_TORNIKE_PHONE and GREEN_API_INSTANCE_ID and GREEN_API_TOKEN:
            chat_id = f"{WHATSAPP_TORNIKE_PHONE}@c.us"
            send_message_to_chat(chat_id, prefix + message)
            logger.info("Operator alert sent via WhatsApp")
            return
    except Exception as exc:
        logger.error("Failed to send WhatsApp alert: %s", exc)

    # Fallback: CRITICAL log — appears in console + log file
    logger.critical("OPERATOR ALERT (WhatsApp unavailable): %s", message)


# ---------------------------------------------------------------------------
# Webhook configuration (for receiving incoming messages)
# ---------------------------------------------------------------------------


def configure_webhook(webhook_url: str) -> dict[str, Any]:
    """Configure Green API to send incoming message notifications to a webhook URL.

    Args:
        webhook_url: Public URL that Green API will POST incoming messages to.
                     e.g. 'https://abc123.ngrok.io/whatsapp-incoming'

    Returns:
        Green API response dict.
    """
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        raise ValueError("Green API not configured")

    url = f"{_base_url()}/setSettings/{GREEN_API_TOKEN}"

    # webhookUrlToken is sent as Authorization header by Green API
    # Must match what /whatsapp-incoming expects (Bearer <WEBHOOK_SECRET>)
    token = f"Bearer {WEBHOOK_SECRET}" if WEBHOOK_SECRET else ""

    settings = {
        "webhookUrl": webhook_url,
        "webhookUrlToken": token,
        "incomingWebhook": "yes",
        "outgoingMessageWebhook": "no",
        "outgoingAPIMessageWebhook": "no",
        "stateWebhook": "no",
        "deviceWebhook": "no",
    }

    with httpx.Client(timeout=30) as client:
        response = client.post(url, json=settings)

    response.raise_for_status()
    result = response.json()
    logger.info("Green API webhook configured: %s → %s", webhook_url, result)
    return result


def get_webhook_settings() -> dict[str, Any]:
    """Get current Green API webhook settings.

    Returns:
        Dict with current webhook configuration.
    """
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        raise ValueError("Green API not configured")

    url = f"{_base_url()}/getSettings/{GREEN_API_TOKEN}"

    with httpx.Client(timeout=30) as client:
        response = client.get(url)

    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Utility: fetch all groups (for getting group IDs)
# ---------------------------------------------------------------------------


def list_groups() -> list[dict[str, Any]]:
    """List all WhatsApp groups the connected account is part of.

    Useful for finding group chat IDs during setup.

    Returns:
        List of group dicts with 'id', 'name', 'participants' etc.
    """
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        raise ValueError("Green API not configured")

    url = f"{_base_url()}/getContacts/{GREEN_API_TOKEN}"

    with httpx.Client(timeout=30) as client:
        response = client.get(url)

    response.raise_for_status()
    contacts = response.json()

    # Filter only groups
    groups = [c for c in contacts if c.get("id", "").endswith("@g.us")]
    logger.info("Found %d WhatsApp groups", len(groups))
    return groups


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_message(text: str) -> list[str]:
    """Split a long message into WhatsApp-compatible chunks."""
    if len(text) <= MESSAGE_MAX_LENGTH:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= MESSAGE_MAX_LENGTH:
            chunks.append(remaining)
            break

        split_at = remaining[:MESSAGE_MAX_LENGTH].rfind("\n\n")
        if split_at < MESSAGE_MAX_LENGTH // 2:
            split_at = remaining[:MESSAGE_MAX_LENGTH].rfind("\n")
        if split_at < MESSAGE_MAX_LENGTH // 2:
            split_at = remaining[:MESSAGE_MAX_LENGTH].rfind(" ")
        if split_at <= 0:
            split_at = MESSAGE_MAX_LENGTH

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks


# ---------------------------------------------------------------------------
# CLI entrypoint for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("WhatsApp Sender (Green API) — Test Mode")
    print(f"Instance ID: {'Yes' if GREEN_API_INSTANCE_ID else 'No'}")
    print(f"API Token: {'Yes' if GREEN_API_TOKEN else 'No'}")
    print(f"Tornike phone: {'Yes' if WHATSAPP_TORNIKE_PHONE else 'No'}")
    print(f"Group 1 ID: {'Yes' if WHATSAPP_GROUP1_ID else 'No'}")
    print(f"Group 2 ID: {'Yes' if WHATSAPP_GROUP2_ID else 'No'}")

    if GREEN_API_INSTANCE_ID and GREEN_API_TOKEN:
        print("\nFetching WhatsApp groups...")
        try:
            grps = list_groups()
            for g in grps:
                print(f"  {g.get('name', '?')} → {g.get('id', '?')}")
        except Exception as e:
            print(f"  Error: {e}")
    else:
        print("\nConfigure GREEN_API_INSTANCE_ID and GREEN_API_TOKEN in .env first")
