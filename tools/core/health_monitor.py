"""Proactive health monitoring for the Training Agent system.

Runs periodic checks on all external dependencies and internal state,
alerting the operator BEFORE failures happen rather than after. Each
check returns a structured result with severity (OK / WARNING / CRITICAL)
and an actionable message.

Integration points:
  - Scheduler: ``_health_check_job`` runs every 30 minutes.
  - Server: ``/health`` endpoint returns ``HealthMonitor.check_all()``.
  - Daily report: 09:00 Tbilisi summary via WhatsApp.
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from tools.core.config import (
    ANTHROPIC_API_KEY,
    GEMINI_API_KEY,
    GEMINI_API_KEY_PAID,
    GREEN_API_INSTANCE_ID,
    GREEN_API_TOKEN,
    GROUPS,
    PINECONE_API_KEY,
    PINECONE_INDEX_NAME,
    TBILISI_TZ,
    TMP_DIR,
    TOTAL_LECTURES,
    ZOOM_ACCOUNT_ID,
    ZOOM_CLIENT_ID,
    ZOOM_CLIENT_SECRET,
    get_lecture_number,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    """Health check severity levels."""

    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class CheckResult:
    """Immutable result of a single health check."""

    name: str
    severity: Severity
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "severity": self.severity.value,
            "message": self.message,
            "details": self.details,
        }


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

GOOGLE_TOKEN_WARNING_HOURS = 48
GOOGLE_TOKEN_CRITICAL_HOURS = 6
DISK_WARNING_GB = 5.0
DISK_CRITICAL_GB = 2.0
API_ERROR_WARNING_MINUTES = 30
PIPELINE_STUCK_WARNING_HOURS = 2
LECTURE_OVERDUE_CRITICAL_HOURS = 4


# ---------------------------------------------------------------------------
# Internal state — tracks consecutive API failures
# ---------------------------------------------------------------------------

_api_error_timestamps: dict[str, datetime] = {}


def record_api_error(service_name: str) -> None:
    """Record the first failure time for a service (called by checks)."""
    if service_name not in _api_error_timestamps:
        _api_error_timestamps[service_name] = datetime.now(TBILISI_TZ)


def clear_api_error(service_name: str) -> None:
    """Clear failure tracking when a service recovers."""
    _api_error_timestamps.pop(service_name, None)


def get_api_error_duration_minutes(service_name: str) -> float:
    """Return how many minutes a service has been failing, or 0 if healthy."""
    first_failure = _api_error_timestamps.get(service_name)
    if first_failure is None:
        return 0.0
    elapsed = datetime.now(TBILISI_TZ) - first_failure
    return elapsed.total_seconds() / 60


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_google_token() -> CheckResult:
    """Check Google OAuth2 token expiry and refresh status.

    Uses gdrive_manager._get_credentials() to load the token from disk
    (or base64 env var on Railway) and inspect its expiry.
    """
    try:
        from tools.integrations.gdrive_manager import _get_credentials

        creds = _get_credentials()
        if creds is None:
            return CheckResult(
                name="google_token",
                severity=Severity.CRITICAL,
                message="Google OAuth token not found. Re-authenticate.",
            )

        if not hasattr(creds, "expiry") or creds.expiry is None:
            # Token is valid but has no expiry (e.g. service account)
            return CheckResult(
                name="google_token",
                severity=Severity.OK,
                message="Google credentials loaded (no expiry — likely service account).",
            )

        now = datetime.utcnow()
        remaining = creds.expiry - now
        remaining_hours = remaining.total_seconds() / 3600

        if remaining_hours < GOOGLE_TOKEN_CRITICAL_HOURS:
            return CheckResult(
                name="google_token",
                severity=Severity.CRITICAL,
                message=(
                    f"Google token expires in {remaining_hours:.1f}h. "
                    "Refresh immediately or re-authenticate."
                ),
                details={"expires_in_hours": round(remaining_hours, 1)},
            )

        if remaining_hours < GOOGLE_TOKEN_WARNING_HOURS:
            return CheckResult(
                name="google_token",
                severity=Severity.WARNING,
                message=f"Google token expires in {remaining_hours:.1f}h.",
                details={"expires_in_hours": round(remaining_hours, 1)},
            )

        clear_api_error("google_token")
        return CheckResult(
            name="google_token",
            severity=Severity.OK,
            message=f"Google token valid for {remaining_hours:.0f}h.",
            details={"expires_in_hours": round(remaining_hours, 1)},
        )

    except Exception as exc:
        record_api_error("google_token")
        return CheckResult(
            name="google_token",
            severity=Severity.WARNING,
            message=f"Cannot check Google token: {exc}",
        )


def check_zoom_auth() -> CheckResult:
    """Test Zoom S2S OAuth with a lightweight API call."""
    if not all((ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET)):
        return CheckResult(
            name="zoom_auth",
            severity=Severity.CRITICAL,
            message="Zoom credentials not configured.",
        )

    try:
        from tools.integrations.zoom_manager import get_access_token

        token = get_access_token()
        if token:
            clear_api_error("zoom_auth")
            return CheckResult(
                name="zoom_auth",
                severity=Severity.OK,
                message="Zoom S2S OAuth token acquired successfully.",
            )
        record_api_error("zoom_auth")
        return CheckResult(
            name="zoom_auth",
            severity=Severity.CRITICAL,
            message="Zoom token request returned empty.",
        )
    except Exception as exc:
        record_api_error("zoom_auth")
        duration = get_api_error_duration_minutes("zoom_auth")
        severity = (
            Severity.CRITICAL
            if duration > API_ERROR_WARNING_MINUTES
            else Severity.WARNING
        )
        return CheckResult(
            name="zoom_auth",
            severity=severity,
            message=f"Zoom auth failed: {exc}",
            details={"failing_for_minutes": round(duration, 1)},
        )


def check_gemini_quota() -> CheckResult:
    """Test Gemini API with a tiny generation call."""
    api_key = GEMINI_API_KEY_PAID or GEMINI_API_KEY
    if not api_key:
        return CheckResult(
            name="gemini_api",
            severity=Severity.CRITICAL,
            message="No Gemini API key configured.",
        )

    try:
        from google.genai import Client

        client = Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents="Say OK",
            config={"max_output_tokens": 5},
        )
        if response and response.text:
            clear_api_error("gemini_api")
            return CheckResult(
                name="gemini_api",
                severity=Severity.OK,
                message="Gemini API responding normally.",
            )
        record_api_error("gemini_api")
        return CheckResult(
            name="gemini_api",
            severity=Severity.WARNING,
            message="Gemini returned empty response.",
        )
    except Exception as exc:
        record_api_error("gemini_api")
        duration = get_api_error_duration_minutes("gemini_api")
        severity = (
            Severity.CRITICAL
            if duration > API_ERROR_WARNING_MINUTES
            else Severity.WARNING
        )
        return CheckResult(
            name="gemini_api",
            severity=severity,
            message=f"Gemini API error: {exc}",
            details={"failing_for_minutes": round(duration, 1)},
        )


def check_claude_api() -> CheckResult:
    """Test Claude/Anthropic API with a simple message."""
    if not ANTHROPIC_API_KEY:
        return CheckResult(
            name="claude_api",
            severity=Severity.CRITICAL,
            message="ANTHROPIC_API_KEY not configured.",
        )

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=5,
            messages=[{"role": "user", "content": "Say OK"}],
        )
        if response and response.content:
            clear_api_error("claude_api")
            return CheckResult(
                name="claude_api",
                severity=Severity.OK,
                message="Claude API responding normally.",
            )
        record_api_error("claude_api")
        return CheckResult(
            name="claude_api",
            severity=Severity.WARNING,
            message="Claude returned empty response.",
        )
    except Exception as exc:
        record_api_error("claude_api")
        duration = get_api_error_duration_minutes("claude_api")
        severity = (
            Severity.CRITICAL
            if duration > API_ERROR_WARNING_MINUTES
            else Severity.WARNING
        )
        return CheckResult(
            name="claude_api",
            severity=severity,
            message=f"Claude API error: {exc}",
            details={"failing_for_minutes": round(duration, 1)},
        )


def check_whatsapp() -> CheckResult:
    """Check Green API WhatsApp connection health."""
    if not GREEN_API_INSTANCE_ID or not GREEN_API_TOKEN:
        return CheckResult(
            name="whatsapp",
            severity=Severity.WARNING,
            message="Green API not configured — WhatsApp notifications disabled.",
        )

    try:
        import httpx

        url = (
            f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE_ID}"
            f"/getStateInstance/{GREEN_API_TOKEN}"
        )
        with httpx.Client(timeout=15) as client:
            response = client.get(url)

        if response.status_code == 200:
            state = response.json().get("stateInstance", "unknown")
            if state == "authorized":
                clear_api_error("whatsapp")
                return CheckResult(
                    name="whatsapp",
                    severity=Severity.OK,
                    message="WhatsApp connected and authorized.",
                    details={"state": state},
                )
            return CheckResult(
                name="whatsapp",
                severity=Severity.WARNING,
                message=f"WhatsApp state: {state}. May need QR re-scan.",
                details={"state": state},
            )

        record_api_error("whatsapp")
        return CheckResult(
            name="whatsapp",
            severity=Severity.WARNING,
            message=f"Green API returned HTTP {response.status_code}.",
        )
    except Exception as exc:
        record_api_error("whatsapp")
        return CheckResult(
            name="whatsapp",
            severity=Severity.WARNING,
            message=f"WhatsApp health check failed: {exc}",
        )


def check_pinecone() -> CheckResult:
    """Check Pinecone vector DB connectivity via index stats."""
    if not PINECONE_API_KEY:
        return CheckResult(
            name="pinecone",
            severity=Severity.WARNING,
            message="PINECONE_API_KEY not configured.",
        )

    try:
        from pinecone import Pinecone

        pc = Pinecone(api_key=PINECONE_API_KEY)
        index = pc.Index(PINECONE_INDEX_NAME)
        stats = index.describe_index_stats()
        total_vectors = stats.get("total_vector_count", 0)
        clear_api_error("pinecone")
        return CheckResult(
            name="pinecone",
            severity=Severity.OK,
            message=f"Pinecone online — {total_vectors} vectors indexed.",
            details={"total_vectors": total_vectors},
        )
    except Exception as exc:
        record_api_error("pinecone")
        duration = get_api_error_duration_minutes("pinecone")
        severity = (
            Severity.CRITICAL
            if duration > API_ERROR_WARNING_MINUTES
            else Severity.WARNING
        )
        return CheckResult(
            name="pinecone",
            severity=severity,
            message=f"Pinecone error: {exc}",
            details={"failing_for_minutes": round(duration, 1)},
        )


def check_disk_space() -> CheckResult:
    """Check free disk space in the .tmp/ directory."""
    try:
        usage = shutil.disk_usage(str(TMP_DIR))
        free_gb = usage.free / (1024**3)

        if free_gb < DISK_CRITICAL_GB:
            return CheckResult(
                name="disk_space",
                severity=Severity.CRITICAL,
                message=f"Disk space critically low: {free_gb:.1f} GB free.",
                details={"free_gb": round(free_gb, 1)},
            )

        if free_gb < DISK_WARNING_GB:
            return CheckResult(
                name="disk_space",
                severity=Severity.WARNING,
                message=f"Disk space low: {free_gb:.1f} GB free.",
                details={"free_gb": round(free_gb, 1)},
            )

        return CheckResult(
            name="disk_space",
            severity=Severity.OK,
            message=f"Disk space OK: {free_gb:.1f} GB free.",
            details={"free_gb": round(free_gb, 1)},
        )
    except Exception as exc:
        return CheckResult(
            name="disk_space",
            severity=Severity.WARNING,
            message=f"Cannot check disk space: {exc}",
        )


def check_pending_lectures() -> CheckResult:
    """Check for lectures that should have been processed but were not.

    Looks at today's schedule and verifies that any lecture whose meeting
    ended more than LECTURE_OVERDUE_CRITICAL_HOURS ago has been processed
    (either a pipeline state file exists or vectors are in Pinecone).
    """
    now = datetime.now(TBILISI_TZ)
    today = now.date()
    issues: list[str] = []

    for group_num in GROUPS:
        group = GROUPS[group_num]
        if today.weekday() not in group["meeting_days"]:
            continue

        # Meeting ends at 22:00 — check only if we are past the overdue window
        meeting_end = now.replace(hour=22, minute=0, second=0, microsecond=0)
        overdue_threshold = meeting_end + timedelta(hours=LECTURE_OVERDUE_CRITICAL_HOURS)

        if now < overdue_threshold:
            continue  # Not yet overdue

        lecture_num = get_lecture_number(group_num, for_date=today)
        if lecture_num == 0 or lecture_num > TOTAL_LECTURES:
            continue

        # Check if pipeline ran for this lecture
        try:
            from tools.core.pipeline_state import is_pipeline_done

            if is_pipeline_done(group_num, lecture_num):
                continue
        except Exception:
            pass

        issues.append(
            f"Group {group_num}, Lecture #{lecture_num} — not processed "
            f"(meeting ended {(now - meeting_end).total_seconds() / 3600:.1f}h ago)"
        )

    if issues:
        return CheckResult(
            name="pending_lectures",
            severity=Severity.CRITICAL,
            message=f"{len(issues)} lecture(s) overdue: {'; '.join(issues)}",
            details={"overdue_lectures": issues},
        )

    return CheckResult(
        name="pending_lectures",
        severity=Severity.OK,
        message="No overdue lectures.",
    )


def check_stuck_pipelines() -> CheckResult:
    """Check for pipelines stuck in the same state for too long."""
    try:
        from tools.core.pipeline_state import list_active_pipelines

        active = list_active_pipelines()
        stuck: list[str] = []
        now = datetime.now(TBILISI_TZ)

        for pipeline in active:
            try:
                started = datetime.fromisoformat(pipeline.started_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=TBILISI_TZ)
                elapsed_hours = (now - started).total_seconds() / 3600
                if elapsed_hours > PIPELINE_STUCK_WARNING_HOURS:
                    stuck.append(
                        f"G{pipeline.group} L{pipeline.lecture} "
                        f"in '{pipeline.status}' for {elapsed_hours:.1f}h"
                    )
            except (ValueError, TypeError):
                continue

        if stuck:
            return CheckResult(
                name="stuck_pipelines",
                severity=Severity.WARNING,
                message=f"{len(stuck)} pipeline(s) may be stuck: {'; '.join(stuck)}",
                details={"stuck_pipelines": stuck},
            )

        return CheckResult(
            name="stuck_pipelines",
            severity=Severity.OK,
            message=f"No stuck pipelines ({len(active)} active).",
            details={"active_count": len(active)},
        )
    except Exception as exc:
        return CheckResult(
            name="stuck_pipelines",
            severity=Severity.WARNING,
            message=f"Cannot check pipeline state: {exc}",
        )


# ---------------------------------------------------------------------------
# Aggregate check
# ---------------------------------------------------------------------------


def check_all() -> dict[str, Any]:
    """Run all health checks and return a structured report.

    Returns:
        Dict with keys: overall_status, timestamp, checks (list of dicts),
        warnings_count, critical_count.
    """
    checks = [
        check_disk_space(),
        check_whatsapp(),
        check_pinecone(),
        check_pending_lectures(),
        check_stuck_pipelines(),
    ]

    # Expensive API checks — only run if keys are configured
    if ZOOM_ACCOUNT_ID and ZOOM_CLIENT_ID and ZOOM_CLIENT_SECRET:
        checks.append(check_zoom_auth())

    if GEMINI_API_KEY or GEMINI_API_KEY_PAID:
        checks.append(check_gemini_quota())

    if ANTHROPIC_API_KEY:
        checks.append(check_claude_api())

    checks.append(check_google_token())

    warnings = sum(1 for c in checks if c.severity == Severity.WARNING)
    criticals = sum(1 for c in checks if c.severity == Severity.CRITICAL)

    if criticals > 0:
        overall = "critical"
    elif warnings > 0:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "overall_status": overall,
        "timestamp": datetime.now(TBILISI_TZ).isoformat(),
        "checks": [c.to_dict() for c in checks],
        "warnings_count": warnings,
        "critical_count": criticals,
    }


# ---------------------------------------------------------------------------
# Scheduled job — runs every 30 minutes
# ---------------------------------------------------------------------------


def run_health_check_job() -> None:
    """Run all checks and alert operator on WARNING/CRITICAL findings.

    Designed to be called from APScheduler (blocking, in thread executor).
    """
    logger.info("[health] Running scheduled health check...")
    start = time.monotonic()

    try:
        report = check_all()
    except Exception as exc:
        logger.error("[health] Health check failed: %s", exc)
        try:
            from tools.integrations.whatsapp_sender import alert_operator

            alert_operator(f"Health check itself failed: {exc}")
        except Exception:
            pass
        return

    elapsed = time.monotonic() - start
    logger.info(
        "[health] Check complete in %.1fs — %s (%d warnings, %d critical)",
        elapsed,
        report["overall_status"],
        report["warnings_count"],
        report["critical_count"],
    )

    if report["critical_count"] > 0 or report["warnings_count"] > 0:
        _send_health_alert(report)


def _send_health_alert(report: dict[str, Any]) -> None:
    """Format and send a WhatsApp alert for unhealthy checks."""
    try:
        from tools.integrations.whatsapp_sender import alert_operator

        lines: list[str] = []
        for check in report["checks"]:
            if check["severity"] in ("warning", "critical"):
                icon = "🔴" if check["severity"] == "critical" else "🟡"
                lines.append(f"{icon} {check['name']}: {check['message']}")

        if not lines:
            return

        message = (
            f"System Health: {report['overall_status'].upper()}\n"
            f"{'─' * 30}\n"
            + "\n".join(lines)
        )
        alert_operator(message)
    except Exception as exc:
        logger.error("[health] Failed to send health alert: %s", exc)


# ---------------------------------------------------------------------------
# Daily morning report — 09:00 Tbilisi time
# ---------------------------------------------------------------------------


def run_daily_morning_report() -> None:
    """Send a daily summary to the operator at 09:00 Tbilisi time.

    Includes: system status, yesterday's processed lectures,
    today's upcoming lectures, any pending retries.
    """
    logger.info("[health] Generating daily morning report...")

    try:
        report = check_all()
    except Exception as exc:
        logger.error("[health] Daily report check_all failed: %s", exc)
        return

    now = datetime.now(TBILISI_TZ)
    today = now.date()
    yesterday = today - timedelta(days=1)

    # --- Yesterday's lectures ---
    yesterday_lectures: list[str] = []
    for group_num in GROUPS:
        group = GROUPS[group_num]
        if yesterday.weekday() in group["meeting_days"]:
            lecture_num = get_lecture_number(group_num, for_date=yesterday)
            if 0 < lecture_num <= TOTAL_LECTURES:
                status = "✅"
                try:
                    from tools.core.pipeline_state import is_pipeline_done

                    if not is_pipeline_done(group_num, lecture_num):
                        status = "❌ NOT PROCESSED"
                except Exception:
                    status = "❓ unknown"
                yesterday_lectures.append(
                    f"  ჯგუფი {group_num}, ლექცია #{lecture_num}: {status}"
                )

    # --- Today's schedule ---
    today_lectures: list[str] = []
    for group_num in GROUPS:
        group = GROUPS[group_num]
        if today.weekday() in group["meeting_days"]:
            lecture_num = get_lecture_number(group_num, for_date=today)
            if 0 < lecture_num <= TOTAL_LECTURES:
                today_lectures.append(
                    f"  ჯგუფი {group_num}, ლექცია #{lecture_num} — 20:00"
                )

    # --- Pending retries / stuck pipelines ---
    pending_info: list[str] = []
    try:
        from tools.core.pipeline_state import list_active_pipelines

        active = list_active_pipelines()
        for p in active:
            pending_info.append(f"  G{p.group} L{p.lecture}: {p.status}")
    except Exception:
        pass

    # --- Format message ---
    status_icon = {"healthy": "✅", "degraded": "🟡", "critical": "🔴"}.get(
        report["overall_status"], "❓"
    )

    lines = [
        f"📊 დილის რეპორტი — {today.isoformat()}",
        f"{'─' * 30}",
        f"სისტემა: {status_icon} {report['overall_status'].upper()}",
    ]

    # Add problem details if not healthy
    if report["overall_status"] != "healthy":
        for check in report["checks"]:
            if check["severity"] in ("warning", "critical"):
                icon = "🔴" if check["severity"] == "critical" else "🟡"
                lines.append(f"  {icon} {check['name']}: {check['message']}")

    if yesterday_lectures:
        lines.append(f"\n📚 გუშინ ({yesterday.isoformat()}):")
        lines.extend(yesterday_lectures)
    else:
        lines.append("\nგუშინ ლექცია არ იყო.")

    if today_lectures:
        lines.append(f"\n📅 დღეს ({today.isoformat()}):")
        lines.extend(today_lectures)
    else:
        lines.append("\nდღეს ლექცია არ არის.")

    if pending_info:
        lines.append("\n⏳ აქტიური pipeline-ები:")
        lines.extend(pending_info)

    message = "\n".join(lines)

    try:
        from tools.integrations.whatsapp_sender import send_private_report

        send_private_report(message)
        logger.info("[health] Daily morning report sent.")
    except Exception as exc:
        logger.error("[health] Failed to send morning report: %s", exc)
        try:
            from tools.integrations.whatsapp_sender import alert_operator

            alert_operator(f"Daily report send failed: {exc}")
        except Exception:
            pass
