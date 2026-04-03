"""Tests for the proactive health monitoring system.

All external API calls are mocked — these tests verify check logic,
severity thresholds, aggregation, alert formatting, and daily reports.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tools.core.config import TBILISI_TZ
from tools.core.health_monitor import (
    CheckResult,
    Severity,
    _api_error_timestamps,
    _send_health_alert,
    check_all,
    check_claude_api,
    check_disk_space,
    check_gemini_quota,
    check_google_token,
    check_pending_lectures,
    check_pinecone,
    check_stuck_pipelines,
    check_whatsapp,
    check_zoom_auth,
    clear_api_error,
    get_api_error_duration_minutes,
    record_api_error,
    run_daily_morning_report,
    run_health_check_job,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_api_errors():
    """Clear API error timestamps between tests."""
    _api_error_timestamps.clear()
    yield
    _api_error_timestamps.clear()


# ---------------------------------------------------------------------------
# CheckResult dataclass
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_to_dict(self):
        result = CheckResult(
            name="test",
            severity=Severity.OK,
            message="All good",
            details={"key": "value"},
        )
        d = result.to_dict()
        assert d["name"] == "test"
        assert d["severity"] == "ok"
        assert d["message"] == "All good"
        assert d["details"] == {"key": "value"}

    def test_frozen(self):
        result = CheckResult(name="x", severity=Severity.OK, message="ok")
        with pytest.raises(AttributeError):
            result.name = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# API error tracking
# ---------------------------------------------------------------------------


class TestApiErrorTracking:
    def test_record_and_get_duration(self):
        record_api_error("test_service")
        duration = get_api_error_duration_minutes("test_service")
        assert duration >= 0
        assert duration < 1  # Just recorded, should be < 1 min

    def test_clear_resets_duration(self):
        record_api_error("test_service")
        clear_api_error("test_service")
        assert get_api_error_duration_minutes("test_service") == 0.0

    def test_record_does_not_overwrite(self):
        """Recording twice should keep the FIRST timestamp."""
        record_api_error("svc")
        first_ts = _api_error_timestamps["svc"]
        record_api_error("svc")
        assert _api_error_timestamps["svc"] == first_ts

    def test_unknown_service_returns_zero(self):
        assert get_api_error_duration_minutes("nonexistent") == 0.0


# ---------------------------------------------------------------------------
# check_disk_space
# ---------------------------------------------------------------------------


class TestCheckDiskSpace:
    @patch("tools.core.health_monitor.shutil.disk_usage")
    def test_ok(self, mock_usage):
        mock_usage.return_value = MagicMock(free=20 * 1024**3)  # 20 GB
        result = check_disk_space()
        assert result.severity == Severity.OK
        assert result.details["free_gb"] == 20.0

    @patch("tools.core.health_monitor.shutil.disk_usage")
    def test_warning(self, mock_usage):
        mock_usage.return_value = MagicMock(free=3 * 1024**3)  # 3 GB
        result = check_disk_space()
        assert result.severity == Severity.WARNING

    @patch("tools.core.health_monitor.shutil.disk_usage")
    def test_critical(self, mock_usage):
        mock_usage.return_value = MagicMock(free=1 * 1024**3)  # 1 GB
        result = check_disk_space()
        assert result.severity == Severity.CRITICAL

    @patch("tools.core.health_monitor.shutil.disk_usage", side_effect=OSError("fail"))
    def test_error_returns_warning(self, mock_usage):
        result = check_disk_space()
        assert result.severity == Severity.WARNING
        assert "Cannot check" in result.message


# ---------------------------------------------------------------------------
# check_google_token
# ---------------------------------------------------------------------------


class TestCheckGoogleToken:
    def test_token_not_found(self):
        with patch(
            "tools.integrations.gdrive_manager._get_credentials", return_value=None
        ):
            result = check_google_token()
            assert result.severity == Severity.CRITICAL
            assert "not found" in result.message

    def test_token_valid_with_refresh(self):
        """Access token valid with refresh_token → OK."""
        mock_token = MagicMock()
        mock_token.expiry = datetime.utcnow() + timedelta(hours=100)
        mock_token.refresh_token = "valid-refresh-token"
        with patch(
            "tools.integrations.gdrive_manager._get_credentials", return_value=mock_token
        ):
            result = check_google_token()
            assert result.severity == Severity.OK

    def test_token_valid_without_refresh(self):
        """Access token valid but far from expiry, no refresh_token → OK."""
        mock_token = MagicMock()
        mock_token.expiry = datetime.utcnow() + timedelta(hours=100)
        mock_token.refresh_token = None
        with patch(
            "tools.integrations.gdrive_manager._get_credentials", return_value=mock_token
        ):
            result = check_google_token()
            assert result.severity == Severity.OK

    def test_token_warning_threshold(self):
        """Access token in warning range WITHOUT refresh_token → WARNING."""
        mock_token = MagicMock()
        mock_token.expiry = datetime.utcnow() + timedelta(hours=24)
        mock_token.refresh_token = None
        with patch(
            "tools.integrations.gdrive_manager._get_credentials", return_value=mock_token
        ):
            result = check_google_token()
            assert result.severity == Severity.WARNING

    def test_token_critical_threshold_no_refresh_token(self):
        """Access token expiring soon WITHOUT refresh_token → CRITICAL."""
        mock_token = MagicMock()
        mock_token.expiry = datetime.utcnow() + timedelta(hours=2)
        mock_token.refresh_token = None
        with patch(
            "tools.integrations.gdrive_manager._get_credentials", return_value=mock_token
        ):
            result = check_google_token()
            assert result.severity == Severity.CRITICAL

    def test_token_expiring_with_refresh_token_is_ok(self):
        """Access token expiring soon WITH valid refresh_token → OK (auto-refreshable)."""
        mock_token = MagicMock()
        mock_token.expiry = datetime.utcnow() + timedelta(minutes=30)
        mock_token.refresh_token = "valid-refresh-token"
        with patch(
            "tools.integrations.gdrive_manager._get_credentials", return_value=mock_token
        ):
            result = check_google_token()
            assert result.severity == Severity.OK
            assert "auto-refresh" in result.message.lower()

    def test_token_warning_with_refresh_token_is_ok(self):
        """Access token in warning range WITH refresh_token → OK."""
        mock_token = MagicMock()
        mock_token.expiry = datetime.utcnow() + timedelta(hours=24)
        mock_token.refresh_token = "valid-refresh-token"
        with patch(
            "tools.integrations.gdrive_manager._get_credentials", return_value=mock_token
        ):
            result = check_google_token()
            assert result.severity == Severity.OK

    def test_token_warning_without_refresh_token(self):
        """Access token in warning range WITHOUT refresh_token → WARNING."""
        mock_token = MagicMock()
        mock_token.expiry = datetime.utcnow() + timedelta(hours=24)
        mock_token.refresh_token = None
        with patch(
            "tools.integrations.gdrive_manager._get_credentials", return_value=mock_token
        ):
            result = check_google_token()
            assert result.severity == Severity.WARNING

    def test_token_no_expiry(self):
        mock_token = MagicMock(spec=[])  # no expiry attr
        with patch(
            "tools.integrations.gdrive_manager._get_credentials", return_value=mock_token
        ):
            result = check_google_token()
            assert result.severity == Severity.OK
            assert "no expiry" in result.message

    def test_import_error(self):
        with patch(
            "tools.integrations.gdrive_manager._get_credentials",
            side_effect=ImportError("no module"),
        ):
            result = check_google_token()
            assert result.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# check_zoom_auth
# ---------------------------------------------------------------------------


class TestCheckZoomAuth:
    @patch("tools.core.health_monitor.ZOOM_ACCOUNT_ID", "")
    def test_no_credentials(self):
        result = check_zoom_auth()
        assert result.severity == Severity.CRITICAL
        assert "not configured" in result.message

    @patch("tools.core.health_monitor.ZOOM_ACCOUNT_ID", "id")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_ID", "cid")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_SECRET", "secret")
    def test_success(self):
        with patch(
            "tools.integrations.zoom_manager.get_access_token", return_value="tok123"
        ):
            result = check_zoom_auth()
            assert result.severity == Severity.OK

    @patch("tools.core.health_monitor.ZOOM_ACCOUNT_ID", "id")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_ID", "cid")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_SECRET", "secret")
    def test_failure(self):
        with patch(
            "tools.integrations.zoom_manager.get_access_token",
            side_effect=RuntimeError("auth error"),
        ):
            result = check_zoom_auth()
            assert result.severity in (Severity.WARNING, Severity.CRITICAL)


# ---------------------------------------------------------------------------
# check_gemini_quota
# ---------------------------------------------------------------------------


class TestCheckGeminiQuota:
    @patch("tools.core.health_monitor.GEMINI_API_KEY", "")
    @patch("tools.core.health_monitor.GEMINI_API_KEY_PAID", "")
    def test_no_key(self):
        result = check_gemini_quota()
        assert result.severity == Severity.CRITICAL

    @patch("tools.core.health_monitor.GEMINI_API_KEY", "key123")
    def test_success(self):
        mock_response = MagicMock()
        mock_response.text = "OK"
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with patch("google.genai.Client", return_value=mock_client):
            result = check_gemini_quota()
            assert result.severity == Severity.OK

    @patch("tools.core.health_monitor.GEMINI_API_KEY", "key123")
    def test_failure(self):
        with patch("google.genai.Client", side_effect=RuntimeError("quota")):
            result = check_gemini_quota()
            assert result.severity in (Severity.WARNING, Severity.CRITICAL)


# ---------------------------------------------------------------------------
# check_claude_api
# ---------------------------------------------------------------------------


class TestCheckClaudeApi:
    @patch("tools.core.health_monitor.ANTHROPIC_API_KEY", "")
    def test_no_key(self):
        result = check_claude_api()
        assert result.severity == Severity.CRITICAL

    @patch("tools.core.health_monitor.ANTHROPIC_API_KEY", "sk-ant-test")
    def test_success(self):
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="OK")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        with patch("anthropic.Anthropic", return_value=mock_client):
            result = check_claude_api()
            assert result.severity == Severity.OK

    @patch("tools.core.health_monitor.ANTHROPIC_API_KEY", "sk-ant-test")
    def test_failure(self):
        with patch("anthropic.Anthropic", side_effect=RuntimeError("api error")):
            result = check_claude_api()
            assert result.severity in (Severity.WARNING, Severity.CRITICAL)


# ---------------------------------------------------------------------------
# check_whatsapp
# ---------------------------------------------------------------------------


class TestCheckWhatsapp:
    @patch("tools.core.health_monitor.GREEN_API_INSTANCE_ID", "")
    def test_not_configured(self):
        result = check_whatsapp()
        assert result.severity == Severity.WARNING

    @patch("tools.core.health_monitor.GREEN_API_INSTANCE_ID", "inst123")
    @patch("tools.core.health_monitor.GREEN_API_TOKEN", "tok123")
    def test_authorized(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"stateInstance": "authorized"}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response

        with patch("httpx.Client", return_value=mock_client):
            result = check_whatsapp()
            assert result.severity == Severity.OK

    @patch("tools.core.health_monitor.GREEN_API_INSTANCE_ID", "inst123")
    @patch("tools.core.health_monitor.GREEN_API_TOKEN", "tok123")
    def test_not_authorized(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"stateInstance": "notAuthorized"}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response

        with patch("httpx.Client", return_value=mock_client):
            result = check_whatsapp()
            assert result.severity == Severity.WARNING


# ---------------------------------------------------------------------------
# check_pinecone
# ---------------------------------------------------------------------------


class TestCheckPinecone:
    @patch("tools.core.health_monitor.PINECONE_API_KEY", "")
    def test_no_key(self):
        result = check_pinecone()
        assert result.severity == Severity.WARNING

    @patch("tools.core.health_monitor.PINECONE_API_KEY", "pk-test")
    def test_success(self):
        mock_index = MagicMock()
        mock_index.describe_index_stats.return_value = {"total_vector_count": 500}
        mock_pc = MagicMock()
        mock_pc.Index.return_value = mock_index

        with patch("pinecone.Pinecone", return_value=mock_pc):
            result = check_pinecone()
            assert result.severity == Severity.OK
            assert result.details["total_vectors"] == 500

    @patch("tools.core.health_monitor.PINECONE_API_KEY", "pk-test")
    def test_failure(self):
        with patch("pinecone.Pinecone", side_effect=RuntimeError("conn error")):
            result = check_pinecone()
            assert result.severity in (Severity.WARNING, Severity.CRITICAL)


# ---------------------------------------------------------------------------
# check_pending_lectures
# ---------------------------------------------------------------------------


class TestCheckPendingLectures:
    def test_no_lecture_today(self):
        """On a day with no lectures (Wednesday), should return OK."""
        # Wednesday weekday=2 — neither group has lectures
        mock_now = datetime(2026, 3, 25, 10, 0, 0, tzinfo=TBILISI_TZ)
        with patch("tools.core.health_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            # Allow datetime(...) constructor to still work
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_pending_lectures()
        assert result.severity == Severity.OK

    def test_lecture_not_overdue_yet(self):
        """Before the overdue window, should return OK."""
        # Monday at 23:00 — meeting ended at 22:00, only 1h ago (threshold is 4h)
        mock_now = datetime(2026, 3, 30, 23, 0, 0, tzinfo=TBILISI_TZ)
        with patch("tools.core.health_monitor.datetime") as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_pending_lectures()
        assert result.severity == Severity.OK


# ---------------------------------------------------------------------------
# check_stuck_pipelines
# ---------------------------------------------------------------------------


class TestCheckStuckPipelines:
    def test_no_active_pipelines(self):
        with patch(
            "tools.core.pipeline_state.list_active_pipelines", return_value=[]
        ):
            result = check_stuck_pipelines()
            assert result.severity == Severity.OK

    def test_stuck_pipeline(self):
        mock_pipeline = MagicMock()
        mock_pipeline.group = 1
        mock_pipeline.lecture = 5
        mock_pipeline.status = "transcribing"
        mock_pipeline.started_at = (
            datetime.now(TBILISI_TZ) - timedelta(hours=3)
        ).isoformat()

        with patch(
            "tools.core.pipeline_state.list_active_pipelines",
            return_value=[mock_pipeline],
        ):
            result = check_stuck_pipelines()
            assert result.severity == Severity.WARNING
            assert "stuck" in result.message.lower()


# ---------------------------------------------------------------------------
# check_all
# ---------------------------------------------------------------------------


class TestCheckAll:
    @patch("tools.core.health_monitor.check_disk_space")
    @patch("tools.core.health_monitor.check_whatsapp")
    @patch("tools.core.health_monitor.check_pinecone")
    @patch("tools.core.health_monitor.check_pending_lectures")
    @patch("tools.core.health_monitor.check_stuck_pipelines")
    @patch("tools.core.health_monitor.check_google_token")
    @patch("tools.core.health_monitor.ZOOM_ACCOUNT_ID", "")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_ID", "")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_SECRET", "")
    @patch("tools.core.health_monitor.GEMINI_API_KEY", "")
    @patch("tools.core.health_monitor.GEMINI_API_KEY_PAID", "")
    @patch("tools.core.health_monitor.ANTHROPIC_API_KEY", "")
    def test_all_ok(self, mock_gt, mock_sp, mock_pl, mock_pc, mock_wa, mock_ds):
        ok = CheckResult(name="test", severity=Severity.OK, message="ok")
        mock_ds.return_value = ok
        mock_wa.return_value = ok
        mock_pc.return_value = ok
        mock_pl.return_value = ok
        mock_sp.return_value = ok
        mock_gt.return_value = ok

        report = check_all()
        assert report["overall_status"] == "healthy"
        assert report["warnings_count"] == 0
        assert report["critical_count"] == 0

    @patch("tools.core.health_monitor.check_disk_space")
    @patch("tools.core.health_monitor.check_whatsapp")
    @patch("tools.core.health_monitor.check_pinecone")
    @patch("tools.core.health_monitor.check_pending_lectures")
    @patch("tools.core.health_monitor.check_stuck_pipelines")
    @patch("tools.core.health_monitor.check_google_token")
    @patch("tools.core.health_monitor.ZOOM_ACCOUNT_ID", "")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_ID", "")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_SECRET", "")
    @patch("tools.core.health_monitor.GEMINI_API_KEY", "")
    @patch("tools.core.health_monitor.GEMINI_API_KEY_PAID", "")
    @patch("tools.core.health_monitor.ANTHROPIC_API_KEY", "")
    def test_warning_state(self, mock_gt, mock_sp, mock_pl, mock_pc, mock_wa, mock_ds):
        ok = CheckResult(name="ok", severity=Severity.OK, message="ok")
        warn = CheckResult(name="warn", severity=Severity.WARNING, message="low")
        mock_ds.return_value = warn
        mock_wa.return_value = ok
        mock_pc.return_value = ok
        mock_pl.return_value = ok
        mock_sp.return_value = ok
        mock_gt.return_value = ok

        report = check_all()
        assert report["overall_status"] == "degraded"
        assert report["warnings_count"] == 1

    @patch("tools.core.health_monitor.check_disk_space")
    @patch("tools.core.health_monitor.check_whatsapp")
    @patch("tools.core.health_monitor.check_pinecone")
    @patch("tools.core.health_monitor.check_pending_lectures")
    @patch("tools.core.health_monitor.check_stuck_pipelines")
    @patch("tools.core.health_monitor.check_google_token")
    @patch("tools.core.health_monitor.ZOOM_ACCOUNT_ID", "")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_ID", "")
    @patch("tools.core.health_monitor.ZOOM_CLIENT_SECRET", "")
    @patch("tools.core.health_monitor.GEMINI_API_KEY", "")
    @patch("tools.core.health_monitor.GEMINI_API_KEY_PAID", "")
    @patch("tools.core.health_monitor.ANTHROPIC_API_KEY", "")
    def test_critical_state(self, mock_gt, mock_sp, mock_pl, mock_pc, mock_wa, mock_ds):
        ok = CheckResult(name="ok", severity=Severity.OK, message="ok")
        crit = CheckResult(name="crit", severity=Severity.CRITICAL, message="bad")
        mock_ds.return_value = crit
        mock_wa.return_value = ok
        mock_pc.return_value = ok
        mock_pl.return_value = ok
        mock_sp.return_value = ok
        mock_gt.return_value = ok

        report = check_all()
        assert report["overall_status"] == "critical"
        assert report["critical_count"] == 1

    def test_report_has_timestamp(self):
        """Verify check_all returns required structure keys."""
        report = check_all()
        assert "timestamp" in report
        assert "checks" in report
        assert isinstance(report["checks"], list)


# ---------------------------------------------------------------------------
# Alert formatting
# ---------------------------------------------------------------------------


class TestSendHealthAlert:
    @patch("tools.integrations.whatsapp_sender.alert_operator")
    def test_sends_alert_for_warnings(self, mock_alert):
        report = {
            "overall_status": "degraded",
            "checks": [
                {"name": "disk", "severity": "warning", "message": "Low space"},
                {"name": "api", "severity": "ok", "message": "Fine"},
            ],
        }
        _send_health_alert(report)
        mock_alert.assert_called_once()
        msg = mock_alert.call_args[0][0]
        assert "disk" in msg
        assert "Low space" in msg

    @patch("tools.integrations.whatsapp_sender.alert_operator")
    def test_no_alert_if_all_ok(self, mock_alert):
        report = {
            "overall_status": "healthy",
            "checks": [
                {"name": "api", "severity": "ok", "message": "Fine"},
            ],
        }
        _send_health_alert(report)
        mock_alert.assert_not_called()


# ---------------------------------------------------------------------------
# run_health_check_job
# ---------------------------------------------------------------------------


class TestRunHealthCheckJob:
    @patch("tools.core.health_monitor._send_health_alert")
    @patch("tools.core.health_monitor.check_all")
    def test_alerts_on_warning(self, mock_check_all, mock_alert):
        mock_check_all.return_value = {
            "overall_status": "degraded",
            "warnings_count": 1,
            "critical_count": 0,
            "checks": [],
        }
        run_health_check_job()
        mock_alert.assert_called_once()

    @patch("tools.core.health_monitor._send_health_alert")
    @patch("tools.core.health_monitor.check_all")
    def test_no_alert_when_healthy(self, mock_check_all, mock_alert):
        mock_check_all.return_value = {
            "overall_status": "healthy",
            "warnings_count": 0,
            "critical_count": 0,
            "checks": [],
        }
        run_health_check_job()
        mock_alert.assert_not_called()

    @patch("tools.core.health_monitor.check_all", side_effect=RuntimeError("boom"))
    @patch("tools.integrations.whatsapp_sender.alert_operator")
    def test_handles_check_failure(self, mock_alert, mock_check_all):
        """If check_all itself crashes, alert the operator."""
        run_health_check_job()
        mock_alert.assert_called_once()
        assert "failed" in mock_alert.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# run_daily_morning_report
# ---------------------------------------------------------------------------


class TestRunDailyMorningReport:
    @patch("tools.integrations.whatsapp_sender.send_private_report")
    @patch("tools.core.health_monitor.check_all")
    def test_sends_report(self, mock_check_all, mock_send):
        mock_check_all.return_value = {
            "overall_status": "healthy",
            "checks": [],
            "warnings_count": 0,
            "critical_count": 0,
        }
        run_daily_morning_report()
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "დილის რეპორტი" in msg
        assert "სისტემა" in msg

    @patch("tools.core.health_monitor.check_all", side_effect=RuntimeError("boom"))
    def test_handles_check_failure(self, mock_check_all):
        """Should not raise even if check_all fails."""
        run_daily_morning_report()  # Should not raise


# ---------------------------------------------------------------------------
# Severity enum
# ---------------------------------------------------------------------------


class TestSeverity:
    def test_values(self):
        assert Severity.OK.value == "ok"
        assert Severity.WARNING.value == "warning"
        assert Severity.CRITICAL.value == "critical"

    def test_string_comparison(self):
        assert Severity.OK == "ok"
        assert Severity.CRITICAL == "critical"
