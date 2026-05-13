"""Tests for edit_message_with_verification and _delete_and_resend.

All Green API HTTP calls are mocked — no network traffic is made.

Run with:
    pytest tools/tests/test_whatsapp_edit_message.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call, patch

import pytest

import tools.integrations.whatsapp_sender as ws
from tools.integrations.whatsapp_sender import (
    EDIT_SAFE_WINDOW_MINUTES,
    WHATSAPP_EDIT_VERIFY_DELAY_SECONDS,
    EditResult,
    edit_message_with_verification,
)

# ---------------------------------------------------------------------------
# Shared test fixtures / helpers
# ---------------------------------------------------------------------------

CHAT_ID = "120363000000000001@g.us"
MSG_ID = "BAE5D1234567890A"
NEW_TEXT = "Corrected message text"
OLD_TEXT = "Original typo message"


def _history_with_new_text() -> list[dict]:
    """Simulate getChatHistory returning the message with the corrected text."""
    return [
        {"idMessage": MSG_ID, "textMessage": NEW_TEXT, "type": "outgoing"},
        {"idMessage": "BAE5AAAA", "textMessage": "earlier message", "type": "incoming"},
    ]


def _history_with_old_text() -> list[dict]:
    """Simulate getChatHistory returning the message still showing the old text."""
    return [
        {"idMessage": MSG_ID, "textMessage": OLD_TEXT, "type": "outgoing"},
    ]


def _history_without_message() -> list[dict]:
    """Simulate getChatHistory where the original message is no longer present."""
    return [
        {"idMessage": "BAE5BBBB", "textMessage": "some other message", "type": "incoming"},
    ]


def _ok_edit_response() -> dict:
    return {"idMessage": "BAE5EDIT001"}


def _ok_resend_response() -> dict:
    return {"idMessage": "BAE5RESEND001"}


# ---------------------------------------------------------------------------
# 1. Edit within window — edit verified as landed in chat
# ---------------------------------------------------------------------------


class TestEditWithinWindowVerifiedSuccess:
    """editMessage returns 200 AND chat history confirms the new text."""

    def test_returns_success_with_method_edited(self):
        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", return_value=_ok_edit_response()) as mock_raw,
            patch.object(ws, "get_chat_history", return_value=_history_with_new_text()),
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)

        assert result.success is True
        assert result.method == "edited"
        assert result.new_id_message == "BAE5EDIT001"
        assert result.error is None

        # Only editMessage should have been called (no delete, no resend)
        mock_raw.assert_called_once()
        called_method = mock_raw.call_args[0][0]
        assert called_method == "editMessage"

    def test_sleep_called_with_configured_delay(self):
        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", return_value=_ok_edit_response()),
            patch.object(ws, "get_chat_history", return_value=_history_with_new_text()),
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)

        mock_time.sleep.assert_called_once_with(WHATSAPP_EDIT_VERIFY_DELAY_SECONDS)


# ---------------------------------------------------------------------------
# 2. Edit within window — chat still shows old text (silent failure)
# ---------------------------------------------------------------------------


class TestEditWithinWindowSilentFailureFallsBack:
    """editMessage returns 200 but getChatHistory still shows old text.

    Expected: fall back to deleteMessage + sendMessage.
    """

    def test_calls_delete_and_resend(self):
        delete_calls = []
        send_calls = []

        def fake_send_request_raw(method, payload, purpose):
            if method == "editMessage":
                return _ok_edit_response()
            if method == "deleteMessage":
                delete_calls.append(payload)
                return {}
            raise AssertionError(f"Unexpected method: {method}")

        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", side_effect=fake_send_request_raw),
            patch.object(ws, "get_chat_history", return_value=_history_with_old_text()),
            patch.object(ws, "send_message_to_chat", return_value=_ok_resend_response()) as mock_send,
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)
            send_calls = mock_send.call_args_list

        assert result.method == "deleted_and_resent"
        assert result.success is True
        assert result.new_id_message == "BAE5RESEND001"

        # deleteMessage was called with the original id
        assert len(delete_calls) == 1
        assert delete_calls[0]["idMessage"] == MSG_ID
        assert delete_calls[0]["onlySenderDelete"] is False

        # sendMessage (via send_message_to_chat) was called with the corrected text
        assert len(send_calls) == 1
        assert send_calls[0] == call(CHAT_ID, NEW_TEXT)

    def test_result_method_is_deleted_and_resent(self):
        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", return_value=_ok_edit_response()),
            patch.object(ws, "get_chat_history", return_value=_history_with_old_text()),
            patch.object(ws, "send_message_to_chat", return_value=_ok_resend_response()),
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)

        assert result.method == "deleted_and_resent"


# ---------------------------------------------------------------------------
# 3. Outside 15-minute window — goes direct to delete+resend
# ---------------------------------------------------------------------------


class TestEditOutside15MinWindowGoesDirect:
    """When sent_at indicates message is older than EDIT_SAFE_WINDOW_MINUTES,
    editMessage must NOT be called at all.
    """

    def test_edit_message_not_called(self):
        sent_at = datetime.now(tz=timezone.utc) - timedelta(minutes=20)

        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw") as mock_raw,
            patch.object(ws, "send_message_to_chat", return_value=_ok_resend_response()),
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT, sent_at=sent_at)

        # editMessage must not appear in any call
        for call_args in mock_raw.call_args_list:
            assert call_args[0][0] != "editMessage", "editMessage was called despite being outside window"

        assert result.method == "deleted_and_resent"

    def test_delete_and_send_are_called(self):
        sent_at = datetime.now(tz=timezone.utc) - timedelta(minutes=20)

        delete_called = []

        def fake_raw(method, payload, purpose):
            if method == "deleteMessage":
                delete_called.append(True)
                return {}
            raise AssertionError(f"Unexpected _send_request_raw call: {method}")

        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", side_effect=fake_raw),
            patch.object(ws, "send_message_to_chat", return_value=_ok_resend_response()) as mock_send,
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT, sent_at=sent_at)

        assert delete_called, "deleteMessage was not called"
        mock_send.assert_called_once_with(CHAT_ID, NEW_TEXT)

    def test_exactly_at_boundary_still_attempts_edit(self):
        """A message sent exactly EDIT_SAFE_WINDOW_MINUTES ago is still within window."""
        sent_at = datetime.now(tz=timezone.utc) - timedelta(minutes=EDIT_SAFE_WINDOW_MINUTES - 1)

        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", return_value=_ok_edit_response()) as mock_raw,
            patch.object(ws, "get_chat_history", return_value=_history_with_new_text()),
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT, sent_at=sent_at)

        # editMessage should have been attempted
        called_methods = [c[0][0] for c in mock_raw.call_args_list]
        assert "editMessage" in called_methods
        assert result.method == "edited"


# ---------------------------------------------------------------------------
# 4. Message not found in history — falls back
# ---------------------------------------------------------------------------


class TestEditMessageNotFoundInHistory:
    """getChatHistory returns no entry matching id_message.

    The original may have been deleted already, or pagination cut it off.
    Either way we cannot verify — fall back to delete+resend.
    """

    def test_falls_back_when_message_absent(self):
        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", return_value=_ok_edit_response()),
            patch.object(ws, "get_chat_history", return_value=_history_without_message()),
            patch.object(ws, "send_message_to_chat", return_value=_ok_resend_response()) as mock_send,
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)

        assert result.method == "deleted_and_resent"
        mock_send.assert_called_once_with(CHAT_ID, NEW_TEXT)

    def test_falls_back_when_history_empty(self):
        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", return_value=_ok_edit_response()),
            patch.object(ws, "get_chat_history", return_value=[]),
            patch.object(ws, "send_message_to_chat", return_value=_ok_resend_response()),
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)

        assert result.method == "deleted_and_resent"


# ---------------------------------------------------------------------------
# 5. editMessage HTTP error
# ---------------------------------------------------------------------------


class TestEditMessageHttpError:
    """When Green API returns a non-200 status for editMessage."""

    def test_returns_failure_result(self):
        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", side_effect=RuntimeError("HTTP 500: Internal Server Error")),
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)

        assert result.success is False
        assert result.method == "edited"
        assert result.new_id_message is None
        assert result.error is not None
        assert "500" in result.error or "HTTP" in result.error

    def test_does_not_call_fallback_on_http_error(self):
        """An HTTP error at the network level is reported as a failure, not a fallback."""
        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", side_effect=RuntimeError("HTTP 500")),
            patch.object(ws, "send_message_to_chat") as mock_send,
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)

        mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Delete failure during fallback still attempts resend
# ---------------------------------------------------------------------------


class TestDeleteFailureDuringFallbackStillResends:
    """If deleteMessage fails (e.g. message too old), resend should still happen."""

    def test_resend_succeeds_despite_delete_error(self):
        def fake_raw(method, payload, purpose):
            if method == "editMessage":
                return _ok_edit_response()
            if method == "deleteMessage":
                raise RuntimeError("HTTP 400: message too old to delete")
            raise AssertionError(f"Unexpected method: {method}")

        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", side_effect=fake_raw),
            patch.object(ws, "get_chat_history", return_value=_history_with_old_text()),
            patch.object(ws, "send_message_to_chat", return_value=_ok_resend_response()) as mock_send,
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)

        # Resend was called regardless of delete failure
        mock_send.assert_called_once_with(CHAT_ID, NEW_TEXT)
        assert result.success is True
        assert result.method == "deleted_and_resent"
        assert result.new_id_message == "BAE5RESEND001"

    def test_result_still_has_new_id_message(self):
        def fake_raw(method, payload, purpose):
            if method == "editMessage":
                return _ok_edit_response()
            if method == "deleteMessage":
                raise RuntimeError("Cannot delete")
            raise AssertionError(f"Unexpected: {method}")

        with (
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"),
            patch.object(ws, "GREEN_API_TOKEN", "tok"),
            patch.object(ws, "_send_request_raw", side_effect=fake_raw),
            patch.object(ws, "get_chat_history", return_value=_history_with_old_text()),
            patch.object(ws, "send_message_to_chat", return_value={"idMessage": "BAE5NEW999"}),
            patch.object(ws, "time") as mock_time,
        ):
            mock_time.sleep = MagicMock()
            result = edit_message_with_verification(CHAT_ID, MSG_ID, NEW_TEXT)

        assert result.new_id_message == "BAE5NEW999"


# ---------------------------------------------------------------------------
# 7. EditResult dataclass
# ---------------------------------------------------------------------------


class TestEditResultDataclass:
    def test_frozen_immutable(self):
        result = EditResult(success=True, method="edited", new_id_message="abc", error=None)
        with pytest.raises((AttributeError, TypeError)):
            result.success = False  # type: ignore[misc]

    def test_fields_accessible(self):
        result = EditResult(success=False, method="skipped_too_old", new_id_message=None, error="too old")
        assert result.success is False
        assert result.method == "skipped_too_old"
        assert result.new_id_message is None
        assert result.error == "too old"


# ---------------------------------------------------------------------------
# 8. Import sanity check
# ---------------------------------------------------------------------------


def test_public_exports_importable():
    """Verify that the new symbols are importable from the module."""
    assert callable(ws.edit_message_with_verification)
    assert ws.EditResult is EditResult
    assert isinstance(ws.EDIT_SAFE_WINDOW_MINUTES, int)
    assert isinstance(ws.WHATSAPP_EDIT_VERIFY_DELAY_SECONDS, (int, float))
