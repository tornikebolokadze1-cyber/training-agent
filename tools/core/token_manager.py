"""Google OAuth token lifecycle manager.

Proactively refreshes Google OAuth tokens so they NEVER expire.
Google Cloud apps in "Testing" mode revoke refresh tokens after 7 days,
and access tokens expire every ~60 minutes.  This module ensures:

  - Access tokens are refreshed well before expiry (1-hour threshold).
  - A cron job refreshes every 6 hours as a safety net.
  - The /health endpoint reports token health.
  - If the refresh_token itself is revoked, the operator is alerted
    immediately with instructions to re-authorize.

Usage:
    from tools.core.token_manager import ensure_fresh_token

    # Call before any Drive/Docs operation:
    ensure_fresh_token()

CLI:
    python -m tools.core.token_manager          # check token health
    python -m tools.core.token_manager --reauth  # interactive re-authorization
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.core.config import (
    IS_RAILWAY,
    PROJECT_ROOT,
    _decode_b64_env,
    _materialize_credential_file,
    get_google_credentials_path,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TOKEN_PATH = PROJECT_ROOT / "token.json"
REFRESH_THRESHOLD_SECONDS = 3600      # Force-refresh if < 1 hour remaining
HEALTH_WARNING_HOURS = 48             # Mark health "degraded" if < 48h

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/docs",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_token_path() -> Path:
    """Resolve the token.json path (from env var or local file)."""
    return _materialize_credential_file("GOOGLE_TOKEN_JSON_B64", TOKEN_PATH)


def _load_credentials():
    """Load Google OAuth2 Credentials from token.json.

    Returns:
        A google.oauth2.credentials.Credentials instance, or None.
    """
    from google.oauth2.credentials import Credentials

    token_path = _get_token_path()
    if not token_path.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as exc:
        logger.error("Failed to load credentials from %s: %s", token_path, exc)
        return None


def _save_token_to_disk(creds) -> None:
    """Write refreshed credentials back to token.json on disk."""
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    TOKEN_PATH.chmod(0o600)
    logger.info("Token saved to disk: %s", TOKEN_PATH)


def _update_railway_env_var(creds) -> None:
    """Update GOOGLE_TOKEN_JSON_B64 on Railway via CLI.

    This ensures the refreshed token survives Railway redeploys.
    Non-fatal: logs a warning on failure.
    """
    try:
        token_json = creds.to_json()
        b64_value = base64.b64encode(token_json.encode("utf-8")).decode("ascii")
        result = subprocess.run(
            ["railway", "variables", "set", f"GOOGLE_TOKEN_JSON_B64={b64_value}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("Railway env var GOOGLE_TOKEN_JSON_B64 updated successfully")
        else:
            logger.warning(
                "Railway CLI returned non-zero (%d): %s",
                result.returncode,
                result.stderr[:200],
            )
    except FileNotFoundError:
        logger.warning(
            "Railway CLI not found — cannot update env var. "
            "Token will be refreshed in memory only."
        )
    except Exception as exc:
        logger.warning("Failed to update Railway env var: %s", exc)


def _invalidate_gdrive_service_cache() -> None:
    """Clear the cached Drive/Docs service objects so they pick up new creds.

    The gdrive_manager module caches service objects at module level.
    After a token refresh, those cached objects hold stale credentials.
    """
    try:
        import tools.integrations.gdrive_manager as gdm
        gdm._drive_service_cache = None
        gdm._docs_service_cache = None
        gdm._token_path_cache = None
        logger.debug("Cleared gdrive_manager service caches")
    except (ImportError, AttributeError) as exc:
        logger.debug("Could not clear gdrive_manager caches: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def refresh_google_token() -> bool:
    """Force-refresh the Google OAuth access token.

    Reads token.json, uses the refresh_token to obtain a new access_token,
    saves the result to disk, and (on Railway) updates the env var.

    Returns:
        True if refresh succeeded, False otherwise.
    """
    from google.auth.transport.requests import Request

    creds = _load_credentials()
    if creds is None:
        logger.error("No credentials found — cannot refresh token")
        return False

    if not creds.refresh_token:
        logger.critical(
            "Google refresh_token is MISSING. The token file exists but has "
            "no refresh_token. Re-authorize with: python -m tools.core.token_manager --reauth"
        )
        _alert_token_revoked()
        return False

    try:
        creds.refresh(Request())
    except Exception as exc:
        error_str = str(exc).lower()
        is_revoked = any(
            kw in error_str
            for kw in ("revoked", "invalid_grant", "token has been expired or revoked")
        )
        if is_revoked:
            logger.critical(
                "Google refresh_token has been REVOKED. "
                "Re-authorize with: python -m tools.core.token_manager --reauth"
            )
            _alert_token_revoked()
        else:
            logger.error("Token refresh failed: %s", exc)
        return False

    # Save refreshed token
    if IS_RAILWAY:
        _update_railway_env_var(creds)
        # Also write to the materialized temp file so in-memory usage works
        token_path = _get_token_path()
        token_path.write_text(creds.to_json(), encoding="utf-8")
        logger.info("Token refreshed and saved (Railway mode)")
    else:
        _save_token_to_disk(creds)

    _invalidate_gdrive_service_cache()

    logger.info(
        "Google token refreshed successfully. New expiry: %s",
        creds.expiry.isoformat() if creds.expiry else "unknown",
    )
    return True


def check_token_health() -> dict[str, Any]:
    """Check Google OAuth token health.

    Returns:
        Dict with keys:
          - valid (bool): token can be used right now
          - expires_in_hours (float | None): hours until access_token expires
          - needs_refresh (bool): True if < 24h remaining or token invalid
          - has_refresh_token (bool): refresh_token is present
          - error (str | None): error message if something is wrong
    """
    result: dict[str, Any] = {
        "valid": False,
        "expires_in_hours": None,
        "needs_refresh": True,
        "has_refresh_token": False,
        "error": None,
    }

    creds = _load_credentials()
    if creds is None:
        result["error"] = "No token.json found"
        return result

    result["has_refresh_token"] = bool(creds.refresh_token)

    if not creds.refresh_token:
        result["error"] = "refresh_token is missing — re-authorization needed"
        return result

    if creds.expiry:
        now = datetime.now(timezone.utc)
        expiry = creds.expiry
        # Ensure expiry is timezone-aware
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        delta = (expiry - now).total_seconds()
        hours_remaining = delta / 3600.0
        result["expires_in_hours"] = round(hours_remaining, 2)
        result["valid"] = delta > 0
        result["needs_refresh"] = hours_remaining < 24.0
    else:
        # No expiry info — might still be valid, but we should refresh
        result["valid"] = creds.valid if hasattr(creds, "valid") else False
        result["needs_refresh"] = True

    return result


def ensure_fresh_token() -> None:
    """Ensure the Google token is fresh before a Drive/Docs operation.

    Call this at the start of any pipeline that uses Google APIs.
    If the token expires in less than 1 hour, forces a refresh.
    If refresh fails, alerts the operator.

    Raises:
        RuntimeError: If the token cannot be refreshed and no valid
            credentials exist (so the caller can abort the pipeline).
    """
    health = check_token_health()

    if health.get("error") and not health.get("has_refresh_token"):
        msg = (
            "Google OAuth token is unusable: "
            f"{health['error']}. "
            "Run: python -m tools.core.token_manager --reauth"
        )
        logger.critical(msg)
        _alert_token_revoked()
        raise RuntimeError(msg)

    expires_in = health.get("expires_in_hours")

    # Refresh if < 1 hour remaining or token is invalid
    needs_immediate_refresh = (
        not health["valid"]
        or (expires_in is not None and expires_in < (REFRESH_THRESHOLD_SECONDS / 3600.0))
    )

    if needs_immediate_refresh:
        logger.info(
            "Token needs refresh (valid=%s, expires_in=%.1fh). Refreshing...",
            health["valid"],
            expires_in if expires_in is not None else -1,
        )
        success = refresh_google_token()
        if not success:
            msg = (
                "Google token refresh FAILED. Pipeline may fail on Drive operations. "
                "Run: python -m tools.core.token_manager --reauth"
            )
            logger.error(msg)
            raise RuntimeError(msg)
    elif health.get("needs_refresh"):
        # Token is valid but getting close to expiry (< 24h) — refresh proactively
        logger.info(
            "Token valid but approaching expiry (%.1fh remaining). Proactive refresh...",
            expires_in if expires_in is not None else -1,
        )
        refresh_google_token()  # Best-effort, don't raise on failure


def _alert_token_revoked() -> None:
    """Alert the operator that the Google refresh token was revoked."""
    try:
        from tools.integrations.whatsapp_sender import alert_operator
        alert_operator(
            "CRITICAL: Google OAuth token has been REVOKED.\n\n"
            "The recording pipeline will FAIL until you re-authorize.\n\n"
            "Fix: Run on your local machine:\n"
            "  python -m tools.core.token_manager --reauth\n\n"
            "Then update Railway env var:\n"
            "  base64 -i token.json | railway variables set GOOGLE_TOKEN_JSON_B64=$(cat)"
        )
    except Exception as exc:
        logger.critical(
            "Could not alert operator about token revocation: %s", exc
        )


# ---------------------------------------------------------------------------
# Scheduler integration
# ---------------------------------------------------------------------------


def token_refresh_job() -> None:
    """Cron job that runs every 6 hours to proactively refresh the token.

    This is a blocking function intended for APScheduler's thread executor.
    """
    logger.info("[token] Scheduled token refresh starting...")
    health = check_token_health()
    logger.info(
        "[token] Current health: valid=%s, expires_in=%.1fh, has_refresh=%s",
        health["valid"],
        health.get("expires_in_hours") or -1,
        health.get("has_refresh_token"),
    )

    if not health.get("has_refresh_token"):
        logger.critical("[token] No refresh_token — alerting operator")
        _alert_token_revoked()
        return

    success = refresh_google_token()
    if success:
        logger.info("[token] Scheduled refresh completed successfully")
    else:
        logger.error("[token] Scheduled refresh FAILED — next attempt in 6 hours")


# ---------------------------------------------------------------------------
# CLI: --reauth interactive flow
# ---------------------------------------------------------------------------


def _run_reauth() -> None:
    """Run the interactive OAuth2 authorization flow.

    This must be run on a local machine with a browser. It:
    1. Reads credentials.json (client secrets).
    2. Opens a browser for the user to authorize.
    3. Saves the resulting token.json with a fresh refresh_token.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    credentials_path = get_google_credentials_path()
    if not credentials_path.exists():
        print(f"ERROR: credentials.json not found at {credentials_path}")
        print("Download it from Google Cloud Console > OAuth 2.0 Client IDs")
        sys.exit(1)

    print("Starting Google OAuth2 authorization flow...")
    print("A browser window will open. Sign in and grant permissions.")
    print()

    flow = InstalledAppFlow.from_client_secrets_file(
        str(credentials_path), SCOPES
    )
    creds = flow.run_local_server(port=0)

    _save_token_to_disk(creds)

    print()
    print(f"Token saved to: {TOKEN_PATH}")
    print()
    print("For Railway deployment, update the env var:")
    print(f"  base64 -i {TOKEN_PATH} | tr -d '\\n' | pbcopy")
    print("  railway variables set GOOGLE_TOKEN_JSON_B64=<paste>")
    print()

    # Show token health after re-auth
    health = check_token_health()
    print(f"Token valid: {health['valid']}")
    print(f"Expires in: {health.get('expires_in_hours', 'unknown')} hours")
    print(f"Has refresh_token: {health.get('has_refresh_token')}")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entrypoint: check health or re-authorize."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if "--reauth" in sys.argv:
        _run_reauth()
        return

    # Default: show token health
    print("Google OAuth Token Health Check")
    print("=" * 40)

    health = check_token_health()
    for key, value in health.items():
        print(f"  {key}: {value}")

    if health.get("error"):
        print()
        print(f"ERROR: {health['error']}")
        print("Run: python -m tools.core.token_manager --reauth")
        sys.exit(1)

    if not health["valid"]:
        print()
        print("Token is EXPIRED. Attempting refresh...")
        success = refresh_google_token()
        if success:
            print("Token refreshed successfully!")
        else:
            print("Refresh FAILED. Run: python -m tools.core.token_manager --reauth")
            sys.exit(1)
    elif health.get("needs_refresh"):
        print()
        print(f"Token expires in {health['expires_in_hours']:.1f}h — refreshing proactively...")
        refresh_google_token()
    else:
        print()
        print(f"Token is healthy. Expires in {health['expires_in_hours']:.1f}h.")


if __name__ == "__main__":
    main()
