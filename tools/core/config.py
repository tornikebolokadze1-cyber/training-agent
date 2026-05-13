"""Shared configuration for the Training Agent system.

Supports two deployment modes:
  - Local: reads .env file + JSON credential files from disk
  - Railway/Cloud: reads all config from environment variables,
    with JSON files provided as base64-encoded env vars
"""

from __future__ import annotations

import base64
import atexit
import json
import logging
import os
import tempfile
from collections.abc import Iterator
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


PRESENTATION_APP_URL = _env(
    "PRESENTATION_APP_URL",
    "https://presentation-production-7e47.up.railway.app/",
)


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


def _cleanup_materialized_credential_files() -> None:
    """Remove credential files decoded from base64 env vars at process exit."""
    for path in list(_credential_file_cache.values()):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Failed to remove materialized credential file: %s", exc)


atexit.register(_cleanup_materialized_credential_files)


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
        logger.info("Materialized %s from env var to a private temp file", b64_env_key)
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
    # Build a fallback dict covering every statically known group key.
    # Optional groups (3+) pick up their attendee lists via
    # _ATTENDEES.get(str(n), []) inside _load_optional_groups(), which
    # runs after _ATTENDEES is assigned, so they always fall back to [].
    return {str(n): [] for n in range(1, 10)}


_ATTENDEES = _load_attendees()


# ---------------------------------------------------------------------------
# Timezone — single source of truth for all modules
# ---------------------------------------------------------------------------
TBILISI_TZ = ZoneInfo("Asia/Tbilisi")

# Minimum meeting duration (minutes) to consider a meeting "really ended".
# Below this threshold, a meeting.ended event is treated as a temporary
# disconnect (break/reconnect) — the pipeline will NOT start.
# Set to 110 min (not 120) because lectures are 2h but may end up to 10 min early.
# A 76-min disconnect at the old threshold (75) would have triggered the full
# irreversible pipeline prematurely.
try:
    MINIMUM_LECTURE_DURATION_MINUTES = int(_env("MINIMUM_LECTURE_DURATION_MINUTES", "110"))
except (ValueError, TypeError):
    MINIMUM_LECTURE_DURATION_MINUTES = 110

# ---------------------------------------------------------------------------
# Group Definitions
# ---------------------------------------------------------------------------
# Group #1: Tuesday/Friday 20:00-22:00 Georgian time (GMT+4)
# Group #2: Monday/Thursday 20:00-22:00 Georgian time (GMT+4)
# Lecture #1 already completed for both groups.


class GroupConfig(TypedDict, total=False):
    """Type definition for training group configuration.

    ``course_completed`` is the per-group lifecycle flag introduced when
    Course #1 wrapped up. When True, the scheduler skips lecture cron
    registration for that group while the WhatsApp advisor (მრჩეველი)
    keeps running for it. Defaults to False (active course) when the
    field is absent — see ``iter_active_groups()``.

    ``whatsapp_chat_id`` mirrors WHATSAPP_GROUP{N}_ID env vars but lives
    on the group entry so per-chat lookups don't require module-level
    constants for every new group.

    ``total`` is False so older entries that pre-date the new fields
    still type-check while we migrate.
    """

    name: str
    folder_name: str
    drive_folder_id: str
    analysis_folder_id: str
    zoom_meeting_id: str
    meeting_days: list[int]
    start_date: date
    attendee_emails: list[str]
    whatsapp_chat_id: str
    course_completed: bool


# Weekday integer (Monday=0) → APScheduler CronTrigger day_of_week code
_WEEKDAY_CRON_NAMES: tuple[str, ...] = (
    "mon", "tue", "wed", "thu", "fri", "sat", "sun",
)


