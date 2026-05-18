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

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

from tools.core.config import (
    GREEN_API_INSTANCE_ID,
    GREEN_API_TOKEN,
    GROUPS,
    TMP_DIR,
    WEBHOOK_SECRET,
    WHATSAPP_TORNIKE_PHONE,
)
from tools.core.retry import retry_with_backoff

logger = logging.getLogger(__name__)

# Prometheus metric (US-026) — graceful-import so a missing prometheus_client
# does not crash the module. WHATSAPP_SENT.labels(result=…).inc() is a no-op
# when the dep is absent.
try:
    from prometheus_client import REGISTRY as _PROM_REGISTRY
    from prometheus_client import Counter as _PromCounter

    try:
        _wa_sent_counter: Any = _PromCounter(
            "whatsapp_messages_sent_total",
            "WhatsApp send attempts by outcome (sent|suppressed|failed)",
            ["result"],
        )
    except ValueError:
        # Already registered (test re-import). Find the existing collector.
        _wa_sent_counter = None
        for _c in list(_PROM_REGISTRY._collector_to_names.keys()):  # type: ignore[attr-defined]
            if getattr(_c, "_name", None) == "whatsapp_messages_sent":
                _wa_sent_counter = _c
                break
        if _wa_sent_counter is None:  # pragma: no cover
            raise
    WHATSAPP_SENT: Any = _wa_sent_counter
except Exception:  # pragma: no cover — exercised only without the dep
    class _NoOpCounter:
        def labels(self, *a, **kw):
            return self

        def inc(self, *a, **kw) -> None:
            return None

    WHATSAPP_SENT = _NoOpCounter()

MAX_RETRIES = 3
RETRY_BASE_DELAY = 2  # seconds
MESSAGE_MAX_LENGTH = 4096  # WhatsApp message character limit

# Rate limiter: max 20 messages per 60 seconds
RATE_LIMIT_MAX_MESSAGES = 20
RATE_LIMIT_WINDOW_SECONDS = 60

# DLQ retry config
DLQ_MAX_RETRIES = 3

MISSED_ALERTS_PATH = TMP_DIR / "missed_alerts.json"

# Group ID mapping — built dynamically from the GROUPS config so every enabled
# cohort is routable. Previously this dict was hardcoded to {1, 2} only, which
# silently dropped Group 3/4 reminders introduced by the May 2026 cohort
# wire-up (Group 4's first lecture had its 18:00 Zoom reminder swallowed
# because the lookup returned None).
def _build_group_chat_ids() -> dict[int, str]:
    return {
        g_num: cfg["whatsapp_chat_id"]
        for g_num, cfg in GROUPS.items()
        if cfg.get("whatsapp_chat_id")
    }


_GROUP_CHAT_IDS: dict[int, str] = _build_group_chat_ids()


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Simple sliding-window rate limiter for WhatsApp sends.

    Thread-safe. Tracks timestamps of recent sends and blocks when the
    limit is hit, returning the wait time needed.
    """

    def __init__(self, max_messages: int = RATE_LIMIT_MAX_MESSAGES, window: int = RATE_LIMIT_WINDOW_SECONDS) -> None:
        self._max = max_messages
        self._window = window
        self._timestamps: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Try to acquire a send slot.

        Returns:
            0.0 if slot acquired, otherwise the number of seconds to wait.
        """
        now = time.monotonic()
        with self._lock:
            cutoff = now - self._window
            self._timestamps = [t for t in self._timestamps if t > cutoff]
            if len(self._timestamps) >= self._max:
                wait = self._timestamps[0] - cutoff
                return max(wait, 0.1)
            self._timestamps.append(now)
            return 0.0

    def wait_and_acquire(self) -> None:
        """Block until a send slot is available."""
        while True:
            wait = self.acquire()
            if wait == 0.0:
                return
            logger.debug("Rate limit hit, waiting %.1fs", wait)
            time.sleep(wait)


_rate_limiter = _RateLimiter()


# ---------------------------------------------------------------------------
# Operator alert deduplication (US-016)
# ---------------------------------------------------------------------------
# alert_operator had no dedup — a 50-error pipeline_retry storm = 50 WhatsApp
# messages. We hash the message body and suppress duplicates within a 300s
# window via a bounded OrderedDict (max 100 entries). The bound prevents
# unbounded growth if a runaway loop produces thousands of unique hashes.
# Suppressed alerts logged at WARNING with hash prefix + elapsed seconds.

_ALERT_DEDUP_WINDOW_SECONDS = 300  # 5 minutes
_ALERT_DEDUP_MAX_ENTRIES = 100
_alert_dedup_state: "OrderedDict[str, float]" = OrderedDict()
# Counts of how many times each hash was suppressed since its last send. On the
# next non-suppressed occurrence (after the window expires) we surface the
# count in the alert so the operator knows how many were swallowed.
_alert_suppression_counts: dict[str, int] = {}
_alert_dedup_lock = threading.Lock()


