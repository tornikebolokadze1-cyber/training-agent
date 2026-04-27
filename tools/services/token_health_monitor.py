"""Proactive Google OAuth token health monitoring.

Runs as a daily cron job to detect token issues BEFORE they cause
pipeline outages. Previously, the system only noticed token revocation
when a pipeline tried to use Drive (often hours after the lecture ended).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

WARNING_HOURS = 7 * 24  # 7 days
CRITICAL_HOURS = 1 * 24  # 1 day


def check_token_proactively() -> dict[str, Any]:
    """Check Google OAuth token health and take action if needed.

    Returns:
        Dict with keys:
            - status: "healthy" | "warning" | "critical"
            - days_remaining: int | None
            - action_taken: str
            - alert_sent: bool
    """
    from tools.core.token_manager import check_token_health, refresh_google_token
    from tools.integrations.whatsapp_sender import alert_operator

    result: dict[str, Any] = {
        "status": "healthy",
        "days_remaining": None,
        "action_taken": "none",
        "alert_sent": False,
    }

    try:
        health = check_token_health()
    except Exception as exc:
        logger.error("Failed to check token health: %s", exc)
        result["status"] = "critical"
        result["action_taken"] = f"check failed: {exc}"
        try:
            alert_operator(
                f"CRITICAL: Proactive token check raised an exception: {exc}"
            )
            result["alert_sent"] = True
        except Exception as alert_exc:
            logger.critical("Could not alert operator: %s", alert_exc)
        return result

    # Case 1: token already revoked / unusable
    if not health.get("valid") or not health.get("has_refresh_token"):
        error = health.get("error") or "token invalid"
        logger.critical("[token-monitor] Token is unusable: %s", error)
        result["status"] = "critical"
        result["action_taken"] = f"token revoked or invalid: {error}"
        try:
            alert_operator(
                "CRITICAL: Google OAuth token is REVOKED or INVALID.\n\n"
                f"Detail: {error}\n\n"
                "Pipeline will fail on Drive operations. Re-authorize now:\n"
                "  python -m tools.core.token_manager --reauth"
            )
            result["alert_sent"] = True
        except Exception as exc:
            logger.critical("Could not alert operator about revocation: %s", exc)
        return result

    expires_in_hours = health.get("expires_in_hours")
    if expires_in_hours is None:
        logger.warning("[token-monitor] Token has no expiry info; skipping")
        result["action_taken"] = "no expiry info"
        return result

    days_remaining = int(expires_in_hours // 24)
    result["days_remaining"] = days_remaining

    # Case 2: plenty of runway
    if expires_in_hours > WARNING_HOURS:
        logger.info(
            "[token-monitor] Token healthy (%.1fh / %d days remaining)",
            expires_in_hours,
            days_remaining,
        )
        result["action_taken"] = "none (healthy)"
        return result

    # Case 3: critical — < 1 day remaining, attempt refresh + alert
    if expires_in_hours < CRITICAL_HOURS:
        logger.critical(
            "[token-monitor] Token expires in <1 day (%.1fh); refreshing + alerting",
            expires_in_hours,
        )
        refreshed = False
        try:
            refreshed = refresh_google_token()
        except Exception as exc:
            logger.error("Refresh raised: %s", exc)

        result["status"] = "critical"
        result["action_taken"] = (
            "refresh succeeded" if refreshed else "refresh FAILED"
        )
        try:
            alert_operator(
                f"CRITICAL: Google token expires in <1 day ({expires_in_hours:.1f}h).\n"
                f"Refresh attempt: {'OK' if refreshed else 'FAILED'}.\n"
                "If failed, re-authorize immediately:\n"
                "  python -m tools.core.token_manager --reauth"
            )
            result["alert_sent"] = True
        except Exception as exc:
            logger.critical("Could not alert operator: %s", exc)
        return result

    # Case 4: warning — 1-7 days, try proactive refresh (no alert unless failed)
    logger.warning(
        "[token-monitor] Token expires in %d days; attempting proactive refresh",
        days_remaining,
    )
    try:
        refreshed = refresh_google_token()
    except Exception as exc:
        logger.error("Proactive refresh raised: %s", exc)
        refreshed = False

    if refreshed:
        result["status"] = "healthy"
        result["action_taken"] = "proactive refresh succeeded"
    else:
        result["status"] = "warning"
        result["action_taken"] = "proactive refresh FAILED"
        try:
            alert_operator(
                f"WARNING: Google token expires in {days_remaining} days and "
                "proactive refresh FAILED. Please re-authorize soon:\n"
                "  python -m tools.core.token_manager --reauth"
            )
            result["alert_sent"] = True
        except Exception as exc:
            logger.critical("Could not alert operator: %s", exc)
    return result


def register_proactive_token_jobs(scheduler) -> None:
    """Register the daily proactive token check with APScheduler.

    Scheduled at 06:00 Tbilisi time (well before the 18:00 pre-meeting
    reminders), so any token issues are caught with hours to spare.
    """
    from apscheduler.triggers.cron import CronTrigger

    from tools.core.config import TBILISI_TZ

    scheduler.add_job(
        check_token_proactively,
        trigger=CronTrigger(hour=6, minute=0, timezone=TBILISI_TZ),
        id="proactive_token_check",
        replace_existing=True,
    )
    logger.info("Registered proactive token check (daily 06:00 Tbilisi)")