def weekday_to_cron(weekday: int) -> str:
    """Map a Python weekday integer (Monday=0) to APScheduler cron code."""
    return _WEEKDAY_CRON_NAMES[weekday]


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
        "whatsapp_chat_id": _env("WHATSAPP_GROUP1_ID"),
        # Course #1 wrapped up 2026-05-06. Advisor stays active; no new lectures booked.
        "course_completed": True,
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
        "whatsapp_chat_id": _env("WHATSAPP_GROUP2_ID"),
        "course_completed": True,
    },
}


# ---------------------------------------------------------------------------
# Optional additional groups (Course #2 onwards) — populated entirely from
# environment variables. A group is ENABLED when both DRIVE_GROUP{N}_FOLDER_ID
# and WHATSAPP_GROUP{N}_ID are set; otherwise the entry is omitted so the
# scheduler doesn't try to register cron jobs for an unconfigured group.
#
# To onboard a new course, set on Railway:
#   GROUP{N}_NAME            — display name, e.g. "მაისის ჯგუფი #3"
#   GROUP{N}_FOLDER_NAME     — Drive folder display name
#   DRIVE_GROUP{N}_FOLDER_ID — Drive folder ID (required)
#   DRIVE_GROUP{N}_ANALYSIS_FOLDER_ID — private Drive folder for reports
#   ZOOM_GROUP{N}_MEETING_ID — optional, falls back to shared Zoom room
#   WHATSAPP_GROUP{N}_ID     — required, e.g. "120363XXX@g.us"
#   GROUP{N}_MEETING_DAYS    — comma-separated weekday integers, e.g. "2,5"
#   GROUP{N}_START_DATE      — ISO date of first lecture, e.g. "2026-05-13"
#   GROUP{N}_TOTAL_LECTURES  — optional override (defaults to TOTAL_LECTURES)
# ---------------------------------------------------------------------------


def _parse_meeting_days(raw: str) -> list[int]:
    """Parse a comma-separated weekday list into a list[int]."""
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            v = int(piece)
            if 0 <= v <= 6:
                out.append(v)
        except ValueError:
            continue
    return out


def _parse_iso_date(raw: str) -> date | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _load_optional_groups(start: int = 3, end: int = 9) -> None:
    """Pull GROUP{N} entries from env vars and merge them into GROUPS.

    Iterates 3..end inclusive. A group entry is added only when both
    DRIVE_GROUP{N}_FOLDER_ID and WHATSAPP_GROUP{N}_ID are set, since
    those are the two values without which the lecture pipeline can't run.
    """
    for n in range(start, end + 1):
        drive = _env(f"DRIVE_GROUP{n}_FOLDER_ID")
        chat = _env(f"WHATSAPP_GROUP{n}_ID")
        if not drive or not chat:
            continue
        if n in GROUPS:
            # already present (shouldn't happen for n>=3, but be safe)
            continue
        meeting_days = _parse_meeting_days(_env(f"GROUP{n}_MEETING_DAYS", ""))
        start_date_val = _parse_iso_date(_env(f"GROUP{n}_START_DATE", ""))
        if not meeting_days or start_date_val is None:
            logger.warning(
                "GROUP%d has DRIVE/WHATSAPP set but missing meeting_days "
                "or start_date — skipping", n,
            )
            continue
        GROUPS[n] = {
            "name": _env(f"GROUP{n}_NAME", f"ჯგუფი #{n}"),
            "folder_name": _env(f"GROUP{n}_FOLDER_NAME", f"AI კურსი (ჯგუფი #{n})"),
            "drive_folder_id": drive,
            "analysis_folder_id": _env(f"DRIVE_GROUP{n}_ANALYSIS_FOLDER_ID"),
            "zoom_meeting_id": _env(f"ZOOM_GROUP{n}_MEETING_ID"),
            "meeting_days": meeting_days,
            "start_date": start_date_val,
            "attendee_emails": _ATTENDEES.get(str(n), []),
            "whatsapp_chat_id": chat,
            "course_completed": _env(f"GROUP{n}_COURSE_COMPLETED", "").strip().lower()
            in ("1", "true", "yes", "on"),
        }
        logger.info(
            "Loaded optional group %d (%s): meeting_days=%s, course_completed=%s",
            n, GROUPS[n]["name"], meeting_days, GROUPS[n]["course_completed"],
        )


