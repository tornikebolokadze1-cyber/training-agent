"""Tests for tools.core.token_manager — Google OAuth token lifecycle."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Helpers — build mock credentials objects
# ---------------------------------------------------------------------------


def _make_mock_creds(
    *,
    valid: bool = True,
    expired: bool = False,
    refresh_token: str | None = "mock-refresh-token",
    expiry_hours_from_now: float | None = 12.0,
) -> MagicMock:
    """Build a mock Credentials object."""
    creds = MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    if expiry_hours_from_now is not None:
        creds.expiry = datetime.now(timezone.utc) + timedelta(hours=expiry_hours_from_now)
    else:
        creds.expiry = None
    creds.to_json.return_value = json.dumps({
        "token": "mock-access-token",
        "refresh_token": refresh_token or "",
        "expiry": creds.expiry.isoformat() if creds.expiry else "",
    })
    return creds


# ---------------------------------------------------------------------------
# check_token_health
# ---------------------------------------------------------------------------


class TestCheckTokenHealth:
    """Tests for check_token_health()."""

    @patch("tools.core.token_manager._load_credentials")
    def test_no_credentials_file(self, mock_load):
        """Returns error when no token.json exists."""
        mock_load.return_value = None
        from tools.core.token_manager import check_token_health

        result = check_token_health()
        assert result["valid"] is False
        assert result["error"] is not None
        assert "No token.json" in result["error"]

    @patch("tools.core.token_manager._load_credentials")
    def test_missing_refresh_token(self, mock_load):
        """Returns error when refresh_token is absent."""
        mock_load.return_value = _make_mock_creds(refresh_token=None)
        from tools.core.token_manager import check_token_health

        result = check_token_health()
        assert result["has_refresh_token"] is False
        assert "re-authorization" in result["error"]

    @patch("tools.core.token_manager._load_credentials")
    def test_healthy_token(self, mock_load):
        """Healthy token with >24h remaining reports valid and no refresh needed."""
        mock_load.return_value = _make_mock_creds(
            valid=True, expiry_hours_from_now=48.0
        )
        from tools.core.token_manager import check_token_health

        result = check_token_health()
        assert result["valid"] is True
        assert result["needs_refresh"] is False
        assert result["has_refresh_token"] is True
        assert result["error"] is None
        assert result["expires_in_hours"] > 24

    @patch("tools.core.token_manager._load_credentials")
    def test_token_needs_refresh_soon(self, mock_load):
        """Token expiring within 24h should report needs_refresh=True."""
        mock_load.return_value = _make_mock_creds(
            valid=True, expiry_hours_from_now=6.0
        )
        from tools.core.token_manager import check_token_health

        result = check_token_health()
        assert result["valid"] is True
        assert result["needs_refresh"] is True
        assert result["expires_in_hours"] < 24

    @patch("tools.core.token_manager._load_credentials")
    def test_expired_token(self, mock_load):
        """Expired token reports valid=False."""
        mock_load.return_value = _make_mock_creds(
            valid=False, expired=True, expiry_hours_from_now=-1.0
        )
        from tools.core.token_manager import check_token_health

        result = check_token_health()
        assert result["valid"] is False
        assert result["needs_refresh"] is True

    @patch("tools.core.token_manager._load_credentials")
    def test_no_expiry_info(self, mock_load):
        """Token with no expiry info should flag needs_refresh."""
        mock_load.return_value = _make_mock_creds(expiry_hours_from_now=None)
        from tools.core.token_manager import check_token_health

        result = check_token_health()
        assert result["needs_refresh"] is True
        assert result["expires_in_hours"] is None


# ---------------------------------------------------------------------------
# refresh_google_token
# ---------------------------------------------------------------------------


class TestRefreshGoogleToken:
    """Tests for refresh_google_token()."""

    @patch("tools.core.token_manager._invalidate_gdrive_service_cache")
    @patch("tools.core.token_manager._save_token_to_disk")
    @patch("tools.core.token_manager.IS_RAILWAY", False)
    @patch("tools.core.token_manager._load_credentials")
    def test_successful_refresh_local(self, mock_load, mock_save, mock_invalidate):
        """On local, refreshes and saves to disk."""
        creds = _make_mock_creds(valid=False, expired=True, expiry_hours_from_now=-1)
        creds.refresh.return_value = None
        # After refresh, update expiry
        creds.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_load.return_value = creds

        from tools.core.token_manager import refresh_google_token

        with patch("google.auth.transport.requests.Request"):
            result = refresh_google_token()

        assert result is True
        creds.refresh.assert_called_once()
        mock_save.assert_called_once_with(creds)
        mock_invalidate.assert_called_once()

    @patch("tools.core.token_manager._invalidate_gdrive_service_cache")
    @patch("tools.core.token_manager._update_railway_env_var")
    @patch("tools.core.token_manager._get_token_path")
    @patch("tools.core.token_manager.IS_RAILWAY", True)
    @patch("tools.core.token_manager._load_credentials")
    def test_successful_refresh_railway(self, mock_load, mock_token_path, mock_railway, mock_invalidate):
        """On Railway, updates env var instead of disk."""
        creds = _make_mock_creds(valid=False, expired=True)
        creds.refresh.return_value = None
        creds.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_load.return_value = creds
        mock_path = MagicMock()
        mock_token_path.return_value = mock_path

        from tools.core.token_manager import refresh_google_token

        with patch("google.auth.transport.requests.Request"):
            result = refresh_google_token()

        assert result is True
        mock_railway.assert_called_once_with(creds)
        mock_invalidate.assert_called_once()

    @patch("tools.core.token_manager._load_credentials")
    def test_no_credentials(self, mock_load):
        """Returns False when no credentials exist."""
        mock_load.return_value = None
        from tools.core.token_manager import refresh_google_token

        result = refresh_google_token()
        assert result is False

    @patch("tools.core.token_manager._alert_token_revoked")
    @patch("tools.core.token_manager._load_credentials")
    def test_missing_refresh_token(self, mock_load, mock_alert):
        """Returns False and alerts when refresh_token is missing."""
        creds = _make_mock_creds(refresh_token=None)
        mock_load.return_value = creds
        from tools.core.token_manager import refresh_google_token

        result = refresh_google_token()
        assert result is False
        mock_alert.assert_called_once()

    @patch("tools.core.token_manager._alert_token_revoked")
    @patch("tools.core.token_manager._load_credentials")
    def test_revoked_token_alerts(self, mock_load, mock_alert):
        """Revoked token triggers CRITICAL alert to operator."""
        creds = _make_mock_creds(valid=False, expired=True)
        creds.refresh.side_effect = Exception("Token has been expired or revoked")
        mock_load.return_value = creds

        from tools.core.token_manager import refresh_google_token

        with patch("google.auth.transport.requests.Request"):
            result = refresh_google_token()

        assert result is False
        mock_alert.assert_called_once()

    @patch("tools.core.token_manager._load_credentials")
    def test_transient_error_no_revoked_alert(self, mock_load):
        """Transient network errors should NOT trigger revoked alert."""
        creds = _make_mock_creds(valid=False, expired=True)
        creds.refresh.side_effect = Exception("Connection timeout")
        mock_load.return_value = creds

        from tools.core.token_manager import refresh_google_token

        with patch("google.auth.transport.requests.Request"), \
             patch("tools.core.token_manager._alert_token_revoked") as mock_alert:
            result = refresh_google_token()

        assert result is False
        mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# ensure_fresh_token
# ---------------------------------------------------------------------------


class TestEnsureFreshToken:
    """Tests for ensure_fresh_token()."""

    @patch("tools.core.token_manager.refresh_google_token")
    @patch("tools.core.token_manager.check_token_health")
    def test_healthy_token_no_refresh(self, mock_health, mock_refresh):
        """Healthy token with plenty of time does not trigger refresh."""
        mock_health.return_value = {
            "valid": True,
            "expires_in_hours": 48.0,
            "needs_refresh": False,
            "has_refresh_token": True,
            "error": None,
        }
        from tools.core.token_manager import ensure_fresh_token

        ensure_fresh_token()
        mock_refresh.assert_not_called()

    @patch("tools.core.token_manager.refresh_google_token", return_value=True)
    @patch("tools.core.token_manager.check_token_health")
    def test_expiring_soon_triggers_refresh(self, mock_health, mock_refresh):
        """Token expiring in < 1h forces immediate refresh."""
        mock_health.return_value = {
            "valid": True,
            "expires_in_hours": 0.5,
            "needs_refresh": True,
            "has_refresh_token": True,
            "error": None,
        }
        from tools.core.token_manager import ensure_fresh_token

        ensure_fresh_token()
        mock_refresh.assert_called_once()

    @patch("tools.core.token_manager.refresh_google_token", return_value=True)
    @patch("tools.core.token_manager.check_token_health")
    def test_invalid_token_triggers_refresh(self, mock_health, mock_refresh):
        """Invalid token triggers immediate refresh."""
        mock_health.return_value = {
            "valid": False,
            "expires_in_hours": -1.0,
            "needs_refresh": True,
            "has_refresh_token": True,
            "error": None,
        }
        from tools.core.token_manager import ensure_fresh_token

        ensure_fresh_token()
        mock_refresh.assert_called_once()

    @patch("tools.core.token_manager.refresh_google_token", return_value=False)
    @patch("tools.core.token_manager.check_token_health")
    def test_failed_refresh_raises(self, mock_health, mock_refresh):
        """Failed refresh raises RuntimeError for pipeline to abort."""
        mock_health.return_value = {
            "valid": False,
            "expires_in_hours": -1.0,
            "needs_refresh": True,
            "has_refresh_token": True,
            "error": None,
        }
        from tools.core.token_manager import ensure_fresh_token

        with pytest.raises(RuntimeError, match="refresh FAILED"):
            ensure_fresh_token()

    @patch("tools.core.token_manager._alert_token_revoked")
    @patch("tools.core.token_manager.check_token_health")
    def test_no_refresh_token_raises(self, mock_health, mock_alert):
        """Missing refresh_token raises RuntimeError immediately."""
        mock_health.return_value = {
            "valid": False,
            "expires_in_hours": None,
            "needs_refresh": True,
            "has_refresh_token": False,
            "error": "refresh_token is missing — re-authorization needed",
        }
        from tools.core.token_manager import ensure_fresh_token

        with pytest.raises(RuntimeError, match="unusable"):
            ensure_fresh_token()
        mock_alert.assert_called_once()

    @patch("tools.core.token_manager.refresh_google_token", return_value=True)
    @patch("tools.core.token_manager.check_token_health")
    def test_proactive_refresh_within_24h(self, mock_health, mock_refresh):
        """Token valid but < 24h remaining triggers proactive refresh."""
        mock_health.return_value = {
            "valid": True,
            "expires_in_hours": 12.0,
            "needs_refresh": True,
            "has_refresh_token": True,
            "error": None,
        }
        from tools.core.token_manager import ensure_fresh_token

        ensure_fresh_token()
        # Should call refresh proactively (best-effort)
        mock_refresh.assert_called_once()


# ---------------------------------------------------------------------------
# token_refresh_job (scheduler cron)
# ---------------------------------------------------------------------------


class TestTokenRefreshJob:
    """Tests for the scheduler cron job wrapper."""

    @patch("tools.core.token_manager.refresh_google_token", return_value=True)
    @patch("tools.core.token_manager.check_token_health")
    def test_job_refreshes_healthy(self, mock_health, mock_refresh):
        """Cron job calls refresh when token has a refresh_token."""
        mock_health.return_value = {
            "valid": True,
            "expires_in_hours": 5.5,
            "has_refresh_token": True,
        }
        from tools.core.token_manager import token_refresh_job

        token_refresh_job()
        mock_refresh.assert_called_once()

    @patch("tools.core.token_manager._alert_token_revoked")
    @patch("tools.core.token_manager.refresh_google_token")
    @patch("tools.core.token_manager.check_token_health")
    def test_job_alerts_on_missing_refresh_token(self, mock_health, mock_refresh, mock_alert):
        """Cron job alerts operator when refresh_token is missing."""
        mock_health.return_value = {
            "valid": False,
            "expires_in_hours": None,
            "has_refresh_token": False,
        }
        from tools.core.token_manager import token_refresh_job

        token_refresh_job()
        mock_alert.assert_called_once()
        mock_refresh.assert_not_called()


# ---------------------------------------------------------------------------
# _invalidate_gdrive_service_cache
# ---------------------------------------------------------------------------


class TestInvalidateCache:
    """Tests for cache invalidation after refresh."""

    def test_clears_caches(self):
        """After refresh, Drive service caches are cleared."""
        from tools.core.token_manager import _invalidate_gdrive_service_cache

        # Set up fake caches on the gdrive_manager module
        gdm = sys.modules.get("tools.integrations.gdrive_manager")
        if gdm is None:
            pytest.skip("gdrive_manager not importable in test env")

        gdm._drive_service_cache = "fake"
        gdm._docs_service_cache = "fake"
        gdm._token_path_cache = "fake"

        _invalidate_gdrive_service_cache()

        assert gdm._drive_service_cache is None
        assert gdm._docs_service_cache is None
        assert gdm._token_path_cache is None


# ---------------------------------------------------------------------------
# _alert_token_revoked
# ---------------------------------------------------------------------------


class TestAlertTokenRevoked:
    """Tests for operator alerting on revoked tokens."""

    @patch("tools.core.token_manager.alert_operator", create=True)
    def test_sends_whatsapp_alert(self, mock_alert):
        """Revocation alert includes re-auth instructions."""
        # We need to patch at the import location
        with patch("tools.integrations.whatsapp_sender.alert_operator", mock_alert):
            from tools.core.token_manager import _alert_token_revoked
            _alert_token_revoked()

        mock_alert.assert_called_once()
        call_msg = mock_alert.call_args[0][0]
        assert "REVOKED" in call_msg
        assert "--reauth" in call_msg

    def test_alert_failure_does_not_raise(self):
        """If WhatsApp alert itself fails, no exception escapes."""
        with patch(
            "tools.integrations.whatsapp_sender.alert_operator",
            side_effect=Exception("WhatsApp down"),
        ):
            from tools.core.token_manager import _alert_token_revoked
            # Should not raise
            _alert_token_revoked()
