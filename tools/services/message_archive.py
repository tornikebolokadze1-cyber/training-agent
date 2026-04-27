"""Durable WhatsApp message archive.

Replaces the in-memory `_chat_history: dict[-15:]` with a SQLite-backed
(Postgres-portable) message log. Primary design goal: zero message loss
across restarts, full raw payload preserved for retrospective analysis.

Current state: STANDALONE — not yet hooked into the live webhook path.
Use via the backfill script. Wiring into `whatsapp_assistant.py` is a
separate, reviewed change.

Key design:
    * `green_api_id UNIQUE` → webhook replays produce no duplicates.
    * `sender_hash = HMAC-SHA256(phone, PEPPER)` → deterministic across
      restarts, pre-image resistant. Requires env `SENDER_HASH_PEPPER`.
    * INSERT wraps `ON CONFLICT DO NOTHING` (SQLite: `INSERT OR IGNORE`)
      so backfill can be re-run safely.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "messages.db"

# Placeholder pepper used when env var missing.  Logs a warning so the
# operator notices; NEVER ship this to production — rotate all hashes
# once real pepper is configured.
_DEV_PEPPER_MARKER = "__DEV_PEPPER_DO_NOT_SHIP__"
_PEPPER_WARNED = False  # emit dev-pepper warning only once per process

# Green API group chat IDs are mapped here for group_number inference.
# Populated lazily from env to keep this module import-safe for tests.
_GROUP_ID_MAP: dict[str, int] | None = None

# Schema check runs once per process.
_SCHEMA_CHECKED = False


def _load_group_map() -> dict[str, int]:
    global _GROUP_ID_MAP
    if _GROUP_ID_MAP is None:
        _GROUP_ID_MAP = {}
        g1 = os.environ.get("WHATSAPP_GROUP1_ID")
        g2 = os.environ.get("WHATSAPP_GROUP2_ID")
        if g1:
            _GROUP_ID_MAP[g1] = 1
        if g2:
            _GROUP_ID_MAP[g2] = 2
    return _GROUP_ID_MAP


def _pepper() -> bytes:
    global _PEPPER_WARNED
    val = os.environ.get("SENDER_HASH_PEPPER")
    if not val:
        if os.environ.get("RAILWAY_ENVIRONMENT"):
            raise RuntimeError(
                "SENDER_HASH_PEPPER must be set in production — refusing dev placeholder"
            )
        if not _PEPPER_WARNED:
            logger.warning(
                "SENDER_HASH_PEPPER not set — using dev placeholder. "
                "All hashes produced are insecure and non-portable."
            )
            _PEPPER_WARNED = True
        return _DEV_PEPPER_MARKER.encode("utf-8")
    return val.encode("utf-8")


def _pepper_fingerprint() -> str:
    return hashlib.sha256(_pepper()).hexdigest()[:16]


def sender_hash(phone_or_id: str) -> str:
    """Deterministic HMAC-SHA256 hash of a phone or sender ID.

    Same input + same pepper → same hash (enables JOINs). Pepper leak
    requires re-hashing; phone numbers are not recoverable from the hash.
    """
    if not phone_or_id:
        raise ValueError("sender_hash requires a non-empty identifier")
    normalized = phone_or_id.strip().lower()
    return hmac.new(_pepper(), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def _utc_iso(ts: int | float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class IngestedMessage:
    """Normalized row shape prior to INSERT."""

    green_api_id: str
    chat_id: str
    sender_hash: str
    sender_display: Optional[str]
    ts_message: str  # ISO-8601 UTC
    direction: str  # 'incoming' | 'outgoing'
    msg_type: str
    content: Optional[str]
    quoted_green_id: Optional[str]
    raw_payload: dict[str, Any]
    group_number: Optional[int]
    is_bot: bool


def normalize_green_api_message(
    msg: dict[str, Any],
    chat_id: str,
    group_number: Optional[int] = None,
) -> IngestedMessage:
    """Convert a Green API getChatHistory payload into an IngestedMessage.

    Handles multiple message types defensively. Unknown fields preserved
    in raw_payload for forensic lookup.
    """
    if group_number is None:
        group_number = _load_group_map().get(chat_id)

    green_id = msg.get("idMessage") or msg.get("id") or ""
    if not green_id:
        raise ValueError(f"message missing idMessage: {msg}")

    ts = msg.get("timestamp")
    if not ts:
        raise ValueError(f"message {green_id} missing timestamp")
    ts_iso = _utc_iso(int(ts))

    sender_id = msg.get("senderId") or msg.get("chatId") or chat_id
    sender_display = msg.get("senderName") or msg.get("senderContactName")
    msg_type = msg.get("typeMessage", "unknown")

    # Extract textual content based on message type.
    # Use explicit branches to avoid `A or B if C else D` operator-precedence
    # trap (parsed as `A or (B if C else D)`, not `(A or B) if C else D`).
    ext = msg.get("extendedTextMessage")
    ext_text = ext.get("text") if isinstance(ext, dict) else None
    content: Optional[str] = msg.get("textMessage") or ext_text
    if not content:
        content = msg.get("caption")  # images/videos may carry a caption
    if not content and msg_type == "reactionMessage":
        content = msg.get("reaction") or ext_text

    quoted_green_id = None
    quoted = msg.get("quotedMessage") or msg.get("quotedMessageId")
    if isinstance(quoted, dict):
        quoted_green_id = quoted.get("stanzaId") or quoted.get("idMessage")
    elif isinstance(quoted, str):
        quoted_green_id = quoted

    direction = "outgoing" if msg.get("type") == "outgoing" else "incoming"
    is_bot = direction == "outgoing"  # simple heuristic; refined once agent writes own rows

    return IngestedMessage(
        green_api_id=str(green_id),
        chat_id=chat_id,
        sender_hash=sender_hash(str(sender_id)),
        sender_display=sender_display,
        ts_message=ts_iso,
        direction=direction,
        msg_type=msg_type,
        content=content,
        quoted_green_id=quoted_green_id,
        raw_payload=msg,
        group_number=group_number,
        is_bot=is_bot,
    )


def normalize_webhook_message(
    body: dict[str, Any],
    group_number: Optional[int] = None,
) -> IngestedMessage:
    """Convert a Green API webhook payload into an IngestedMessage.

    Webhook shape differs from getChatHistory: idMessage and timestamp at
    the top level; senderData / messageData are nested. Direction is
    inferred from typeWebhook (incomingMessageReceived → incoming,
    outgoingMessage* → outgoing) and the fromMe flag.

    Raises ValueError if the payload is missing idMessage or timestamp.
    Unknown message types are preserved in raw_payload for forensics.
    """
    type_webhook = body.get("typeWebhook", "")
    sender_data = body.get("senderData", {}) or {}
    message_data = body.get("messageData", {}) or {}
    type_message = message_data.get("typeMessage", "unknown")
    chat_id = sender_data.get("chatId", "")

    if group_number is None:
        group_number = _load_group_map().get(chat_id)

    green_id = body.get("idMessage") or ""
    if not green_id:
        raise ValueError(f"webhook missing idMessage: typeWebhook={type_webhook}")

    ts = body.get("timestamp")
    if not ts:
        raise ValueError(f"webhook {green_id} missing timestamp")
    ts_iso = _utc_iso(int(ts))

    sender_id = sender_data.get("sender") or sender_data.get("chatId") or chat_id
    sender_display = sender_data.get("senderName") or sender_data.get("senderContactName")

    content: Optional[str] = None
    if type_message == "textMessage":
        text_md = message_data.get("textMessageData", {}) or {}
        content = text_md.get("textMessage")
    elif type_message in ("extendedTextMessage", "quotedMessage"):
        ext = message_data.get("extendedTextMessageData", {}) or {}
        content = ext.get("text")
    elif type_message == "imageMessage":
        file_md = message_data.get("fileMessageData", {}) or {}
        content = file_md.get("caption")
    elif type_message == "reactionMessage":
        reaction_md = message_data.get("reactionMessageData", {}) or {}
        content = reaction_md.get("emoji") or reaction_md.get("reaction")

    quoted_green_id = None
    ext = message_data.get("extendedTextMessageData", {}) or {}
    quoted = ext.get("quotedMessage") or {}
    if isinstance(quoted, dict):
        quoted_green_id = quoted.get("stanzaId") or quoted.get("idMessage")

    from_me = bool(message_data.get("fromMe", False))
    is_outgoing = from_me or type_webhook.startswith("outgoing")
    direction = "outgoing" if is_outgoing else "incoming"

    return IngestedMessage(
        green_api_id=str(green_id),
        chat_id=chat_id,
        sender_hash=sender_hash(str(sender_id)),
        sender_display=sender_display,
        ts_message=ts_iso,
        direction=direction,
        msg_type=type_message,
        content=content,
        quoted_green_id=quoted_green_id,
        raw_payload=body,
        group_number=group_number,
        is_bot=is_outgoing,
    )


def archive_webhook_payload(
    body: dict[str, Any],
    db_path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Single-call API for live webhook handlers.

    Normalizes and inserts a Green API webhook payload into messages.db.
    Designed to be called inside a try/except wrapper at the webhook
    boundary so archive failure never blocks the bot's reply path:
    durability matters, but the live response matters more.

    Returns
    -------
    dict
        {'inserted': bool, 'green_api_id': str, 'reason': str | None}.
        'reason' is None on a successful new insert, 'duplicate' if the
        message was already archived, or an error string otherwise.
    """
    try:
        msg = normalize_webhook_message(body)
    except Exception as exc:
        logger.warning("archive_webhook_payload: normalize failed: %s", exc)
        return {"inserted": False, "green_api_id": "", "reason": f"normalize_error: {exc}"}

    try:
        with connect(db_path) as conn:
            inserted = insert_message(conn, msg)
        return {
            "inserted": inserted,
            "green_api_id": msg.green_api_id,
            "reason": None if inserted else "duplicate",
        }
    except Exception as exc:
        logger.warning(
            "archive_webhook_payload: insert failed for %s: %s",
            msg.green_api_id,
            exc,
        )
        return {
            "inserted": False,
            "green_api_id": msg.green_api_id,
            "reason": f"insert_error: {exc}",
        }


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def _check_schema(conn: sqlite3.Connection) -> None:
    """Verify that migration 001 has been applied. Raises RuntimeError if not."""
    try:
        row = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if not row or row[0] < 1:
            raise RuntimeError(
                "messages.db missing migration 001; "
                "run scripts/migrate_001_messages.sql first"
            )
    except sqlite3.OperationalError as e:
        raise RuntimeError(f"schema_migrations table missing: {e}") from e


