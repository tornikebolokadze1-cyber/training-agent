"""WhatsApp messaging via ManyChat API."""

import logging
import time

import httpx

from tools.config import (
    MANYCHAT_API_KEY,
    MANYCHAT_TORNIKE_SUBSCRIBER_ID,
    GROUPS,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.manychat.com/fb"
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds
MESSAGE_MAX_LENGTH = 4096  # WhatsApp message character limit


def _get_headers() -> dict[str, str]:
    """Return authorization headers for ManyChat API."""
    return {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _send_request(endpoint: str, payload: dict, purpose: str) -> dict:
    """Send a POST request to ManyChat API with retry logic."""
    url = f"{BASE_URL}/{endpoint}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(url, json=payload, headers=_get_headers())

            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success":
                    logger.info("%s sent successfully", purpose)
                    return data
                else:
                    raise RuntimeError(f"ManyChat API error: {data}")

            response.raise_for_status()

        except Exception as e:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                "%s attempt %d failed: %s — retrying in %ds",
                purpose, attempt, e, delay,
            )
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"{purpose} failed after {MAX_RETRIES} attempts: {e}"
                ) from e
            time.sleep(delay)

    raise RuntimeError("Unreachable")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_message_to_subscriber(subscriber_id: str, message: str) -> dict:
    """Send a text message to a specific ManyChat subscriber via WhatsApp.

    Args:
        subscriber_id: The ManyChat subscriber ID.
        message: The text message to send.

    Returns:
        ManyChat API response dict.
    """
    # Split long messages into chunks if needed
    chunks = _split_message(message)

    result = None
    for i, chunk in enumerate(chunks):
        payload = {
            "subscriber_id": subscriber_id,
            "data": {
                "version": "v2",
                "content": {
                    "messages": [{"type": "text", "text": chunk}],
                },
            },
        }
        result = _send_request(
            "sending/sendContent",
            payload,
            f"Message part {i + 1}/{len(chunks)}",
        )
        if i < len(chunks) - 1:
            time.sleep(1)  # Brief pause between chunks

    return result


def send_flow_to_subscriber(subscriber_id: str, flow_id: str) -> dict:
    """Trigger a ManyChat flow for a specific subscriber.

    Args:
        subscriber_id: The ManyChat subscriber ID.
        flow_id: The ManyChat flow namespace/ID.

    Returns:
        ManyChat API response dict.
    """
    payload = {
        "subscriber_id": subscriber_id,
        "flow_ns": flow_id,
    }
    return _send_request(
        "sending/sendFlow",
        payload,
        f"Flow '{flow_id}' trigger",
    )


def send_group_reminder(group_number: int, zoom_link: str, lecture_number: int) -> dict:
    """Send a meeting reminder with Zoom link to a group's WhatsApp flow.

    Args:
        group_number: 1 or 2.
        zoom_link: The Zoom meeting invitation link.
        lecture_number: Current lecture number.

    Returns:
        ManyChat API response dict.
    """
    group = GROUPS[group_number]
    flow_id = group["manychat_flow_id"]

    if not flow_id:
        raise ValueError(f"No ManyChat flow ID configured for Group {group_number}")

    logger.info(
        "Sending reminder for Group %d, Lecture #%d via flow %s",
        group_number, lecture_number, flow_id,
    )

    # For flow-based reminders, we trigger the flow
    # The Zoom link should be set as a custom field or passed via external trigger
    # For now, we send a direct message as fallback
    message = (
        f"🎓 შეხსენება — ლექცია #{lecture_number}\n\n"
        f"ჯგუფი: {group['name']}\n"
        f"დრო: 20:00 - 22:00\n\n"
        f"Zoom ლინკი:\n{zoom_link}\n\n"
        f"გელით ლექციაზე! 🚀"
    )

    # If flow ID is configured, use flow; otherwise send direct message
    if flow_id:
        return send_flow_to_subscriber(MANYCHAT_TORNIKE_SUBSCRIBER_ID, flow_id)
    return send_message_to_subscriber(MANYCHAT_TORNIKE_SUBSCRIBER_ID, message)


