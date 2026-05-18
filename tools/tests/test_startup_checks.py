"""Tests for orchestrator startup probes — US-007 + US-008.

Covers:
- _validate_drive_folders happy path (all OK)
- _validate_drive_folders 404 → "missing" status + alert_operator called
- _validate_drive_folders 403 → "forbidden" status + alert_operator called
- _validate_drive_folders skips course_completed=True groups
- _probe_google_token success path
- _probe_google_token invalid_grant → Georgian operator alert
- _probe_google_token gated on IS_RAILWAY=False
"""

from __future__ import annotations

from unittest.mock import MagicMock


# Import orchestrator with stubbed deps from conftest.
import tools.app.orchestrator as orch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_http_error(status: int) -> Exception:
    """Build a fake googleapiclient.errors.HttpError carrying a status code.

    The stubbed HttpError class in conftest is a bare Exception subclass, so
    we manually attach a ``resp`` attribute with ``.status`` to match how the
    real client exposes the HTTP status.
    """
    from googleapiclient.errors import HttpError

    err = HttpError("fake")
    err.resp = MagicMock()
    err.resp.status = status
    return err


def _fake_groups(course_completed_for: set[int] | None = None) -> dict:
    """Build a fake GROUPS dict for the validation tests.

    Two active groups (3, 4) with main + analysis folder IDs, plus group 1
    flagged as completed so we can prove it gets skipped.
    """
    course_completed_for = course_completed_for or {1}
    return {
        1: {
            "name": "Group #1 (done)",
            "drive_folder_id": "g1_main",
            "analysis_folder_id": "g1_analysis",
            "course_completed": 1 in course_completed_for,
        },
        3: {
            "name": "Group #3",
            "drive_folder_id": "g3_main_folder_id_xxxxx",
            "analysis_folder_id": "g3_analysis_folder_id_yyyyy",
            "course_completed": 3 in course_completed_for,
        },
        4: {
            "name": "Group #4",
            "drive_folder_id": "g4_main_folder_id_aaaaa",
            "analysis_folder_id": "g4_analysis_folder_id_bbbbb",
            "course_completed": 4 in course_completed_for,
        },
    }


# ---------------------------------------------------------------------------
# _validate_drive_folders
# ---------------------------------------------------------------------------


class TestValidateDriveFolders:
    def test_all_ok(self, monkeypatch):
        """Every active group's main + analysis folders return 200."""
        groups = _fake_groups(course_completed_for={1})

        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "x", "name": "ok-folder",
        }

        monkeypatch.setattr(
            "tools.integrations.gdrive_manager.get_drive_service",
            lambda: service,
        )
        # Patch GROUPS at the orchestrator's import site (config module).
        monkeypatch.setattr("tools.core.config.GROUPS", groups)
        # Speed up the polite sleep between calls.
        monkeypatch.setattr(orch.time, "sleep", lambda *_: None)

        alert_mock = MagicMock()
        monkeypatch.setattr(
            "tools.integrations.whatsapp_sender.alert_operator", alert_mock,
        )

        results = orch._validate_drive_folders()

        assert 1 not in results, "completed group must be skipped"
        assert results[3] == {"main": "ok", "analysis": "ok"}
        assert results[4] == {"main": "ok", "analysis": "ok"}
        alert_mock.assert_not_called()

    def test_404_returns_missing_and_alerts(self, monkeypatch):
        """A 404 from Drive marks the folder 'missing' and alerts operator."""
        groups = _fake_groups(course_completed_for={1, 4})

        service = MagicMock()
        service.files.return_value.get.return_value.execute.side_effect = (
            _make_http_error(404)
        )

        monkeypatch.setattr(
            "tools.integrations.gdrive_manager.get_drive_service",
            lambda: service,
        )
        monkeypatch.setattr("tools.core.config.GROUPS", groups)
        monkeypatch.setattr(orch.time, "sleep", lambda *_: None)

        alert_mock = MagicMock()
        monkeypatch.setattr(
            "tools.integrations.whatsapp_sender.alert_operator", alert_mock,
        )

        results = orch._validate_drive_folders()

        assert results[3]["main"] == "missing"
        assert results[3]["analysis"] == "missing"
        assert alert_mock.call_count == 2  # one per failing folder
        # Verify the alert message format includes group + label.
        call_text = alert_mock.call_args_list[0][0][0]
        assert "group=3" in call_text
        assert "status=404" in call_text

    def test_403_returns_forbidden_and_alerts(self, monkeypatch):
        """A 403 from Drive marks the folder 'forbidden' and alerts."""
        groups = _fake_groups(course_completed_for={1, 4})

        service = MagicMock()
        service.files.return_value.get.return_value.execute.side_effect = (
            _make_http_error(403)
        )

        monkeypatch.setattr(
            "tools.integrations.gdrive_manager.get_drive_service",
            lambda: service,
        )
        monkeypatch.setattr("tools.core.config.GROUPS", groups)
        monkeypatch.setattr(orch.time, "sleep", lambda *_: None)

        alert_mock = MagicMock()
        monkeypatch.setattr(
            "tools.integrations.whatsapp_sender.alert_operator", alert_mock,
        )

        results = orch._validate_drive_folders()

        assert results[3]["main"] == "forbidden"
        assert alert_mock.called

    def test_skips_completed_groups(self, monkeypatch):
        """Groups with course_completed=True are not probed at all."""
        # All three groups completed → nothing should be in results.
        groups = _fake_groups(course_completed_for={1, 3, 4})

        service = MagicMock()
        monkeypatch.setattr(
            "tools.integrations.gdrive_manager.get_drive_service",
            lambda: service,
        )
        monkeypatch.setattr("tools.core.config.GROUPS", groups)
        monkeypatch.setattr(orch.time, "sleep", lambda *_: None)

        results = orch._validate_drive_folders()

        assert results == {}
        service.files.return_value.get.assert_not_called()

    def test_not_configured_when_id_empty(self, monkeypatch):
        """Empty folder_id strings produce 'not_configured' (not an API call)."""
        groups = {
            3: {
                "name": "Group #3",
                "drive_folder_id": "",  # not configured
                "analysis_folder_id": "g3_analysis",
                "course_completed": False,
            },
        }

        service = MagicMock()
        service.files.return_value.get.return_value.execute.return_value = {
            "id": "x", "name": "ok",
        }

        monkeypatch.setattr(
            "tools.integrations.gdrive_manager.get_drive_service",
            lambda: service,
        )
        monkeypatch.setattr("tools.core.config.GROUPS", groups)
        monkeypatch.setattr(orch.time, "sleep", lambda *_: None)

        results = orch._validate_drive_folders()

        assert results[3]["main"] == "not_configured"
        assert results[3]["analysis"] == "ok"