# ---------------------------------------------------------------------------
# DLQ (Dead Letter Queue) for failed notifications
# ---------------------------------------------------------------------------


@dataclass
class _DLQEntry:
    chat_id: str
    message: str
    priority: str  # "alert" > "report" > "notification"
    attempts: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "message": self.message,
            "priority": self.priority,
            "attempts": self.attempts,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> _DLQEntry:
        return cls(
            chat_id=d["chat_id"],
            message=d["message"],
            priority=d["priority"],
            attempts=d.get("attempts", 0),
            created_at=d.get("created_at", time.time()),
        )


class NotificationDLQ:
    """Dead Letter Queue for failed WhatsApp notifications.

    Thread-safe. Failed messages are enqueued and retried periodically.
    After DLQ_MAX_RETRIES failures, messages are saved to missed_alerts.json.
    """

    def __init__(self) -> None:
        self._queue: list[_DLQEntry] = []
        self._lock = threading.Lock()

    def enqueue(self, chat_id: str, message: str, priority: str = "notification") -> None:
        """Add a failed message to the DLQ."""
        entry = _DLQEntry(chat_id=chat_id, message=message, priority=priority)
        with self._lock:
            self._queue.append(entry)
        logger.warning("Message enqueued to DLQ (priority=%s, chat=%s)", priority, chat_id[:20])

    def process(self) -> dict[str, int]:
        """Retry all queued messages. Called periodically (every 10 min).

        Returns:
            Dict with counts: {"sent": N, "retrying": N, "dead": N}
        """
        with self._lock:
            to_process = list(self._queue)
            self._queue.clear()

        # Sort by priority: alert > report > notification
        priority_order = {"alert": 0, "report": 1, "notification": 2}
        to_process.sort(key=lambda e: priority_order.get(e.priority, 9))

        sent = 0
        retrying = 0
        dead = 0

        for entry in to_process:
            entry.attempts += 1
            try:
                _rate_limiter.wait_and_acquire()
                result = _send_request_raw("sendMessage", {"chatId": entry.chat_id, "message": entry.message}, f"DLQ retry #{entry.attempts}")
                _validate_send_response(result, f"DLQ retry to {entry.chat_id[:20]}")
                sent += 1
                logger.info("DLQ message sent successfully after %d attempts", entry.attempts)
            except Exception as exc:
                if entry.attempts >= DLQ_MAX_RETRIES:
                    dead += 1
                    logger.error("DLQ message dead after %d attempts: %s", entry.attempts, exc)
                    _save_missed_alert(entry)
                else:
                    retrying += 1
                    with self._lock:
                        self._queue.append(entry)

        if sent or retrying or dead:
            logger.info("DLQ processed: sent=%d, retrying=%d, dead=%d", sent, retrying, dead)
        return {"sent": sent, "retrying": retrying, "dead": dead}

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._queue)


notification_dlq = NotificationDLQ()