def send_group_upload_notification(
    group_number: int,
    lecture_number: int,
    drive_recording_url: str,
    summary_doc_url: str,
) -> dict:
    """Notify the training group's WhatsApp chat that recording + summary are uploaded.

    Args:
        group_number: 1 or 2.
        lecture_number: Current lecture number.
        drive_recording_url: Google Drive URL of the uploaded recording.
        summary_doc_url: Google Docs URL of the lecture summary.

    Returns:
        ManyChat API response dict.
    """
    group = GROUPS[group_number]
    message = (
        f"✅ ლექცია #{lecture_number} — მასალა ატვირთულია!\n\n"
        f"ჯგუფი: {group['name']}\n"
        f"{'─' * 30}\n\n"
        f"📹 ჩანაწერი:\n{drive_recording_url}\n\n"
        f"📝 შეჯამება:\n{summary_doc_url}\n\n"
        f"წარმატებებს გისურვებთ! 🚀"
    )

    flow_id = group.get("manychat_flow_id")
    if flow_id:
        # Use flow for group broadcast
        return send_flow_to_subscriber(MANYCHAT_TORNIKE_SUBSCRIBER_ID, flow_id)

    # Fallback: send direct message to Tornike (who can forward to group)
    logger.warning(
        "No ManyChat flow configured for Group %d — sending to Tornike only",
        group_number,
    )
    return send_message_to_subscriber(MANYCHAT_TORNIKE_SUBSCRIBER_ID, message)


def send_private_report(report_text: str) -> dict:
    """Send the gap analysis report privately to Tornike via WhatsApp.

    Args:
        report_text: The full gap analysis text in Georgian.

    Returns:
        ManyChat API response dict.
    """
    if not MANYCHAT_TORNIKE_SUBSCRIBER_ID:
        raise ValueError("MANYCHAT_TORNIKE_SUBSCRIBER_ID not configured in .env")

    logger.info("Sending private gap analysis report to Tornike...")
    return send_message_to_subscriber(MANYCHAT_TORNIKE_SUBSCRIBER_ID, report_text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_message(text: str) -> list[str]:
    """Split a long message into WhatsApp-compatible chunks.

    Tries to split at paragraph boundaries when possible.
    """
    if len(text) <= MESSAGE_MAX_LENGTH:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= MESSAGE_MAX_LENGTH:
            chunks.append(remaining)
            break

        # Try to split at a paragraph boundary
        split_at = remaining[:MESSAGE_MAX_LENGTH].rfind("\n\n")
        if split_at < MESSAGE_MAX_LENGTH // 2:
            # No good paragraph break — split at last newline
            split_at = remaining[:MESSAGE_MAX_LENGTH].rfind("\n")
        if split_at < MESSAGE_MAX_LENGTH // 2:
            # No good newline — split at last space
            split_at = remaining[:MESSAGE_MAX_LENGTH].rfind(" ")
        if split_at < 0:
            split_at = MESSAGE_MAX_LENGTH

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks


# ---------------------------------------------------------------------------
# CLI entrypoint for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    print("ManyChat Sender — Test Mode")
    print(f"API Key configured: {'Yes' if MANYCHAT_API_KEY else 'No'}")
    print(f"Tornike subscriber ID: {'Yes' if MANYCHAT_TORNIKE_SUBSCRIBER_ID else 'No'}")

    test_msg = "✅ ტესტი — Training Agent სისტემა მუშაობს!"
    if MANYCHAT_API_KEY and MANYCHAT_TORNIKE_SUBSCRIBER_ID:
        result = send_private_report(test_msg)
        print(f"Test message sent: {result}")
    else:
        print("Configure MANYCHAT_API_KEY and MANYCHAT_TORNIKE_SUBSCRIBER_ID in .env first")
