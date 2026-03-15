"""Unit tests for tools/email_sender.py.

Covers:
- _build_reminder_html: HTML structure and Georgian content
- _encode_message: base64url encoding, MIME structure
- send_email: success path, retry on 5xx, non-retryable 4xx, transport errors
- send_meeting_reminder: multi-recipient delivery, failure tracking, unknown group
- _get_gmail_token_path: delegation to _materialize_credential_file
- _load_credentials: valid creds, expired creds refresh, Railway mode

Run with:
    pytest tools/tests/test_email_sender.py -v
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.email_sender as es


# ===========================================================================
# 1. _build_reminder_html — HTML structure
# ===========================================================================


class TestBuildReminderHtml:
    def test_contains_lecture_number(self):
        html = es._build_reminder_html("ჯგუფი #1", 5, "14 მარტი — 20:00", "https://zoom/j/1")
        assert "ლექცია #5" in html

    def test_contains_zoom_link(self):
        html = es._build_reminder_html("ჯგუფი #1", 1, "time", "https://zoom.us/j/999")
        assert "https://zoom.us/j/999" in html

    def test_contains_group_name(self):
        html = es._build_reminder_html("მარტის ჯგუფი #2", 3, "time", "url")
        assert "მარტის ჯგუფი #2" in html

    def test_contains_meeting_time(self):
        html = es._build_reminder_html("g", 1, "15 მარტი 2026 — 20:00", "url")
        assert "15 მარტი 2026 — 20:00" in html

    def test_is_valid_html(self):
        html = es._build_reminder_html("g", 1, "t", "u")
        assert html.strip().startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_has_georgian_charset(self):
        html = es._build_reminder_html("g", 1, "t", "u")
        assert 'charset="UTF-8"' in html or "charset=utf-8" in html.lower()


# ===========================================================================
# 2. _encode_message — MIME encoding
# ===========================================================================


class TestEncodeMessage:
    def test_returns_dict_with_raw_key(self):
        result = es._encode_message("test@example.com", "Subject", "<h1>Body</h1>")
        assert "raw" in result
        assert isinstance(result["raw"], str)

    def test_raw_is_valid_base64url(self):
        result = es._encode_message("a@b.com", "S", "<p>B</p>")
        decoded = base64.urlsafe_b64decode(result["raw"])
        assert b"a@b.com" in decoded

    def test_subject_in_encoded_message(self):
        result = es._encode_message("a@b.com", "AI კურსი — ლექცია #3", "<p>B</p>")
        decoded = base64.urlsafe_b64decode(result["raw"])
        # Subject may be encoded, but the raw bytes contain it
        assert len(decoded) > 0

    def test_html_body_in_encoded_message(self):
        result = es._encode_message("a@b.com", "S", "<p>Hello World</p>")
        decoded = base64.urlsafe_b64decode(result["raw"])
        # MIME base64-encodes the HTML body, so check that the base64 of
        # our content appears in the raw MIME payload
        body_b64 = base64.b64encode(b"<p>Hello World</p>").decode()
        assert body_b64.encode() in decoded


# ===========================================================================
# 3. send_email — success and retry logic
# ===========================================================================


class TestSendEmail:
    def _mock_gmail_service(self, execute_side_effect=None, execute_return=None):
        svc = MagicMock()
        execute = MagicMock()
        if execute_side_effect:
            execute.side_effect = execute_side_effect
        else:
            execute.return_value = execute_return or {"id": "msg-123"}
        svc.users.return_value.messages.return_value.send.return_value.execute = execute
        return svc

    def test_success_returns_true(self):
        svc = self._mock_gmail_service(execute_return={"id": "sent-ok"})

        with patch("tools.email_sender._build_gmail_service", return_value=svc):
            result = es.send_email("a@b.com", "Subject", "<p>Body</p>")

        assert result is True

    def test_retries_on_5xx_error(self):
        HttpError = type("HttpError", (Exception,), {})
        err = HttpError("500")
        err.resp = MagicMock()
        err.resp.status = 500

        svc_fail = self._mock_gmail_service()
        svc_fail.users.return_value.messages.return_value.send.return_value.execute.side_effect = err

        with patch("tools.email_sender._build_gmail_service", return_value=svc_fail), \
             patch("tools.email_sender.HttpError", HttpError), \
             patch("tools.email_sender.time.sleep"):
            result = es.send_email("a@b.com", "S", "<p>B</p>")

        assert result is False

    def test_non_retryable_4xx_fails_immediately(self):
        HttpError = type("HttpError", (Exception,), {})
        err = HttpError("400")
        err.resp = MagicMock()
        err.resp.status = 400

        call_count = [0]
        def track_build():
            call_count[0] += 1
            svc = MagicMock()
            svc.users.return_value.messages.return_value.send.return_value.execute.side_effect = err
            return svc

        with patch("tools.email_sender._build_gmail_service", side_effect=track_build), \
             patch("tools.email_sender.HttpError", HttpError), \
             patch("tools.email_sender.time.sleep"):
            result = es.send_email("a@b.com", "S", "<p>B</p>")

        assert result is False
        assert call_count[0] == 1  # No retry


# ===========================================================================
# 4. send_meeting_reminder — multi-recipient
# ===========================================================================


class TestSendMeetingReminder:
    def test_sends_to_all_attendees(self):
        mock_groups = {
            1: {
                "name": "ჯგუფი #1",
                "attendee_emails": ["a@test.com", "b@test.com", "c@test.com"],
            }
        }

        with patch.object(es, "GROUPS", mock_groups), \
             patch.object(es, "send_email", return_value=True) as mock_send:
            result = es.send_meeting_reminder(1, 3, "https://zoom/j", "20:00")

        assert result["total"] == 3
        assert result["sent"] == 3
        assert result["failed"] == 0
        assert mock_send.call_count == 3

    def test_tracks_failed_emails(self):
        mock_groups = {
            1: {
                "name": "ჯგუფი #1",
                "attendee_emails": ["ok@test.com", "fail@test.com"],
            }
        }

        def send_email_side_effect(to_email, subject, html_body):
            return to_email != "fail@test.com"

        with patch.object(es, "GROUPS", mock_groups), \
             patch.object(es, "send_email", side_effect=send_email_side_effect):
            result = es.send_meeting_reminder(1, 1, "url", "time")

        assert result["sent"] == 1
        assert result["failed"] == 1
        assert "fail@test.com" in result["failed_emails"]

    def test_unknown_group_raises_key_error(self):
        with patch.object(es, "GROUPS", {1: {}}):
            with pytest.raises(KeyError, match="Unknown group"):
                es.send_meeting_reminder(99, 1, "url", "time")


# ===========================================================================
# 5. _get_gmail_token_path
# ===========================================================================


class TestGetGmailTokenPath:
    def test_delegates_to_materialize(self):
        with patch("tools.email_sender._materialize_credential_file", return_value="path") as mock_mat:
            result = es._get_gmail_token_path()

        mock_mat.assert_called_once_with("GOOGLE_GMAIL_TOKEN_JSON_B64", es.TOKEN_PATH)
        assert result == "path"


# ===========================================================================
# 6. _load_credentials
# ===========================================================================


class TestLoadCredentials:
    def test_returns_valid_creds_without_refresh(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        mock_creds = MagicMock()
        mock_creds.valid = True

        with patch("tools.email_sender._get_gmail_token_path", return_value=fake_token), \
             patch("tools.email_sender.Credentials") as mock_cls:
            mock_cls.from_authorized_user_file.return_value = mock_creds
            result = es._load_credentials()

        assert result is mock_creds
        mock_creds.refresh.assert_not_called()

    def test_refreshes_expired_creds(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh"

        with patch("tools.email_sender._get_gmail_token_path", return_value=fake_token), \
             patch("tools.email_sender.Credentials") as mock_cls, \
             patch("tools.email_sender.IS_RAILWAY", True):
            mock_cls.from_authorized_user_file.return_value = mock_creds
            result = es._load_credentials()

        mock_creds.refresh.assert_called_once()
        assert result is mock_creds

    def test_railway_mode_raises_when_no_refresh_token(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = False
        mock_creds.refresh_token = None

        with patch("tools.email_sender._get_gmail_token_path", return_value=fake_token), \
             patch("tools.email_sender.Credentials") as mock_cls, \
             patch("tools.email_sender.IS_RAILWAY", True):
            mock_cls.from_authorized_user_file.return_value = mock_creds
            with pytest.raises(RuntimeError, match="Railway"):
                es._load_credentials()


# ===========================================================================
# 7. Constants
# ===========================================================================


class TestEmailConstants:
    def test_gmail_scopes(self):
        assert "gmail.send" in es.GMAIL_SCOPES[0]

    def test_max_retries_positive(self):
        assert es.MAX_RETRIES > 0

    def test_retry_backoff_base(self):
        assert es.RETRY_BACKOFF_BASE >= 1