_load_optional_groups()


# ---------------------------------------------------------------------------
# Group iteration helpers
# ---------------------------------------------------------------------------


def iter_all_groups() -> Iterator[tuple[int, GroupConfig]]:
    """Yield (group_number, config) for every configured group."""
    for n in sorted(GROUPS.keys()):
        yield n, GROUPS[n]


def iter_active_groups() -> Iterator[tuple[int, GroupConfig]]:
    """Yield (group_number, config) only for groups still booking lectures.

    A group is active when ``course_completed`` is False or absent. This is
    the iteration the scheduler uses to decide which cron jobs to register.
    """
    for n, cfg in iter_all_groups():
        if not cfg.get("course_completed", False):
            yield n, cfg


def get_group_for_chat_id(chat_id: str) -> int | None:
    """Return group number whose ``whatsapp_chat_id`` matches, or None."""
    if not chat_id:
        return None
    for n, cfg in iter_all_groups():
        if cfg.get("whatsapp_chat_id") == chat_id:
            return n
    return None

# Holiday/cancellation dates — lectures scheduled on these dates are skipped
# Format: comma-separated ISO dates in env var, e.g., "2026-04-01,2026-04-15"
_excluded_dates_str = os.environ.get("EXCLUDED_DATES", "2026-04-10,2026-04-13")
EXCLUDED_DATES: frozenset[date] = frozenset(
    date.fromisoformat(d.strip())
    for d in _excluded_dates_str.split(",")
    if d.strip()
) if _excluded_dates_str else frozenset()

if EXCLUDED_DATES:
    logger.info("Excluded dates loaded: %s", sorted(EXCLUDED_DATES))

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

OPERATOR_EMAIL = _env("OPERATOR_EMAIL")

WEBHOOK_SECRET = _env("WEBHOOK_SECRET")
OPERATOR_WEBHOOK_SECRET = _env("OPERATOR_WEBHOOK_SECRET")
N8N_CALLBACK_URL = _env("N8N_CALLBACK_URL")

# ---------------------------------------------------------------------------
# Webhook secret security model
# ---------------------------------------------------------------------------
# Three INDEPENDENT secrets with separate threat models.  They are NEVER
# substitutable for one another — compromising one must not grant access to
# a protected surface controlled by another.
#
#  WEBHOOK_SECRET        — n8n / Zoom ingestion endpoint trust boundary.
#                          Required in all environments. Min 32 chars.
#
#  OPERATOR_WEBHOOK_SECRET — operator-only endpoints (health dashboard,
#                          manual-trigger, retry-latest, admin routes).
#                          Required in production (IS_RAILWAY=True).
#                          In local dev, absent OPERATOR_WEBHOOK_SECRET logs a
#                          WARNING and callers that do `OPERATOR_WEBHOOK_SECRET
#                          or WEBHOOK_SECRET` fall back gracefully; this
#                          preserves dev ergonomics without a security gap.
#
#  PAPERCLIP_WEBHOOK_SECRET — Paperclip orchestration platform task dispatch.
#                          Required when PAPERCLIP_API_BASE is set (integration
#                          is active). If absent, the /paperclip/task endpoint
#                          rejects ALL requests (fail-closed by design).
#
# Intentional asymmetry between dev and prod
# -------------------------------------------
# In production (IS_RAILWAY=True): all three must be set, all must be distinct,
#   all must be at least 32 chars, weak dictionary values are rejected.
# In dev (IS_RAILWAY=False): validation degrades to WARNINGs so the developer
#   can run the stack with a single minimal secret for local testing.
#
# !! DO NOT add `or WEBHOOK_SECRET` fallbacks to these assignments. !!
# ---------------------------------------------------------------------------