def _save_missed_alert(entry: _DLQEntry) -> None:
    """Append a dead message to missed_alerts.json for later recovery."""
    try:
        MISSED_ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict[str, Any]] = []
        if MISSED_ALERTS_PATH.exists():
            try:
                existing = json.loads(MISSED_ALERTS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = []
        existing.append(entry.to_dict())
        MISSED_ALERTS_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Saved missed alert to %s", MISSED_ALERTS_PATH.name)
    except Exception as exc:
        logger.error("Failed to save missed alert to file: %s", exc)


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


class WhatsAppSendError(RuntimeError):
    """Raised when Green API returns 200 but no idMessage (silent failure)."""


def _validate_send_response(data: dict[str, Any], purpose: str) -> None:
    """Validate that a Green API send response actually delivered the message.

    Green API may return 200 with an empty response or missing idMessage
    when the message was NOT actually sent (e.g., phone disconnected).

    Raises:
        WhatsAppSendError: If idMessage is missing from the response.
    """
    if not data.get("idMessage"):
        raise WhatsAppSendError(
            f"{purpose}: API returned 200 but no idMessage — message NOT sent. "
            f"Response: {data}"
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Build the Green API base URL for the configured instance."""
    return f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE_ID}"


class _NonRetryableError(Exception):
    """Raised for HTTP 4xx errors (except 429) that should not be retried."""


def _send_request_raw(method: str, payload: dict[str, Any], purpose: str) -> dict[str, Any]:
    """Send a request to Green API with retry logic (no response validation).

    Args:
        method: API method name (e.g. 'sendMessage', 'sendFileByUrl').
        payload: Request body.
        purpose: Human-readable description for logging.

    Returns:
        Green API response dict.

    Raises:
        RuntimeError: If all retries are exhausted or a non-retryable error occurs.
    """
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        raise ValueError(
            "Green API not configured. Set GREEN_API_INSTANCE_ID and "
            "GREEN_API_TOKEN in .env"
        )

    url = f"{_base_url()}/{method}/{GREEN_API_TOKEN}"

    def _do_request() -> dict[str, Any]:
        with httpx.Client(timeout=30) as client:
            response = client.post(url, json=payload)
        if response.status_code == 200:
            data = response.json()
            logger.info("%s sent successfully: %s", purpose, data.get("idMessage", "ok"))
            return data
        # Don't retry on client errors (except 429 rate limit)
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise _NonRetryableError(
                f"HTTP {response.status_code}: {response.text}"
            )
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")

    try:
        return retry_with_backoff(
            _do_request,
            max_retries=MAX_RETRIES,
            backoff_base=float(RETRY_BASE_DELAY),
            retryable_exceptions=(RuntimeError, httpx.TransportError),
            operation_name=purpose,
        )
    except _NonRetryableError as exc:
        raise RuntimeError(
            f"{purpose} failed with non-retryable error: {exc}"
        ) from exc


def _send_request(method: str, payload: dict[str, Any], purpose: str) -> dict[str, Any]:
    """Send a request to Green API with retry, rate limiting, and response validation.

    Wraps _send_request_raw with:
    1. Rate limiting (max 20 messages/minute)
    2. Response validation (idMessage must be present)
    3. DLQ enqueue on validation failure

    Args:
        method: API method name (e.g. 'sendMessage').
        payload: Request body.
        purpose: Human-readable description for logging.

    Returns:
        Green API response dict with confirmed idMessage.

    Raises:
        RuntimeError: If all retries are exhausted.
        WhatsAppSendError: If response lacks idMessage after retry.
    """
    # Rate limiting: wait if needed, never fail pipeline for this
    _rate_limiter.wait_and_acquire()

    data = _send_request_raw(method, payload, purpose)

    # Validate: idMessage must be present for sendMessage calls
    if method == "sendMessage" and not data.get("idMessage"):
        # Retry once — phone might have reconnected
        logger.warning("%s: no idMessage in response, retrying once...", purpose)
        time.sleep(2)
        _rate_limiter.wait_and_acquire()
        data = _send_request_raw(method, payload, f"{purpose} (validation retry)")
        if not data.get("idMessage"):
            # Enqueue to DLQ for later retry
            chat_id = payload.get("chatId", "unknown")
            message = payload.get("message", "")
            notification_dlq.enqueue(chat_id, message, priority="notification")
            WHATSAPP_SENT.labels(result="failed").inc()
            raise WhatsAppSendError(
                f"{purpose}: API returned 200 but no idMessage after retry — "
                f"message enqueued to DLQ. Response: {data}"
            )

    WHATSAPP_SENT.labels(result="sent").inc()
    return data


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
            f"WhatsApp chat ID not configured for group {group_number}. "
            f"Set WHATSAPP_GROUP{group_number}_ID in .env"
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

    Tries to send a WhatsApp message. If that also fails:
    1. Logs at CRITICAL level (console + rotating log file)
    2. Saves to .tmp/missed_alerts.json for later recovery

    This function NEVER raises — it is the safety net, not another failure
    point. The entire body is wrapped in try/except to guarantee this.

    Deduplication (US-016):
        Identical messages are suppressed within a 300s window. The same
        message sent 50 times in 60s reaches the operator ONCE. When a
        suppression-window-aged hash reappears, the alert is annotated with
        the number of suppressed copies so the operator can see how many
        were swallowed.

    Args:
        message: Plain-text alert (keep it short and actionable).
    """
    try:
        # --- Dedup gate -------------------------------------------------
        try:
            msg_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
        except BaseException as hash_exc:
            # If hashing somehow fails (very unlikely for str input), skip
            # dedup and proceed — the alert is more important than dedup.
            logger.warning("alert dedup hash failed, sending without dedup: %s", hash_exc)
            msg_hash = None

        suppressed_count = 0
        if msg_hash is not None:
            now = time.time()
            with _alert_dedup_lock:
                last_sent = _alert_dedup_state.get(msg_hash)
                if last_sent is not None and (now - last_sent) < _ALERT_DEDUP_WINDOW_SECONDS:
                    # Within window → suppress, bump counter, return.
                    _alert_suppression_counts[msg_hash] = (
                        _alert_suppression_counts.get(msg_hash, 0) + 1
                    )
                    elapsed = now - last_sent
                    count = _alert_suppression_counts[msg_hash]
                    logger.warning(
                        "alert suppressed (duplicate within %ds window): "
                        "hash=%s elapsed=%.1fs suppressed_total=%d",
                        _ALERT_DEDUP_WINDOW_SECONDS,
                        msg_hash[:8],
                        elapsed,
                        count,
                    )
                    WHATSAPP_SENT.labels(result="suppressed").inc()
                    return

                # Either first time, or window expired → record & send.
                # If window expired, capture how many were suppressed so we
                # can surface that in the message below.
                suppressed_count = _alert_suppression_counts.pop(msg_hash, 0)
                # Re-insert (or insert) to mark as most-recent in OrderedDict.
                if msg_hash in _alert_dedup_state:
                    del _alert_dedup_state[msg_hash]
                _alert_dedup_state[msg_hash] = now
                # Enforce upper bound by evicting oldest entries.
                while len(_alert_dedup_state) > _ALERT_DEDUP_MAX_ENTRIES:
                    evicted_hash, _ = _alert_dedup_state.popitem(last=False)
                    _alert_suppression_counts.pop(evicted_hash, None)

        prefix = "⚠️ Training Agent ALERT\n\n"
        if suppressed_count > 0:
            # Window expired with N suppressed duplicates in between → tell
            # the operator how many identical alerts were swallowed.
            prefix = (
                f"⚠️ Training Agent ALERT (+{suppressed_count} duplicates "
                f"suppressed in last {_ALERT_DEDUP_WINDOW_SECONDS}s)\n\n"
            )
        full_message = prefix + message

        # Attempt WhatsApp delivery
        whatsapp_sent = False
        try:
            if WHATSAPP_TORNIKE_PHONE and GREEN_API_INSTANCE_ID and GREEN_API_TOKEN:
                chat_id = f"{WHATSAPP_TORNIKE_PHONE}@c.us"
                send_message_to_chat(chat_id, full_message)
                logger.info("Operator alert sent via WhatsApp")
                whatsapp_sent = True
        except BaseException as exc:
            logger.error("Failed to send WhatsApp alert: %s", exc)

        if not whatsapp_sent:
            # Fallback 1: CRITICAL log
            logger.critical("OPERATOR ALERT (WhatsApp unavailable): %s", message)

            # Fallback 2: Save to missed_alerts.json for nightly health check
            try:
                entry = _DLQEntry(
                    chat_id=f"{WHATSAPP_TORNIKE_PHONE or 'unknown'}@c.us",
                    message=full_message,
                    priority="alert",
                )
                _save_missed_alert(entry)
            except BaseException as file_exc:
                logger.error("Failed to save alert to file: %s", file_exc)

    except BaseException as outer_exc:
        # Ultimate safety net: if ANYTHING above raises, just log it
        try:
            logger.critical(
                "alert_operator TOTAL FAILURE: original=%s, error=%s",
                message, outer_exc,
            )
        except BaseException:
            pass  # Truly nothing we can do


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


def get_chat_history(chat_id: str, count: int = 100) -> list[dict[str, Any]]:
    """Fetch the last N messages of a chat from Green API.

    Used by the assistant catch-up service to recover messages that were
    missed by the live ``/whatsapp-incoming`` webhook (e.g. during a Railway
    boot hang or a Green API webhook outage).

    Returns the raw message list as Green API delivers it. Each item has at
    minimum ``idMessage``, ``timestamp``, ``type`` ("incoming"/"outgoing"),
    ``typeMessage``, ``senderId``, ``senderName``, and (when applicable)
    ``textMessage`` / ``extendedTextMessage`` / ``quotedMessage`` blocks.

    Args:
        chat_id: WhatsApp chat ID, e.g. "120363XXX@g.us" or "995XXXXXXX@c.us".
        count: Maximum number of messages to fetch (Green API caps near 100).

    Returns:
        List of raw Green API message dicts, newest first. Empty list on any
        non-200 response or when the chat has no history.

    Raises:
        ValueError: If Green API credentials are not configured.
    """
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        raise ValueError("Green API not configured")

    url = f"{_base_url()}/getChatHistory/{GREEN_API_TOKEN}"

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(url, json={"chatId": chat_id, "count": count})
    except httpx.TransportError as exc:
        logger.warning("get_chat_history transport error for %s: %s", chat_id, exc)
        return []

    if response.status_code != 200:
        logger.warning(
            "get_chat_history returned %d for %s: %s",
            response.status_code, chat_id, response.text[:200],
        )
        return []

    try:
        data = response.json()
    except ValueError:
        logger.warning("get_chat_history returned non-JSON for %s", chat_id)
        return []

    if not isinstance(data, list):
        return []
    return data


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
    for _grp_num, _grp_id in _GROUP_CHAT_IDS.items():
        print(f"Group {_grp_num} ID: {'Yes' if _grp_id else 'No'}")

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