# ---------------------------------------------------------------------------
# _probe_google_token
# ---------------------------------------------------------------------------


class TestProbeGoogleToken:
    def test_skipped_when_not_railway(self, monkeypatch):
        """Local dev (IS_RAILWAY=False) returns status='skipped'."""
        monkeypatch.setattr(orch, "IS_RAILWAY", False)

        result = orch._probe_google_token()

        assert result["status"] == "skipped"
        assert result["gated"] is True

    def test_success(self, monkeypatch):
        """files().list() succeeds → status='ok'."""
        monkeypatch.setattr(orch, "IS_RAILWAY", True)

        service = MagicMock()
        service.files.return_value.list.return_value.execute.return_value = {
            "files": []
        }
        monkeypatch.setattr(
            "tools.integrations.gdrive_manager.get_drive_service",
            lambda: service,
        )

        alert_mock = MagicMock()
        monkeypatch.setattr(
            "tools.integrations.whatsapp_sender.alert_operator", alert_mock,
        )

        result = orch._probe_google_token()

        assert result["status"] == "ok"
        assert result["gated"] is False
        alert_mock.assert_not_called()

    def test_invalid_grant_alerts_in_georgian(self, monkeypatch):
        """An invalid_grant RefreshError fires a Georgian operator alert."""
        monkeypatch.setattr(orch, "IS_RAILWAY", True)

        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = (
            Exception("invalid_grant: Token has been expired or revoked.")
        )
        monkeypatch.setattr(
            "tools.integrations.gdrive_manager.get_drive_service",
            lambda: service,
        )

        alert_mock = MagicMock()
        monkeypatch.setattr(
            "tools.integrations.whatsapp_sender.alert_operator", alert_mock,
        )

        result = orch._probe_google_token()

        assert result["status"] == "invalid_grant"
        assert alert_mock.called
        msg = alert_mock.call_args[0][0]
        # Must be in Georgian — check for the key phrase asking operator to
        # update authorization on the Google console.
        assert "Google OAuth" in msg
        assert "console.cloud.google.com" in msg

    def test_refresh_error_class_name(self, monkeypatch):
        """A RefreshError-named exception is treated as invalid_grant."""
        monkeypatch.setattr(orch, "IS_RAILWAY", True)

        class RefreshError(Exception):
            pass

        service = MagicMock()
        service.files.return_value.list.return_value.execute.side_effect = (
            RefreshError("token unusable")
        )
        monkeypatch.setattr(
            "tools.integrations.gdrive_manager.get_drive_service",
            lambda: service,
        )

        alert_mock = MagicMock()
        monkeypatch.setattr(
            "tools.integrations.whatsapp_sender.alert_operator", alert_mock,
        )

        result = orch._probe_google_token()

        assert result["status"] == "invalid_grant"
        assert alert_mock.called
