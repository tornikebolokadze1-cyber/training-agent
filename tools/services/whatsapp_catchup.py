"""WhatsApp assistant catch-up service.

Recovers messages that should have triggered an assistant response but
didn't — typically because the live ``/whatsapp-incoming`` webhook was
unreachable (boot hang, deploy in progress, transient Green API outage)
or because the trigger logic had a gap that a later fix closed.

The service is intentionally idempotent:

* It pulls the last N messages of each allowed chat via Green API's
  ``getChatHistory`` endpoint.
* For each incoming message that fits the freshness window, it checks
  whether the assistant ALREADY responded (by looking for an outgoing
  bot message within 3 minutes after the candidate).
* Already-handled IDs are persisted in ``.tmp/whatsapp_responded.json``
  so subsequent runs across restarts never double-respond.
* Per-chat caps prevent a flood of belated replies after a long outage.

Entry points:
    * :func:`replay_recent` — programmatic call from startup hook,
      scheduler, or admin endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.core.config import (
    ASSISTANT_SIGNATURE,
    ASSISTANT_TRIGGER_WORD,
    TMP_DIR,
    WHATSAPP_GROUP1_ID,
    WHATSAPP_GROUP2_ID,
    WHATSAPP_TORNIKE_PHONE,
)
from tools.integrations.whatsapp_sender import get_chat_history
from tools.services.whatsapp_assistant import IncomingMessage, WhatsAppAssistant

logger = logging.getLogger(__name__)

DEDUP_FILE: Path = TMP_DIR / "whatsapp_responded.json"
DEDUP_CAP = 1000  # keep last N responded IDs
RESPONSE_LOOKAHEAD_SECONDS = 180  # consider trigger answered if bot reply within 3 min
DEFAULT_SINCE_MINUTES = 120
DEFAULT_MAX_PER_CHAT = 5


# ---------------------------------------------------------------------------
# Persistence — responded-IDs ledger
# ---------------------------------------------------------------------------


def _load_responded_ids() -> set[str]:
    """Read the persisted set of green_api_ids the bot has already replied to."""
    if not DEDUP_FILE.exists():
        return set()
    try:
        data = json.loads(DEDUP_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[catchup] dedup file unreadable, starting fresh: %s", exc)
        return set()
    ids = data.get("ids") if isinstance(data, dict) else None
    if not isinstance(ids, list):
        return set()
    return {str(i) for i in ids}


def _save_responded_ids(ids: set[str]) -> None:
    """Persist the responded-IDs ledger, capped to the most recent ``DEDUP_CAP``."""
    try:
        DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Keep the tail (most recent — rough proxy by insertion order)
        kept = list(ids)[-DEDUP_CAP:]
        DEDUP_FILE.write_text(
            json.dumps({"ids": kept, "updated_at": int(time.time())}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning("[catchup] failed to persist dedup ledger: %s", exc)


# ---------------------------------------------------------------------------
# Green API → IncomingMessage
# ---------------------------------------------------------------------------


def _extract_text_and_quote(raw: dict[str, Any]) -> tuple[str, str]:
    """Pull message body and quoted-message body from a getChatHistory entry.

    Green API uses different shapes depending on whether the message was a
    plain text, an extended text (with mention/quote), or a quoted reply.
    """
    text = raw.get("textMessage") or ""
    quoted_text = ""
    ext = raw.get("extendedTextMessage")
    if isinstance(ext, dict):
        if not text:
            text = ext.get("text") or ""
        quoted = ext.get("quotedMessage")
        if isinstance(quoted, dict):
            quoted_text = quoted.get("textMessage") or quoted.get("text") or ""
    if not quoted_text:
        # Older shape: top-level quotedMessage
        quoted = raw.get("quotedMessage")
        if isinstance(quoted, dict):
            quoted_text = quoted.get("textMessage") or quoted.get("text") or ""
    return text, quoted_text


def _build_incoming(raw: dict[str, Any], chat_id: str) -> IncomingMessage | None:
    """Construct an IncomingMessage from a Green API history entry, or None to skip."""
    text, quoted_text = _extract_text_and_quote(raw)
    if not text.strip() and not quoted_text.strip():
        return None
    sender_id = str(raw.get("senderId") or raw.get("chatId") or chat_id)
    sender_name = raw.get("senderName") or raw.get("senderContactName") or ""
    timestamp = int(raw.get("timestamp") or 0)
    return IncomingMessage(
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=str(sender_name)[:80],
        text=text,
        quoted_text=quoted_text,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Trigger and "already-answered" detection
# ---------------------------------------------------------------------------


def _has_trigger_word(text: str) -> bool:
    """Lightweight pre-filter mirroring WhatsAppAssistant._is_direct_mention.

    Kept local so the catchup service can decide whether to spend an
    LLM call without instantiating the full assistant.
    """
    if not text:
        return False
    normalized = unicodedata.normalize("NFC", text).lower()
    triggers = (
        unicodedata.normalize("NFC", ASSISTANT_TRIGGER_WORD).lower(),
        unicodedata.normalize("NFC", "მრჩეველი"),
        "mrchevelo",
        "mrcheveli",
    )
    return any(t in normalized for t in triggers)


def _quoted_looks_like_bot(quoted_text: str) -> bool:
    """Mirror of WhatsAppAssistant._is_reply_to_bot, kept local for filtering."""
    if not quoted_text:
        return False
    normalized = unicodedata.normalize("NFC", quoted_text)
    sig = unicodedata.normalize("NFC", ASSISTANT_SIGNATURE)
    if sig in normalized[:200]:
        return True
    return _has_trigger_word(quoted_text)


def _bot_already_replied(history: list[dict[str, Any]], trigger_idx: int) -> bool:
    """Return True if a bot message follows the trigger within the lookahead window.

    ``history`` is in newest-first order, so messages BEFORE ``trigger_idx``
    in the list are NEWER than the trigger.
    """
    trigger = history[trigger_idx]
    trigger_ts = int(trigger.get("timestamp") or 0)
    if trigger_ts == 0:
        return False
    for j in range(trigger_idx - 1, -1, -1):
        msg = history[j]
        ts = int(msg.get("timestamp") or 0)
        if ts < trigger_ts or ts - trigger_ts > RESPONSE_LOOKAHEAD_SECONDS:
            continue
        if msg.get("type") == "outgoing":
            return True
    return False


def _allowed_chats() -> list[str]:
    """Return the list of chat IDs the catchup service is allowed to scan."""
    chats: list[str] = []
    if WHATSAPP_TORNIKE_PHONE:
        chats.append(f"{WHATSAPP_TORNIKE_PHONE}@c.us")
    if WHATSAPP_GROUP1_ID:
        chats.append(WHATSAPP_GROUP1_ID)
    if WHATSAPP_GROUP2_ID:
        chats.append(WHATSAPP_GROUP2_ID)
    return chats


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


@dataclass
class CatchupResult:
    """Aggregated outcome of a catchup run, suitable for JSON return."""

    checked: int = 0
    replied: int = 0
    already_responded: int = 0
    out_of_window: int = 0
    not_a_trigger: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "replied": self.replied,
            "already_responded": self.already_responded,
            "out_of_window": self.out_of_window,
            "not_a_trigger": self.not_a_trigger,
            "failed": self.failed,
        }


async def replay_recent(
    assistant: WhatsAppAssistant,
    since_minutes: int = DEFAULT_SINCE_MINUTES,
    max_per_chat: int = DEFAULT_MAX_PER_CHAT,
) -> dict[str, int]:
    """Scan recent chat history and replay any unanswered triggers.

    Args:
        assistant: An already-initialised WhatsAppAssistant instance.
        since_minutes: How far back to look. Older messages are ignored.
        max_per_chat: Cap on belated replies per chat to avoid flooding.

    Returns:
        Aggregated counts as a flat dict.
    """
    result = CatchupResult()
    cutoff_ts = int(time.time()) - since_minutes * 60
    responded = _load_responded_ids()
    chats = _allowed_chats()

    if not chats:
        logger.info("[catchup] no allowed chats configured — nothing to do")
        return result.as_dict()

    for chat_id in chats:
        try:
            history = await asyncio.to_thread(get_chat_history, chat_id, 100)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("[catchup] get_chat_history failed for %s: %s", chat_id, exc)
            continue

        if not history:
            continue

        per_chat_replied = 0
        # Iterate newest → oldest. Stop replying once we've hit the cap.
        for idx, raw in enumerate(history):
            result.checked += 1

            # Skip our own messages.
            if raw.get("type") == "outgoing":
                continue

            ts = int(raw.get("timestamp") or 0)
            if ts < cutoff_ts:
                result.out_of_window += 1
                # History is newest-first; once we cross the cutoff we can stop.
                break

            green_id = str(raw.get("idMessage") or "")
            if green_id and green_id in responded:
                result.already_responded += 1
                continue

            text, quoted_text = _extract_text_and_quote(raw)
            is_trigger = _has_trigger_word(text) or _quoted_looks_like_bot(quoted_text)
            if not is_trigger:
                result.not_a_trigger += 1
                continue

            if _bot_already_replied(history, idx):
                result.already_responded += 1
                if green_id:
                    responded.add(green_id)
                continue

            if per_chat_replied >= max_per_chat:
                # Conservative: don't carpet-bomb the chat after a long outage.
                logger.info(
                    "[catchup] per-chat cap reached for %s — skipping older triggers",
                    chat_id,
                )
                break

            message = _build_incoming(raw, chat_id)
            if message is None:
                continue

            try:
                reply = await assistant._respond_to_missed(message)
            except Exception as exc:
                logger.error(
                    "[catchup] _respond_to_missed crashed for %s in %s: %s",
                    green_id, chat_id, exc, exc_info=True,
                )
                result.failed += 1
                continue

            if reply:
                result.replied += 1
                per_chat_replied += 1
                if green_id:
                    responded.add(green_id)
                logger.info(
                    "[catchup] replayed message %s in %s (sender=%s)",
                    green_id, chat_id, message.sender_name or message.sender_id,
                )
            else:
                # Assistant decided silent or sending failed — record either way
                # so we don't keep retrying every cycle.
                if green_id:
                    responded.add(green_id)

    _save_responded_ids(responded)
    logger.info("[catchup] run complete: %s", result.as_dict())
    return result.as_dict()
