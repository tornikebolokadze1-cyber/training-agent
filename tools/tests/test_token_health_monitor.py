"""Tests for proactive token health monitoring."""

from __future__ import annotations

from unittest.mock import patch

from tools.services import token_health_monitor


def test_healthy_token_no_alert():
    """Token expires in 30 days → no alert, status=healthy."""
    health = {
        "valid": True,
        "expires_in_hours": 720.0,
        "has_refresh_token": True,
        "needs_refresh": False,
        "error": None,
    }
    with patch.object(
        token_health_monitor, "__name__", token_health_monitor.__name__
    ), patch(
        "tools.core.token_manager.check_token_health", return_value=health
    ), patch(
        "tools.integrations.whatsapp_sender.alert_operator"
    ) as mock_alert, patch(
        "tools.core.token_manager.refresh_google_token"
    ) as mock_refresh:
        result = token_health_monitor.check_token_proactively()

    assert result["status"] == "healthy"
    assert result["alert_sent"] is False
    assert result["days_remaining"] == 30
    mock_alert.assert_not_called()
    mock_refresh.assert_not_called()


def test_revoked_token_alerts():
    """Token already revoked → operator alerted, status=critical."""
    health = {
        "valid": False,
        "expires_in_hours": None,
        "has_refresh_token": False,
        "needs_refresh": True,
        "error": "invalid_grant: Token has been expired or revoked.",
    }
    with patch(
        "tools.core.token_manager.check_token_health", return_value=health
    ), patch(
        "tools.integrations.whatsapp_sender.alert_operator"
    ) as mock_alert, patch(
        "tools.core.token_manager.refresh_google_token"
    ):
        result = token_health_monitor.check_token_proactively()

    assert result["status"] == "critical"
    assert result["alert_sent"] is True
    mock_alert.assert_called_once()
    assert "REVOKED" in mock_alert.call_args.args[0]


def test_expiring_soon_attempts_refresh():
    """Token expires in 2 days → attempt refresh, healthy after success."""
    health = {
        "valid": True,
        "expires_in_hours": 48.0,
        "has_refresh_token": True,
        "needs_refresh": True,
        "error": None,
    }
    with patch(
        "tools.core.token_manager.check_token_health", return_value=health
    ), patch(
        "tools.core.token_manager.refresh_google_token", return_value=True
    ) as mock_refresh, patch(
        "tools.integrations.whatsapp_sender.alert_operator"
    ) as mock_alert:
        result = token_health_monitor.check_token_proactively()

    assert result["status"] == "healthy"
    assert "refresh" in result["action_taken"].lower()
    assert result["alert_sent"] is False
    mock_refresh.assert_called_once()
    mock_alert.assert_not_called()
