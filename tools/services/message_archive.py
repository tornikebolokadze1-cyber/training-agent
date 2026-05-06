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

# Database path — overridable via the MESSAGE_ARCHIVE_DB_PATH env var so
# Railway (and any other host with a mounted persistent volume) can point
# the archive at a path that survives container restarts. Local default
# stays at data/messages.db so existing scripts and tests work unchanged.
# Resolved at import time; production sets this BEFORE process start.
DEFAULT_DB_PATH = Path(
    os.environ.get("MESSAGE_ARCHIVE_DB_PATH")
    or str(PROJECT_ROOT / "data" / "messages.db")
)

# Migration SQL — applied automatically on first connect() if the target
# database is empty. Lets a fresh Railway volume bootstrap itself without
# a separate migration step in the deploy pipeline.
_MIGRATION_FILE = PROJECT_ROOT / "scripts" / "migrate_001_messages.sql"

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

def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Verify migration 001 has been applied; auto-apply on first connect.

    Production volumes (Railway / mounted disks) start empty — the SQLite
    file is created on first connect with no tables. Auto-bootstrap means
    we don't need a separate migration step in the deploy pipeline; the
    first webhook hit on a fresh volume just works.

    Raises RuntimeError only if both the schema is missing AND the migration
    SQL file cannot be located on disk (which would be a packaging bug).
    """
    try:
        row = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row and row[0] >= 1:
            return  # schema already applied — fast path
    except sqlite3.OperationalError:
        pass  # schema_migrations table missing — fall through to bootstrap

    if not _MIGRATION_FILE.exists():
        raise RuntimeError(
            f"messages.db has no schema and migration file {_MIGRATION_FILE} "
            "not found — packaging bug?"
        )
    sql = _MIGRATION_FILE.read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()
    logger.info(
        "Bootstrapped messages.db schema (migration 001 applied) at %s",
        _MIGRATION_FILE,
    )


# Back-compat alias for callers that imported the previous name. The new
# name (`_ensure_schema`) better reflects the auto-bootstrap behavior.
_check_schema = _ensure_schema


@contextmanager
def connect(db_path: Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    global _SCHEMA_CHECKED
    # Ensure parent directory exists (Railway volumes start empty;
    # SQLite cannot create files in a non-existent directory).
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if not _SCHEMA_CHECKED:
            _ensure_schema(conn)
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
    Also keeps the `senders` aggregate in sync via :func:`upsert_sender`
    so that distinct sender_hashes never accumulate in `messages` without
    a matching row in `senders`.
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
    inserted = cur.rowcount > 0
    # Keep senders aggregate consistent regardless of insert vs duplicate:
    # last_seen / display names / groups should reflect the latest data we
    # have observed for this sender, even if the message itself is a dup.
    try:
        upsert_sender(
            conn,
            sender_hash=m.sender_hash,
            ts_message=m.ts_message,
            sender_display=m.sender_display,
            group_number=m.group_number,
        )
    except Exception as exc:  # noqa: BLE001 — never let aggregator crash insert
        logger.warning(
            "upsert_sender failed for hash=%s: %s",
            m.sender_hash[:12], exc,
        )
    return inserted


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
# Senders aggregator
# ---------------------------------------------------------------------------
#
# The `senders` table is an aggregate view over `messages`: one row per
# distinct sender_hash, with first_seen, last_seen, the set of groups they
# appeared in, and the set of display names they used. It exists so that
# downstream analytics, GDPR delete operations, and roster reconciliation
# can run without scanning the full messages table.
#
# Note on `phone_encrypted`: the schema declares this BLOB NOT NULL on the
# assumption that an `encrypt_phone()` helper would be wired in alongside
# the live webhook write path. That helper was deferred until SENDER_ENC_KEY
# is provisioned in production; the postgres-archive plan §4 covers it.
# Until then, we keep the column NOT NULL-compliant by storing an empty
# blob (`b''`) — a deliberate sentinel that "we know this hash but have no
# encrypted phone for it yet". When `encrypt_phone()` lands, a separate
# migration will populate non-empty blobs from any source we still have
# (live webhook payloads contain the raw phone in `senderId`).

def upsert_sender(
    conn: sqlite3.Connection,
    sender_hash: str,
    ts_message: str,
    sender_display: Optional[str] = None,
    group_number: Optional[int] = None,
    phone_encrypted: bytes = b"",
) -> None:
    """Insert a sender row, or update first_seen/last_seen/groups/names.

    Idempotent: safe to call once per `insert_message`, and safe to call
    repeatedly during a backfill. ``phone_encrypted`` defaults to an empty
    blob; once an `encrypt_phone()` helper lands it can be passed through
    from the webhook payload.
    """
    if not sender_hash:
        return

    row = conn.execute(
        """SELECT phone_encrypted, first_seen, last_seen,
                  groups_json, display_names_json
             FROM senders WHERE sender_hash = ?""",
        (sender_hash,),
    ).fetchone()

    if row is None:
        groups = [group_number] if group_number is not None else []
        names = [sender_display] if sender_display else []
        conn.execute(
            """INSERT INTO senders
               (sender_hash, phone_encrypted, first_seen, last_seen,
                groups_json, display_names_json)
               VALUES (?,?,?,?,?,?)""",
            (
                sender_hash,
                phone_encrypted,
                ts_message,
                ts_message,
                json.dumps(groups),
                json.dumps(names, ensure_ascii=False),
            ),
        )
        return

    # Existing row — extend last_seen, first_seen (only if earlier), and
    # the JSON arrays. SQLite has no native set type, so we round-trip
    # through json + Python set semantics. Cardinality is tiny (2 groups,
    # a handful of display names per sender), so this is cheap.
    existing_phone = row["phone_encrypted"] if "phone_encrypted" in row.keys() else row[0]
    new_phone = phone_encrypted if phone_encrypted else existing_phone

    new_first = min(row["first_seen"], ts_message)
    new_last = max(row["last_seen"], ts_message)

    try:
        groups = json.loads(row["groups_json"] or "[]")
    except json.JSONDecodeError:
        groups = []
    if group_number is not None and group_number not in groups:
        groups.append(group_number)
        groups.sort()

    try:
        names = json.loads(row["display_names_json"] or "[]")
    except json.JSONDecodeError:
        names = []
    if sender_display and sender_display not in names:
        names.append(sender_display)

    conn.execute(
        """UPDATE senders
              SET phone_encrypted    = ?,
                  first_seen         = ?,
                  last_seen          = ?,
                  groups_json        = ?,
                  display_names_json = ?
            WHERE sender_hash = ?""",
        (
            new_phone,
            new_first,
            new_last,
            json.dumps(groups),
            json.dumps(names, ensure_ascii=False),
            sender_hash,
        ),
    )


def backfill_senders_from_messages(conn: sqlite3.Connection) -> dict[str, int]:
    """One-shot rebuild of `senders` from the existing `messages` rows.

    Use this once after wiring the aggregator to an already-populated DB.
    Idempotent — running twice yields the same end state. Returns a small
    dict with the count of distinct sender_hashes processed.
    """
    rows = conn.execute(
        """SELECT sender_hash,
                  MIN(ts_message)  AS first_ts,
                  MAX(ts_message)  AS last_ts
             FROM messages
            WHERE sender_hash IS NOT NULL AND sender_hash != ''
            GROUP BY sender_hash"""
    ).fetchall()

    processed = 0
    for r in rows:
        sh = r["sender_hash"]
        first_ts = r["first_ts"]
        last_ts = r["last_ts"]
        # All groups this sender appeared in (1, 2, NULL/DM)
        groups_rows = conn.execute(
            """SELECT DISTINCT group_number FROM messages
                WHERE sender_hash = ?""",
            (sh,),
        ).fetchall()
        groups = sorted(g["group_number"] for g in groups_rows
                        if g["group_number"] is not None)
        # Distinct non-null display names this sender used
        names_rows = conn.execute(
            """SELECT DISTINCT sender_display FROM messages
                WHERE sender_hash = ? AND sender_display IS NOT NULL""",
            (sh,),
        ).fetchall()
        names = [n["sender_display"] for n in names_rows
                 if n["sender_display"]]

        existing = conn.execute(
            "SELECT phone_encrypted FROM senders WHERE sender_hash = ?",
            (sh,),
        ).fetchone()
        phone = existing["phone_encrypted"] if existing else b""

        conn.execute(
            """INSERT INTO senders
               (sender_hash, phone_encrypted, first_seen, last_seen,
                groups_json, display_names_json)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(sender_hash) DO UPDATE SET
                 first_seen         = excluded.first_seen,
                 last_seen          = excluded.last_seen,
                 groups_json        = excluded.groups_json,
                 display_names_json = excluded.display_names_json""",
            (
                sh,
                phone,
                first_ts,
                last_ts,
                json.dumps(groups),
                json.dumps(names, ensure_ascii=False),
            ),
        )
        processed += 1

    return {"processed": processed}


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