# --- Paperclip bridge ---
# Pure values — no fallback to WEBHOOK_SECRET.
# If unset the bridge endpoints will reject all requests (correct fail-closed).
PAPERCLIP_WEBHOOK_SECRET = _env("PAPERCLIP_WEBHOOK_SECRET")
# PAPERCLIP_OPENCLAW_SECRET: Bearer token Paperclip sends on dispatch to the
# OpenClaw / CRO gateway (`POST /query`). Rotated independently of the Training
# Ops Lead bridge secret.  No fallback — unset means the gateway rejects calls.
PAPERCLIP_OPENCLAW_SECRET = _env("PAPERCLIP_OPENCLAW_SECRET")
PAPERCLIP_API_BASE = _env("PAPERCLIP_API_BASE", "http://127.0.0.1:3100").rstrip("/")

SERVER_HOST = _env("SERVER_HOST", "0.0.0.0" if IS_RAILWAY else "127.0.0.1")
try:
    SERVER_PORT = int(_env("SERVER_PORT", _env("PORT", "5001")))
except (ValueError, TypeError):
    SERVER_PORT = 5001
SERVER_PUBLIC_URL = _env("SERVER_PUBLIC_URL")  # e.g. "https://abc123.ngrok.io"

# ---------------------------------------------------------------------------
# Startup validation — fail fast on missing critical config
# ---------------------------------------------------------------------------


_WEAK_SECRET_VALUES: frozenset[str] = frozenset({
    "test", "password", "secret", "changeme", "your-secret-here",
    "your_webhook_secret_here", "webhook_secret", "change_me",
    "example", "placeholder", "dummy", "fake", "none",
})

_MIN_SECRET_LENGTH = 32


def _check_secret_strength(
    value: str,
    name: str,
    errors: list[str],
    warnings: list[str],
    *,
    production: bool,
) -> None:
    """Validate a single secret's length and entropy.

    Appends to *errors* in production mode and to *warnings* in dev mode.
    """
    if not value:
        return  # Absence is handled by callers separately

    messages = errors if production else warnings

    if len(value) < _MIN_SECRET_LENGTH:
        messages.append(
            f"{name} is too short ({len(value)} chars); "
            f"minimum is {_MIN_SECRET_LENGTH} for cryptographic strength"
        )

    value_lower = value.lower()
    if value_lower in _WEAK_SECRET_VALUES or any(
        value_lower.startswith(weak) for weak in _WEAK_SECRET_VALUES
    ):
        messages.append(
            f"{name} matches a known weak/example value — "
            "set a randomly generated secret"
        )


