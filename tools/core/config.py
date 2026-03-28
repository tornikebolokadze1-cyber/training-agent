"""Shared configuration for the Training Agent system.

Supports two deployment modes:
  - Local: reads .env file + JSON credential files from disk
  - Railway/Cloud: reads all config from environment variables,
    with JSON files provided as base64-encoded env vars
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load .env file if present (local development); in Railway, env vars
# are injected directly and this is a no-op.
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

# Detect deployment environment
RAILWAY_ENVIRONMENT = os.getenv("RAILWAY_ENVIRONMENT", "")
IS_RAILWAY = bool(RAILWAY_ENVIRONMENT)


def _env(key: str, default: str = "") -> str:
    """Get environment variable or default."""
    return os.getenv(key, default)


def _decode_b64_env(key: str) -> str | None:
    """Decode a base64-encoded environment variable to a UTF-8 string.

    Returns None if the env var is not set or empty.
    """
    raw = os.getenv(key, "")
    if not raw:
        return None
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception as exc:
        logger.error("Failed to decode base64 env var %s: %s", key, exc)
        return None


_credential_file_cache: dict[str, Path] = {}


def _materialize_credential_file(
    b64_env_key: str,
    fallback_path: Path,
    file_permissions: int = 0o600,
) -> Path:
    """Resolve a credential file from either a base64 env var or a local path.

    On Railway (no persistent filesystem), the base64 env var is decoded and
    written to a secure temp file.  Locally, the file at ``fallback_path`` is
    used directly.  Results are cached so repeated calls reuse the same file.

    Returns:
        Path to the credential file (either the original or a temp file).

    Raises:
        FileNotFoundError: If neither the env var nor the local file is available.
    """
    # Check cache first
    if b64_env_key in _credential_file_cache:
        cached = _credential_file_cache[b64_env_key]
        if cached.exists():
            return cached

    decoded = _decode_b64_env(b64_env_key)
    if decoded:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix=f"{b64_env_key.lower()}_",
            delete=False,
        )
        tmp.write(decoded)
        tmp.close()
        os.chmod(tmp.name, file_permissions)
        result = Path(tmp.name)
        _credential_file_cache[b64_env_key] = result
        logger.info("Materialized %s from env var to %s", b64_env_key, result)
        return result

    if fallback_path.exists():
        return fallback_path

    raise FileNotFoundError(
        f"Credential file not found: set {b64_env_key} env var (base64) "
        f"or place the file at {fallback_path}"
    )


def _load_attendees() -> dict[str, list[str]]:
    """Load attendee emails from base64 env var or local JSON file."""
    # Try base64 env var first (Railway)
    decoded = _decode_b64_env("ATTENDEES_JSON_B64")
    if decoded:
        try:
            return json.loads(decoded)
        except json.JSONDecodeError as exc:
            logger.error("Invalid ATTENDEES_JSON_B64: %s", exc)

    # Fall back to local file
    attendees_path = Path(__file__).parent.parent.parent / "attendees.json"
    if attendees_path.exists():
        with open(attendees_path, encoding="utf-8") as f:
            return json.load(f)
    return {"1": [], "2": []}


_ATTENDEES = _load_attendees()


# ---------------------------------------------------------------------------
# Timezone — single source of truth for all modules
# ---------------------------------------------------------------------------
TBILISI_TZ = ZoneInfo("Asia/Tbilisi")

# Minimum meeting duration (minutes) to consider a meeting "really ended".
# Below this threshold, a meeting.ended event is treated as a temporary
# disconnect (break/reconnect) — the pipeline will NOT start.
try:
    MINIMUM_LECTURE_DURATION_MINUTES = int(_env("MINIMUM_LECTURE_DURATION_MINUTES", "120"))
except (ValueError, TypeError):
    MINIMUM_LECTURE_DURATION_MINUTES = 120

# ---------------------------------------------------------------------------
# Group Definitions
# ---------------------------------------------------------------------------
# Group #1: Tuesday/Friday 20:00-22:00 Georgian time (GMT+4)
# Group #2: Monday/Thursday 20:00-22:00 Georgian time (GMT+4)
# Lecture #1 already completed for both groups.


class GroupConfig(TypedDict):
    """Type definition for training group configuration."""

    name: str
    folder_name: str
    drive_folder_id: str
    analysis_folder_id: str
    zoom_meeting_id: str
    meeting_days: list[int]
    start_date: date
    attendee_emails: list[str]


GROUPS: dict[int, GroupConfig] = {
    1: {
        "name": "მარტის ჯგუფი #1",
        "folder_name": "AI კურსი (მარტის ჯგუფი #1. 2026)",
        "drive_folder_id": _env("DRIVE_GROUP1_FOLDER_ID"),
        "analysis_folder_id": _env("DRIVE_GROUP1_ANALYSIS_FOLDER_ID"),
        "zoom_meeting_id": _env("ZOOM_GROUP1_MEETING_ID"),
        "meeting_days": [1, 4],  # Tuesday=1, Friday=4 (Monday=0)
        "start_date": date(2026, 3, 13),  # First lecture: Friday March 13
        "attendee_emails": _ATTENDEES.get("1", []),
    },
    2: {
        "name": "მარტის ჯგუფი #2",
        "folder_name": "AI კურსი (მარტის ჯგუფი #2. 2026)",
        "drive_folder_id": _env("DRIVE_GROUP2_FOLDER_ID"),
        "analysis_folder_id": _env("DRIVE_GROUP2_ANALYSIS_FOLDER_ID"),
        "zoom_meeting_id": _env("ZOOM_GROUP2_MEETING_ID"),
        "meeting_days": [0, 3],  # Monday=0, Thursday=3
        "start_date": date(2026, 3, 12),  # First lecture: Thursday March 12
        "attendee_emails": _ATTENDEES.get("2", []),
    },
}

TOTAL_LECTURES = 15

# Lecture folder IDs will be populated after folder creation.
# Format: {group_number: {lecture_number: folder_id}}
LECTURE_FOLDER_IDS: dict[int, dict[int, str]] = {1: {}, 2: {}}

# ---------------------------------------------------------------------------
# API Credentials
# ---------------------------------------------------------------------------

ZOOM_ACCOUNT_ID = _env("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID = _env("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = _env("ZOOM_CLIENT_SECRET")
ZOOM_WEBHOOK_SECRET_TOKEN = _env("ZOOM_WEBHOOK_SECRET_TOKEN", "")

# Google OAuth credentials file — resolved from base64 env var or local file.
# This is evaluated lazily via a function to avoid crashing at import time
# if credentials are not yet needed.
_google_credentials_path: Path | None = None


def get_google_credentials_path() -> Path:
    """Return the path to the Google OAuth credentials.json file.

    On Railway, decodes GOOGLE_CREDENTIALS_JSON_B64 to a temp file.
    Locally, uses the file at GOOGLE_CREDENTIALS_PATH (default ./credentials.json).
    """
    global _google_credentials_path
    if _google_credentials_path is not None and _google_credentials_path.exists():
        return _google_credentials_path

    local_path = Path(_env("GOOGLE_CREDENTIALS_PATH", "./credentials.json"))
    _google_credentials_path = _materialize_credential_file(
        "GOOGLE_CREDENTIALS_JSON_B64", local_path
    )
    return _google_credentials_path


# Keep backward-compatible string for modules that import this directly,
# but prefer get_google_credentials_path() in new code.
GOOGLE_CREDENTIALS_PATH = _env("GOOGLE_CREDENTIALS_PATH", "./credentials.json")
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_API_KEY_PAID = _env("GEMINI_API_KEY_PAID")

# Green API (WhatsApp) — replaces ManyChat
GREEN_API_INSTANCE_ID = _env("GREEN_API_INSTANCE_ID")
GREEN_API_TOKEN = _env("GREEN_API_TOKEN")
WHATSAPP_TORNIKE_PHONE = _env("WHATSAPP_TORNIKE_PHONE")  # e.g. "995599123456"
WHATSAPP_GROUP1_ID = _env("WHATSAPP_GROUP1_ID")  # e.g. "120363XXX@g.us"
WHATSAPP_GROUP2_ID = _env("WHATSAPP_GROUP2_ID")

# Anthropic API (Claude Opus 4.6 — assistant reasoning engine)
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")

# Pinecone (vector DB for course knowledge)
PINECONE_API_KEY = _env("PINECONE_API_KEY")
PINECONE_INDEX_NAME = "training-course"

WEBHOOK_SECRET = _env("WEBHOOK_SECRET")
N8N_CALLBACK_URL = _env("N8N_CALLBACK_URL")

SERVER_HOST = _env("SERVER_HOST", "0.0.0.0" if IS_RAILWAY else "127.0.0.1")
try:
    SERVER_PORT = int(_env("SERVER_PORT", _env("PORT", "5001")))
except (ValueError, TypeError):
    SERVER_PORT = 5001
SERVER_PUBLIC_URL = _env("SERVER_PUBLIC_URL")  # e.g. "https://abc123.ngrok.io"

# ---------------------------------------------------------------------------
# Startup validation — fail fast on missing critical config
# ---------------------------------------------------------------------------


def validate_critical_config() -> list[str]:
    """Check that critical environment variables are set.

    Returns a list of warning messages for missing non-critical vars.
    Raises RuntimeError if any critical var is missing.
    """
    warnings: list[str] = []

    # Critical for production — server won't work without these
    critical_missing = []
    if not WEBHOOK_SECRET:
        critical_missing.append("WEBHOOK_SECRET")

    # Important but not fatal — specific features won't work
    if not GEMINI_API_KEY and not GEMINI_API_KEY_PAID:
        warnings.append("No Gemini API key configured (GEMINI_API_KEY or GEMINI_API_KEY_PAID)")
    if not ANTHROPIC_API_KEY:
        warnings.append("ANTHROPIC_API_KEY not set — Claude reasoning disabled")
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        warnings.append("Green API not configured — WhatsApp notifications disabled")
    if not WHATSAPP_TORNIKE_PHONE:
        warnings.append("WHATSAPP_TORNIKE_PHONE not set — operator alerts disabled")

    # These are HIGH-risk if missing — warn at startup
    _warn_vars = [
        ("ZOOM_WEBHOOK_SECRET_TOKEN", "Zoom webhooks will return 503"),
        ("DRIVE_GROUP1_FOLDER_ID", "Group 1 Drive uploads will fail"),
        ("DRIVE_GROUP2_FOLDER_ID", "Group 2 Drive uploads will fail"),
        ("DRIVE_GROUP1_ANALYSIS_FOLDER_ID", "Group 1 analysis reports won't upload"),
        ("DRIVE_GROUP2_ANALYSIS_FOLDER_ID", "Group 2 analysis reports won't upload"),
        ("WHATSAPP_GROUP1_ID", "Group 1 WhatsApp notifications will fail"),
        ("WHATSAPP_GROUP2_ID", "Group 2 WhatsApp notifications will fail"),
    ]
    for var_name, consequence in _warn_vars:
        if not os.environ.get(var_name):
            logger.warning("Missing %s — %s", var_name, consequence)
            warnings.append(f"Missing {var_name} — {consequence}")

    # Log warnings
    for w in warnings:
        logger.warning("Config: %s", w)

    # Critical failures only in Railway (production)
    if IS_RAILWAY and critical_missing:
        raise RuntimeError(
            f"Critical env vars missing in production: {', '.join(critical_missing)}"
        )
    elif critical_missing:
        for var in critical_missing:
            logger.warning("Config: %s not set (OK for local dev)", var)

    return warnings


# Run validation at import time
_config_warnings = validate_critical_config()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent
TMP_DIR = PROJECT_ROOT / ".tmp"
TMP_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Gemini Config
# ---------------------------------------------------------------------------

# Hybrid model strategy: Pro for long video transcription, 3.1 Pro for Georgian text writing
GEMINI_MODEL_TRANSCRIPTION = "gemini-2.5-flash"  # Multimodal transcription (cheaper, video chunked to fit 1M token limit)
GEMINI_MODEL_ANALYSIS = "gemini-3.1-pro-preview"  # Smartest for Georgian text writing

# Prompt templates moved to tools/core/prompts.py — re-exported for backward compatibility
from tools.core.prompts import (  # noqa: F401, E402
    DEEP_ANALYSIS_PROMPT,
    GAP_ANALYSIS_PROMPT,
    SUMMARIZATION_PROMPT,
    TRANSCRIPTION_CONTINUATION_PROMPT,
    TRANSCRIPTION_PROMPT,
)

# WhatsApp Assistant ("მრჩეველი") config
ASSISTANT_NAME = "მრჩეველი"
ASSISTANT_TRIGGER_WORD = "მრჩეველო"
ASSISTANT_SIGNATURE = "AI ასისტენტი - მრჩეველი"
ASSISTANT_COOLDOWN_SECONDS = 300  # 5 min between passive responses
ASSISTANT_CLAUDE_MODEL = "claude-opus-4-6"
GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_lecture_number(group_number: int, for_date: date | None = None) -> int:
    """Calculate which lecture number falls on the given date.

    Counts the number of meeting days from the group's start date
    up to and including ``for_date``.
    """
    if for_date is None:
        for_date = date.today()

    group = GROUPS[group_number]
    start = group["start_date"]
    meeting_days = group["meeting_days"]

    if for_date < start:
        return 0

    count = 0
    current = start
    while current <= for_date:
        if current.weekday() in meeting_days:
            count += 1
        current += timedelta(days=1)

    return count


def get_group_for_weekday(weekday: int) -> int | None:
    """Return group number for a given weekday (Monday=0), or None."""
    for group_num, group in GROUPS.items():
        if weekday in group["meeting_days"]:
            return group_num
    return None


def get_lecture_folder_name(lecture_number: int) -> str:
    """Return Georgian folder name for a lecture number."""
    return f"ლექცია #{lecture_number}"


def extract_group_from_topic(topic: str) -> int | None:
    """Extract group number from a Zoom meeting topic string.

    Searches for the short Georgian group marker (ჯგუფი #N) in the topic,
    which is more robust than matching the full group name — Zoom topics
    may vary but always contain "ჯგუფი #1" or "ჯგუფი #2".

    Returns:
        Group number (1 or 2) if found, None otherwise.
    """
    if not isinstance(topic, str) or not topic.strip():
        logger.warning("extract_group_from_topic called with invalid topic: %r", topic)
        return None
    for group_num in GROUPS:
        if f"ჯგუფი #{group_num}" in topic:
            return group_num
    logger.debug("No group marker found in topic: %s", topic[:80])
    return None


def get_drive_file_url(file_id: str, is_doc: bool = False) -> str:
    """Build a shareable Google Drive/Docs URL."""
    if is_doc:
        return f"https://docs.google.com/document/d/{file_id}/edit"
    return f"https://drive.google.com/file/d/{file_id}/view"