@contextmanager
def connect(db_path: Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    global _SCHEMA_CHECKED
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if not _SCHEMA_CHECKED:
            _check_schema(conn)
            _SCHEMA_CHECKED = True
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_message(conn: sqlite3.Connection, m: IngestedMessage) -> bool:
    """Insert a message. Returns True if newly inserted, False if duplicate.

    Idempotent via UNIQUE(green_api_id) + INSERT OR IGNORE.
    """
    cur = conn.execute(
        """INSERT OR IGNORE INTO messages
           (green_api_id, chat_id, sender_hash, sender_display,
            ts_message, direction, msg_type, content, quoted_green_id,
            raw_payload, group_number, is_bot)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            m.green_api_id,
            m.chat_id,
            m.sender_hash,
            m.sender_display,
            m.ts_message,
            m.direction,
            m.msg_type,
            m.content,
            m.quoted_green_id,
            json.dumps(m.raw_payload, ensure_ascii=False),
            m.group_number,
            1 if m.is_bot else 0,
        ),
    )
    return cur.rowcount > 0


def bulk_insert(conn: sqlite3.Connection, messages: list[IngestedMessage]) -> dict[str, int]:
    """Insert many messages. Returns {'inserted': N, 'skipped': N}."""
    inserted = 0
    skipped = 0
    for m in messages:
        if insert_message(conn, m):
            inserted += 1
        else:
            skipped += 1
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Query helpers (retrospective analysis)
# ---------------------------------------------------------------------------

def count_by_group(conn: sqlite3.Connection) -> dict[str, int]:
    cur = conn.execute(
        """SELECT COALESCE(group_number, 0) AS g, COUNT(*) AS n
           FROM messages GROUP BY g"""
    )
    return {f"group_{row['g']}" if row["g"] else "dm": row["n"] for row in cur}


def messages_in_window(
    conn: sqlite3.Connection,
    group_number: int,
    start_iso: str,
    end_iso: str,
) -> list[sqlite3.Row]:
    return list(conn.execute(
        """SELECT * FROM messages
           WHERE group_number = ?
             AND ts_message BETWEEN ? AND ?
           ORDER BY ts_message""",
        (group_number, start_iso, end_iso),
    ))


def search_content(
    conn: sqlite3.Connection,
    pattern: str,
    group_number: Optional[int] = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Naive LIKE-based search. For Postgres, swap for tsvector + regexp."""
    params: list[Any] = [f"%{pattern}%"]
    sql = "SELECT * FROM messages WHERE content LIKE ?"
    if group_number is not None:
        sql += " AND group_number = ?"
        params.append(group_number)
    sql += " ORDER BY ts_message DESC LIMIT ?"
    params.append(limit)
    return list(conn.execute(sql, params))