def validate_critical_config() -> list[str]:
    """Check that critical environment variables are set and secrets are strong.

    Security model enforced here (see module docstring block above):
    - WEBHOOK_SECRET: always required, min 32 chars, no weak values.
    - OPERATOR_WEBHOOK_SECRET: required in production; warn in dev.
    - PAPERCLIP_WEBHOOK_SECRET: required when Paperclip integration is active.
    - All three secrets must be distinct in production.

    Returns a list of warning messages for non-fatal issues.
    Raises RuntimeError if any critical check fails in production (IS_RAILWAY=True).
    In local dev (IS_RAILWAY=False), critical checks degrade to warnings so
    the developer can run the stack with minimal config.

    NOTE for production rollout: Tornike must set OPERATOR_WEBHOOK_SECRET and
    PAPERCLIP_WEBHOOK_SECRET distinct from WEBHOOK_SECRET BEFORE deploying this
    code to Railway.  See docs/security/secret-separation-rollout.md.
    """
    warnings: list[str] = []
    errors: list[str] = []

    production = IS_RAILWAY

    # ------------------------------------------------------------------
    # 1. WEBHOOK_SECRET — always critical
    # ------------------------------------------------------------------
    if not WEBHOOK_SECRET:
        errors.append("WEBHOOK_SECRET is not set")
    else:
        _check_secret_strength(WEBHOOK_SECRET, "WEBHOOK_SECRET", errors, warnings, production=production)

    # ------------------------------------------------------------------
    # 2. OPERATOR_WEBHOOK_SECRET — required in production
    # ------------------------------------------------------------------
    if not OPERATOR_WEBHOOK_SECRET:
        if production:
            errors.append(
                "OPERATOR_WEBHOOK_SECRET is not set in production; "
                "operator/admin endpoints will reject all requests"
            )
        else:
            warnings.append(
                "OPERATOR_WEBHOOK_SECRET not set; in dev, operator endpoints "
                "fall back to WEBHOOK_SECRET (NOT safe in production)"
            )
    else:
        _check_secret_strength(
            OPERATOR_WEBHOOK_SECRET, "OPERATOR_WEBHOOK_SECRET",
            errors, warnings, production=production,
        )

    # ------------------------------------------------------------------
    # 3. PAPERCLIP_WEBHOOK_SECRET — required when integration is active
    # ------------------------------------------------------------------
    paperclip_active = bool(_env("PAPERCLIP_API_BASE") or PAPERCLIP_API_BASE != "http://127.0.0.1:3100")
    if not PAPERCLIP_WEBHOOK_SECRET:
        if paperclip_active:
            msg = (
                "PAPERCLIP_WEBHOOK_SECRET not set while Paperclip integration "
                "is active — /paperclip/task will reject all requests (fail-closed)"
            )
            if production:
                errors.append(msg)
            else:
                warnings.append(msg)
        else:
            warnings.append(
                "PAPERCLIP_WEBHOOK_SECRET not set; Paperclip endpoints will "
                "reject all requests until set"
            )
    else:
        _check_secret_strength(
            PAPERCLIP_WEBHOOK_SECRET, "PAPERCLIP_WEBHOOK_SECRET",
            errors, warnings, production=production,
        )

    # ------------------------------------------------------------------
    # 4. Secrets must be distinct from each other
    # ------------------------------------------------------------------
    set_secrets: dict[str, str] = {}
    if WEBHOOK_SECRET:
        set_secrets["WEBHOOK_SECRET"] = WEBHOOK_SECRET
    if OPERATOR_WEBHOOK_SECRET:
        set_secrets["OPERATOR_WEBHOOK_SECRET"] = OPERATOR_WEBHOOK_SECRET
    if PAPERCLIP_WEBHOOK_SECRET:
        set_secrets["PAPERCLIP_WEBHOOK_SECRET"] = PAPERCLIP_WEBHOOK_SECRET

    seen: dict[str, str] = {}  # value → first name that used it
    for name, value in set_secrets.items():
        if value in seen:
            collision_msg = (
                f"{name} is identical to {seen[value]} — "
                "each secret must be unique so that rotating one does not "
                "compromise the others"
            )
            if production:
                errors.append(collision_msg)
            else:
                warnings.append(collision_msg)
        else:
            seen[value] = name

    # ------------------------------------------------------------------
    # 5. Important but not fatal — specific features won't work
    # ------------------------------------------------------------------
    if not GEMINI_API_KEY and not GEMINI_API_KEY_PAID:
        warnings.append("No Gemini API key configured (GEMINI_API_KEY or GEMINI_API_KEY_PAID)")
    if not ANTHROPIC_API_KEY:
        warnings.append("ANTHROPIC_API_KEY not set — Claude reasoning disabled")
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        warnings.append("Green API not configured — WhatsApp notifications disabled")
    if not WHATSAPP_TORNIKE_PHONE:
        warnings.append("WHATSAPP_TORNIKE_PHONE not set — operator alerts disabled")

    # ------------------------------------------------------------------
    # 6. HIGH-risk missing vars — warn at startup
    # ------------------------------------------------------------------
    _warn_vars: list[tuple[str, str]] = [
        ("ZOOM_WEBHOOK_SECRET_TOKEN", "Zoom webhooks will return 503"),
    ]
    for _n, _cfg in iter_all_groups():
        _group_name = _cfg["name"]
        _warn_vars.append((
            f"DRIVE_GROUP{_n}_FOLDER_ID",
            f"{_group_name} Drive uploads will fail",
        ))
        _warn_vars.append((
            f"DRIVE_GROUP{_n}_ANALYSIS_FOLDER_ID",
            f"{_group_name} analysis reports won't upload",
        ))
        _warn_vars.append((
            f"WHATSAPP_GROUP{_n}_ID",
            f"{_group_name} WhatsApp notifications will fail",
        ))
    optional_group_keys = (
        "NAME",
        "FOLDER_NAME",
        "MEETING_DAYS",
        "START_DATE",
        "COURSE_COMPLETED",
    )
    for n in range(3, 10):
        if n in GROUPS:
            continue
        configured_keys = [
            f"DRIVE_GROUP{n}_FOLDER_ID",
            f"DRIVE_GROUP{n}_ANALYSIS_FOLDER_ID",
            f"WHATSAPP_GROUP{n}_ID",
            f"ZOOM_GROUP{n}_MEETING_ID",
            *(f"GROUP{n}_{suffix}" for suffix in optional_group_keys),
        ]
        present = [key for key in configured_keys if os.environ.get(key)]
        if present:
            missing_required = [
                key
                for key in (
                    f"DRIVE_GROUP{n}_FOLDER_ID",
                    f"WHATSAPP_GROUP{n}_ID",
                    f"GROUP{n}_MEETING_DAYS",
                    f"GROUP{n}_START_DATE",
                )
                if not os.environ.get(key)
            ]
            warnings.append(
                f"GROUP{n} is partially configured but disabled; "
                f"present={present}; missing_required={missing_required}"
            )

    for var_name, consequence in _warn_vars:
        if not os.environ.get(var_name):
            logger.warning("Missing %s — %s", var_name, consequence)
            warnings.append(f"Missing {var_name} — {consequence}")

    # ------------------------------------------------------------------
    # Log everything and fail fast if production has errors
    # ------------------------------------------------------------------
    for w in warnings:
        logger.warning("Config: %s", w)
    for e in errors:
        logger.error("Config security error: %s", e)

    if production and errors:
        raise RuntimeError(
            f"Critical config errors in production ({len(errors)}): "
            + "; ".join(errors)
        )
    elif errors:
        # Dev mode: errors are demoted to warnings in the return value
        for e in errors:
            logger.warning("Config: %s (OK for local dev)", e)
        warnings.extend(errors)

    return warnings


# Validation is now called explicitly by orchestrator.py at startup.
# Removed auto-execution to prevent import-time side effects in tests.

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
GEMINI_MODEL_TRANSCRIPTION = "gemini-2.5-flash-lite"  # 2.5-flash hit persistent 503 high-demand 2026-04-16 — switched to lite (different capacity pool)
GEMINI_MODEL_ANALYSIS = os.environ.get("GEMINI_MODEL_ANALYSIS_OVERRIDE") or "gemini-3.1-pro-preview"  # env override for quota-exhaustion fallback; default smartest for Georgian text writing

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
ASSISTANT_CLAUDE_MODEL = "claude-sonnet-4-6"  # Was opus — Sonnet saves ~$150/course with minimal quality loss
GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"

# Pinecone relevance score thresholds (cosine similarity).
# Georgian text similarity scores are naturally lower than English.
PINECONE_SCORE_THRESHOLD_DIRECT = float(
    _env("PINECONE_SCORE_THRESHOLD_DIRECT", "0.3")
)
PINECONE_SCORE_THRESHOLD_PASSIVE = float(
    _env("PINECONE_SCORE_THRESHOLD_PASSIVE", "0.4")
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_lecture_number(group_number: int, for_date: date | None = None) -> int:
    """Calculate which lecture number falls on the given date.

    Counts the number of meeting days from the group's start date
    up to and including ``for_date``.
    """
    if for_date is None:
        from datetime import datetime
        for_date = datetime.now(TBILISI_TZ).date()

    group = GROUPS[group_number]
    start = group["start_date"]
    meeting_days = group["meeting_days"]

    if for_date < start:
        return 0

    count = 0
    current = start
    while current <= for_date:
        if current.weekday() in meeting_days and current not in EXCLUDED_DATES:
            count += 1
            if count >= TOTAL_LECTURES:
                break
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

    Two markers are checked, in priority order:

    1. **Cohort-prefixed name** (e.g. ``მაისის ჯგუფი #1``) — matched against
       each group's configured ``name`` field. This handles topics written for
       the user-facing convention where each cohort restarts numbering at #1.
       More specific match wins (longest ``name`` first) so that
       ``მაისის ჯგუფი #1`` doesn't accidentally match a topic that only says
       ``ჯგუფი #1``.

    2. **Short marker** (``ჯგუფი #N`` where N is the internal group index) —
       legacy fallback for early March topics that were created before the
       cohort-prefixed convention.

    Returns:
        Internal group number (1, 2, 3, ...) if found, None otherwise.
    """
    if not isinstance(topic, str) or not topic.strip():
        logger.warning("extract_group_from_topic called with invalid topic: %r", topic)
        return None

    # 1. Direct match of the configured GROUPS[n].name as a substring
    #    (handles cohort-prefixed form "მაისის ჯგუფი #1"). Sort by length
    #    descending so the longest (most specific) name wins.
    import re

    by_name = sorted(
        ((g_num, cfg.get("name", "")) for g_num, cfg in GROUPS.items() if cfg.get("name")),
        key=lambda kv: len(kv[1]),
        reverse=True,
    )
    for group_num, name in by_name:
        if name and name in topic:
            return group_num

    # 2. Cohort-marker + short form: handles operator-style topics like
    #    "AI კურსი — ჯგუფი #1 (მაისი)" where the cohort label sits in
    #    parentheses or anywhere outside the group prefix. We extract:
    #      • a Georgian month-stem token ending in -ის (e.g. "მაისის")
    #        OR the same word without the -ის suffix in parentheses
    #      • a group number from "ჯგუფი #N"
    #    Then match against a GROUPS[i] whose ``name`` starts with that
    #    month and ends with "ჯგუფი #N".
    short_m = re.search(r"ჯგუფი\s*#?\s*(\d+)", topic)
    cohort_m = re.search(
        r"\(\s*(მარტი|მაისი|ივნისი|ივლისი|აგვისტო|სექტემბერი|ოქტომბერი|"
        r"ნოემბერი|დეკემბერი|იანვარი|თებერვალი|აპრილი)(?:ს)?\s*\)",
        topic,
    )
    if short_m and cohort_m:
        sub_num = int(short_m.group(1))
        # Convert captured nominative form ("მაისი") to genitive ("მაისის")
        # by appending "ს" — Georgian month names all end in -ი or -ო so
        # adding -ს yields the correct genitive form for matching GROUPS.name
        month_stem = cohort_m.group(1)
        month_genitive = month_stem if month_stem.endswith("ის") else month_stem + "ს"
        candidate = f"{month_genitive} ჯგუფი #{sub_num}"
        for g_num, gcfg in GROUPS.items():
            if gcfg.get("name") == candidate:
                return g_num

    # 3. Legacy short marker "ჯგუფი #N" — only kicks in when no cohort label
    #    is present at all. Old March topics (#1, #2) still resolve correctly.
    for group_num in sorted(GROUPS.keys()):
        if f"ჯგუფი #{group_num}" in topic:
            return group_num

    logger.debug("No group marker found in topic: %s", topic[:80])
    return None


def get_drive_file_url(file_id: str, is_doc: bool = False) -> str:
    """Build a shareable Google Drive/Docs URL."""
    if is_doc:
        return f"https://docs.google.com/document/d/{file_id}/edit"
    return f"https://drive.google.com/file/d/{file_id}/view"
