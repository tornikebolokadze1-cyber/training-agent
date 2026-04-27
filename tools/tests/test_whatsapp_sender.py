"""Unit tests for tools/whatsapp_sender.py.

Covers:
- _base_url construction
- _split_message chunking logic
- _send_request retry behavior, auth errors, network errors
- _send_request response validation (idMessage check)
- send_message_to_chat chunked delivery
- send_group_reminder message formatting + missing group ID
- send_group_upload_notification with and without group ID
- send_private_report validation
- alert_operator never-raise guarantee + file fallback
- configure_webhook / get_webhook_settings / list_groups API calls
- Rate limiter (sliding window, wait behavior)
- Notification DLQ (enqueue, process, dead letter to file)
- WhatsAppSendError for silent failures

Run with:
    pytest tools/tests/test_whatsapp_sender.py -v
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.integrations.whatsapp_sender as ws

# ===========================================================================
# 1. _base_url
# ===========================================================================


class TestBaseUrl:
    def test_contains_instance_id(self):
        with patch.object(ws, "GREEN_API_INSTANCE_ID", "12345"):
            assert "waInstance12345" in ws._base_url()

    def test_returns_https_url(self):
        with patch.object(ws, "GREEN_API_INSTANCE_ID", "99"):
            url = ws._base_url()
            assert url.startswith("https://")


# ===========================================================================
# 2. _split_message — pure logic, no mocking needed
# ===========================================================================


class TestSplitMessage:
    def test_short_message_returns_single_chunk(self):
        result = ws._split_message("Hello")
        assert result == ["Hello"]

    def test_exact_limit_returns_single_chunk(self):
        text = "a" * ws.MESSAGE_MAX_LENGTH
        result = ws._split_message(text)
        assert len(result) == 1

    def test_long_message_splits_into_multiple_chunks(self):
        text = "word " * 2000  # ~10000 chars, well over 4096
        result = ws._split_message(text)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= ws.MESSAGE_MAX_LENGTH

    def test_splits_on_double_newline_when_possible(self):
        # Build text with a double newline at a good split point
        part1 = "a" * 2000
        part2 = "b" * 2000
        part3 = "c" * 2000
        text = part1 + "\n\n" + part2 + "\n\n" + part3
        result = ws._split_message(text)
        assert len(result) >= 2

    def test_empty_message_returns_single_chunk(self):
        result = ws._split_message("")
        assert result == [""]


# ===========================================================================
# 3. _send_request — retry logic and error handling
# ===========================================================================


class TestSendRequestRaw:
    """Tests for _send_request_raw (no validation, no rate limiting)."""

    def setup_method(self):
        self._patches = [
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst-1"),
            patch.object(ws, "GREEN_API_TOKEN", "tok-1"),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()

    def test_success_on_first_attempt(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"idMessage": "msg-123"}

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch("tools.integrations.whatsapp_sender.httpx.Client", return_value=mock_client):
            result = ws._send_request_raw("sendMessage", {"chatId": "x"}, "test")

        assert result == {"idMessage": "msg-123"}

    def test_raises_value_error_when_not_configured(self):
        with patch.object(ws, "GREEN_API_INSTANCE_ID", ""), \
             patch.object(ws, "GREEN_API_TOKEN", ""):
            with pytest.raises(ValueError, match="not configured"):
                ws._send_request_raw("sendMessage", {}, "test")

    def test_client_error_raises_immediately(self):
        """4xx errors (except 429) should not be retried."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch("tools.integrations.whatsapp_sender.httpx.Client", return_value=mock_client):
            with pytest.raises(RuntimeError, match="HTTP 400"):
                ws._send_request_raw("sendMessage", {}, "test")

        # Should only attempt once for client errors
        assert mock_client.post.call_count == 1

    def test_network_error_retries_then_fails(self):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = RuntimeError("Network down")

        with patch("tools.integrations.whatsapp_sender.httpx.Client", return_value=mock_client), \
             patch("tools.core.retry.time.sleep"):
            with pytest.raises(RuntimeError, match="Network down"):
                ws._send_request_raw("sendMessage", {}, "test send")

        assert mock_client.post.call_count == ws.MAX_RETRIES

    def test_server_error_retries(self):
        """5xx errors should be retried."""
        fail_response = MagicMock()
        fail_response.status_code = 500
        fail_response.text = "Internal Server Error"

        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.json.return_value = {"idMessage": "ok"}

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = [fail_response, ok_response]

        with patch("tools.integrations.whatsapp_sender.httpx.Client", return_value=mock_client), \
             patch("tools.core.retry.time.sleep"):
            result = ws._send_request_raw("sendMessage", {}, "test")

        assert result == {"idMessage": "ok"}
        assert mock_client.post.call_count == 2


# ===========================================================================
# 4. send_message_to_chat — chunked delivery
# ===========================================================================


class TestSendMessageToChat:
    @pytest.fixture(autouse=True)
    def _reset_rate_limiter(self):
        """Reset rate limiter state between tests."""
        ws._rate_limiter._timestamps.clear()

    def test_short_message_single_call(self):
        with patch.object(ws, "_send_request", return_value={"idMessage": "m1"}) as mock_send:
            result = ws.send_message_to_chat("chat@c.us", "hello")

        mock_send.assert_called_once()
        assert result == {"idMessage": "m1"}

    def test_long_message_multiple_calls(self):
        long_text = "word " * 2000
        call_count = [0]

        def fake_send(method, payload, purpose):
            call_count[0] += 1
            return {"idMessage": f"m{call_count[0]}"}

        with patch.object(ws, "_send_request", side_effect=fake_send), \
             patch("tools.integrations.whatsapp_sender.time.sleep"):
            ws.send_message_to_chat("chat@c.us", long_text)

        assert call_count[0] > 1


# ===========================================================================
# 5. send_group_reminder
# ===========================================================================


class TestSendGroupReminder:
    """Test send_group_reminder by calling it directly.

    When the full suite runs, conftest stubs httpx before this module is imported.
    send_group_reminder is defined at module level so it exists regardless of httpx.
    We verify it by checking if the attribute exists; if not (import ordering issue),
    we skip gracefully.
    """

    @pytest.fixture(autouse=True)
    def _check_function_exists(self):
        if not hasattr(ws, "send_group_reminder"):
            pytest.skip("send_group_reminder not available (import ordering issue)")

    def test_sends_formatted_message(self):
        with patch.object(ws, "send_message_to_chat", return_value={"idMessage": "ok"}) as mock_send, \
             patch.object(ws, "_GROUP_CHAT_IDS", {1: "group1@g.us"}), \
             patch.object(ws, "GROUPS", {1: {"name": "ჯგუფი #1"}}):

            result = ws.send_group_reminder(1, "https://zoom.us/j/123", 5)

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        assert "ლექცია #5" in msg
        assert "https://zoom.us/j/123" in msg
        assert result == {"idMessage": "ok"}

    def test_raises_for_missing_group_id(self):
        with patch.object(ws, "_GROUP_CHAT_IDS", {}), \
             patch.object(ws, "GROUPS", {3: {"name": "test"}}):
            with pytest.raises(ValueError, match="No WhatsApp group ID"):
                ws.send_group_reminder(3, "https://zoom.us", 1)


# ===========================================================================
# 6. send_group_upload_notification
# ===========================================================================


class TestSendGroupUploadNotification:
    def test_sends_to_group_chat(self):
        with patch.object(ws, "send_message_to_chat", return_value={"ok": True}) as mock_send, \
             patch.object(ws, "_GROUP_CHAT_IDS", {1: "grp@g.us"}), \
             patch.object(ws, "GROUPS", {1: {"name": "ჯგუფი #1"}}):

            ws.send_group_upload_notification(1, 3, "https://drive/rec", "https://drive/sum")

        msg = mock_send.call_args[0][1]
        assert "ლექცია #3" in msg
        assert "https://drive/rec" in msg
        assert "https://drive/sum" in msg

    def test_falls_back_to_tornike_when_no_group_id(self):
        with patch.object(ws, "send_message_to_chat", return_value={}) as mock_send, \
             patch.object(ws, "_GROUP_CHAT_IDS", {}), \
             patch.object(ws, "GROUPS", {1: {"name": "Test"}}), \
             patch.object(ws, "WHATSAPP_TORNIKE_PHONE", "995555"):

            ws.send_group_upload_notification(1, 1, "rec_url", "sum_url")

        chat_id = mock_send.call_args[0][0]
        assert chat_id == "995555@c.us"


# ===========================================================================
# 7. send_private_report
# ===========================================================================


class TestSendPrivateReport:
    def test_sends_to_tornike(self):
        with patch.object(ws, "send_message_to_chat", return_value={"ok": True}) as mock_send, \
             patch.object(ws, "WHATSAPP_TORNIKE_PHONE", "995123"):

            ws.send_private_report("gap analysis text")

        mock_send.assert_called_once_with("995123@c.us", "gap analysis text")

    def test_raises_when_phone_not_configured(self):
        with patch.object(ws, "WHATSAPP_TORNIKE_PHONE", ""):
            with pytest.raises(ValueError, match="not configured"):
                ws.send_private_report("text")


# ===========================================================================
# 8. alert_operator — must never raise
# ===========================================================================


class TestAlertOperator:
    def test_sends_alert_via_whatsapp(self):
        with patch.object(ws, "send_message_to_chat") as mock_send, \
             patch.object(ws, "WHATSAPP_TORNIKE_PHONE", "995111"), \
             patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"), \
             patch.object(ws, "GREEN_API_TOKEN", "tok"):

            ws.alert_operator("Server down!")

        mock_send.assert_called_once()
        msg = mock_send.call_args[0][1]
        assert "Server down!" in msg
        assert "ALERT" in msg

    def test_never_raises_even_on_send_failure(self):
        with patch.object(ws, "send_message_to_chat", side_effect=Exception("boom")), \
             patch.object(ws, "WHATSAPP_TORNIKE_PHONE", "995"), \
             patch.object(ws, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(ws, "GREEN_API_TOKEN", "t"), \
             patch.object(ws, "_save_missed_alert") as mock_save:

            # Must not raise
            ws.alert_operator("Critical error")

        # Should save to file as fallback
        mock_save.assert_called_once()
        entry = mock_save.call_args[0][0]
        assert entry.priority == "alert"

    def test_falls_back_to_logging_when_not_configured(self):
        with patch.object(ws, "WHATSAPP_TORNIKE_PHONE", ""), \
             patch.object(ws, "GREEN_API_INSTANCE_ID", ""), \
             patch.object(ws, "GREEN_API_TOKEN", ""), \
             patch.object(ws, "_save_missed_alert"):

            # Must not raise — just logs + saves to file
            ws.alert_operator("No WhatsApp config")

    def test_never_raises_even_when_everything_fails(self):
        """Even if file save fails, alert_operator must not raise."""
        with patch.object(ws, "send_message_to_chat", side_effect=Exception("whatsapp down")), \
             patch.object(ws, "WHATSAPP_TORNIKE_PHONE", "995"), \
             patch.object(ws, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(ws, "GREEN_API_TOKEN", "t"), \
             patch.object(ws, "_save_missed_alert", side_effect=Exception("disk full")):

            # Must STILL not raise
            ws.alert_operator("Total failure scenario")

    def test_saves_alert_to_missed_alerts_file(self, tmp_path):
        """Verify alert is saved to missed_alerts.json when WhatsApp fails."""
        alerts_file = tmp_path / "missed_alerts.json"

        with patch.object(ws, "send_message_to_chat", side_effect=Exception("fail")), \
             patch.object(ws, "WHATSAPP_TORNIKE_PHONE", "995"), \
             patch.object(ws, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(ws, "GREEN_API_TOKEN", "t"), \
             patch.object(ws, "MISSED_ALERTS_PATH", alerts_file):

            ws.alert_operator("Test alert message")

        assert alerts_file.exists()
        data = json.loads(alerts_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert "Test alert message" in data[0]["message"]
        assert data[0]["priority"] == "alert"


# ===========================================================================
# 9. configure_webhook
# ===========================================================================


class TestConfigureWebhook:
    def test_raises_when_not_configured(self):
        with patch.object(ws, "GREEN_API_INSTANCE_ID", ""), \
             patch.object(ws, "GREEN_API_TOKEN", ""):
            with pytest.raises(ValueError, match="not configured"):
                ws.configure_webhook("https://example.com/hook")

    def test_sends_settings_to_api(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"saveSettings": True}
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response

        with patch.object(ws, "GREEN_API_INSTANCE_ID", "inst"), \
             patch.object(ws, "GREEN_API_TOKEN", "tok"), \
             patch.object(ws, "WEBHOOK_SECRET", "secret"), \
             patch("tools.integrations.whatsapp_sender.httpx.Client", return_value=mock_client):

            result = ws.configure_webhook("https://example.com/hook")

        assert result == {"saveSettings": True}
        payload = mock_client.post.call_args[1]["json"]
        assert payload["webhookUrl"] == "https://example.com/hook"
        assert "Bearer secret" in payload["webhookUrlToken"]


# ===========================================================================
# 10. get_webhook_settings
# ===========================================================================


class TestGetWebhookSettings:
    def test_raises_when_not_configured(self):
        with patch.object(ws, "GREEN_API_INSTANCE_ID", ""), \
             patch.object(ws, "GREEN_API_TOKEN", ""):
            with pytest.raises(ValueError):
                ws.get_webhook_settings()

    def test_returns_settings(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"webhookUrl": "https://hook"}
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response

        with patch.object(ws, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(ws, "GREEN_API_TOKEN", "t"), \
             patch("tools.integrations.whatsapp_sender.httpx.Client", return_value=mock_client):

            result = ws.get_webhook_settings()

        assert result["webhookUrl"] == "https://hook"


# ===========================================================================
# 11. list_groups
# ===========================================================================


class TestListGroups:
    def test_filters_only_group_chats(self):
        contacts = [
            {"id": "group1@g.us", "name": "Group 1"},
            {"id": "person@c.us", "name": "Person"},
            {"id": "group2@g.us", "name": "Group 2"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = contacts
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response

        with patch.object(ws, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(ws, "GREEN_API_TOKEN", "t"), \
             patch("tools.integrations.whatsapp_sender.httpx.Client", return_value=mock_client):

            groups = ws.list_groups()

        assert len(groups) == 2
        assert all(g["id"].endswith("@g.us") for g in groups)


# ===========================================================================
# 12. Response validation — _validate_send_response
# ===========================================================================


class TestValidateSendResponse:
    def test_valid_response_passes(self):
        # Should not raise
        ws._validate_send_response({"idMessage": "abc123"}, "test")

    def test_missing_id_message_raises(self):
        with pytest.raises(ws.WhatsAppSendError, match="no idMessage"):
            ws._validate_send_response({}, "test send")

    def test_none_id_message_raises(self):
        with pytest.raises(ws.WhatsAppSendError, match="no idMessage"):
            ws._validate_send_response({"idMessage": None}, "test send")

    def test_empty_string_id_message_raises(self):
        with pytest.raises(ws.WhatsAppSendError, match="no idMessage"):
            ws._validate_send_response({"idMessage": ""}, "test send")


# ===========================================================================
# 13. _send_request (with validation + rate limiting)
# ===========================================================================


class TestSendRequestWithValidation:
    """Tests for the validating _send_request wrapper."""

    @pytest.fixture(autouse=True)
    def _reset_rate_limiter(self):
        ws._rate_limiter._timestamps.clear()

    def setup_method(self):
        self._patches = [
            patch.object(ws, "GREEN_API_INSTANCE_ID", "inst-1"),
            patch.object(ws, "GREEN_API_TOKEN", "tok-1"),
        ]
        for p in self._patches:
            p.start()

    def teardown_method(self):
        for p in self._patches:
            p.stop()

    def test_valid_response_returns_data(self):
        with patch.object(ws, "_send_request_raw", return_value={"idMessage": "msg-1"}) as mock_raw:
            result = ws._send_request("sendMessage", {"chatId": "x", "message": "hi"}, "test")

        assert result == {"idMessage": "msg-1"}
        mock_raw.assert_called_once()

    def test_missing_id_message_retries_then_enqueues_dlq(self):
        """When idMessage is missing, retry once; if still missing, enqueue to DLQ."""
        with patch.object(ws, "_send_request_raw", return_value={"status": "ok"}) as mock_raw, \
             patch("tools.integrations.whatsapp_sender.time.sleep"), \
             patch.object(ws.notification_dlq, "enqueue") as mock_enqueue:

            with pytest.raises(ws.WhatsAppSendError):
                ws._send_request("sendMessage", {"chatId": "chat@c.us", "message": "hi"}, "test")

        # Should have tried twice (original + validation retry)
        assert mock_raw.call_count == 2
        # Should have enqueued to DLQ
        mock_enqueue.assert_called_once_with("chat@c.us", "hi", priority="notification")

    def test_non_sendmessage_method_skips_validation(self):
        """Non-sendMessage methods (e.g., setSettings) don't need idMessage."""
        with patch.object(ws, "_send_request_raw", return_value={"saveSettings": True}):
            result = ws._send_request("setSettings", {}, "test")

        assert result == {"saveSettings": True}

    def test_missing_id_message_recovers_on_retry(self):
        """If first attempt has no idMessage but retry succeeds, return success."""
        call_count = [0]

        def fake_raw(method, payload, purpose):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"status": "queued"}  # No idMessage
            return {"idMessage": "msg-ok"}  # Success on retry

        with patch.object(ws, "_send_request_raw", side_effect=fake_raw), \
             patch("tools.integrations.whatsapp_sender.time.sleep"):

            result = ws._send_request("sendMessage", {"chatId": "x", "message": "hi"}, "test")

        assert result == {"idMessage": "msg-ok"}
        assert call_count[0] == 2


# ===========================================================================
# 14. Rate limiter
# ===========================================================================


class TestRateLimiter:
    def test_acquire_within_limit(self):
        limiter = ws._RateLimiter(max_messages=5, window=60)
        for _ in range(5):
            assert limiter.acquire() == 0.0

    def test_acquire_over_limit_returns_wait_time(self):
        limiter = ws._RateLimiter(max_messages=2, window=60)
        limiter.acquire()
        limiter.acquire()
        wait = limiter.acquire()
        assert wait > 0.0

    def test_old_timestamps_expire(self):
        limiter = ws._RateLimiter(max_messages=1, window=1)
        limiter._timestamps = [0.0]  # Timestamp in the distant past
        assert limiter.acquire() == 0.0

    def test_wait_and_acquire_blocks_until_available(self):
        limiter = ws._RateLimiter(max_messages=1, window=0.1)
        limiter.acquire()  # First slot taken

        # Second call should block briefly then succeed
        with patch("tools.integrations.whatsapp_sender.time.sleep"):
            # Force timestamps to expire
            limiter._timestamps = [0.0]
            limiter.wait_and_acquire()

    def test_thread_safety(self):
        """Rate limiter should be thread-safe."""
        import threading

        limiter = ws._RateLimiter(max_messages=100, window=60)
        results = []

        def acquire():
            results.append(limiter.acquire())

        threads = [threading.Thread(target=acquire) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should succeed (100 slots, 50 requests)
        assert all(r == 0.0 for r in results)


# ===========================================================================
# 15. Notification DLQ
# ===========================================================================


class TestNotificationDLQ:
    def test_enqueue_and_size(self):
        dlq = ws.NotificationDLQ()
        assert dlq.size == 0
        dlq.enqueue("chat@c.us", "test message", "notification")
        assert dlq.size == 1

    def test_process_sends_queued_messages(self):
        dlq = ws.NotificationDLQ()
        dlq.enqueue("chat@c.us", "hello", "notification")

        with patch.object(ws, "_send_request_raw", return_value={"idMessage": "ok"}), \
             patch.object(ws, "_validate_send_response"), \
             patch.object(ws._rate_limiter, "wait_and_acquire"):

            result = dlq.process()

        assert result["sent"] == 1
        assert result["retrying"] == 0
        assert result["dead"] == 0
        assert dlq.size == 0

    def test_process_requeues_on_failure(self):
        dlq = ws.NotificationDLQ()
        dlq.enqueue("chat@c.us", "hello", "notification")

        with patch.object(ws, "_send_request_raw", side_effect=RuntimeError("fail")), \
             patch.object(ws._rate_limiter, "wait_and_acquire"):

            result = dlq.process()

        assert result["sent"] == 0
        assert result["retrying"] == 1
        assert dlq.size == 1

    def test_process_dead_letters_after_max_retries(self):
        dlq = ws.NotificationDLQ()
        dlq.enqueue("chat@c.us", "hello", "notification")

        # Exhaust retries
        for _ in range(ws.DLQ_MAX_RETRIES):
            with patch.object(ws, "_send_request_raw", side_effect=RuntimeError("fail")), \
                 patch.object(ws._rate_limiter, "wait_and_acquire"), \
                 patch.object(ws, "_save_missed_alert"):
                dlq.process()

        assert dlq.size == 0  # Dead-lettered, not requeued

    def test_process_sorts_by_priority(self):
        dlq = ws.NotificationDLQ()
        dlq.enqueue("chat1@c.us", "low", "notification")
        dlq.enqueue("chat2@c.us", "high", "alert")
        dlq.enqueue("chat3@c.us", "mid", "report")

        send_order = []

        def track_send(method, payload, purpose):
            send_order.append(payload["message"])
            return {"idMessage": "ok"}

        with patch.object(ws, "_send_request_raw", side_effect=track_send), \
             patch.object(ws, "_validate_send_response"), \
             patch.object(ws._rate_limiter, "wait_and_acquire"):

            dlq.process()

        # Alert first, then report, then notification
        assert send_order == ["high", "mid", "low"]

    def test_empty_process_returns_zeros(self):
        dlq = ws.NotificationDLQ()
        result = dlq.process()
        assert result == {"sent": 0, "retrying": 0, "dead": 0}


# ===========================================================================
# 16. _save_missed_alert
# ===========================================================================


class TestSaveMissedAlert:
    def test_creates_file_if_not_exists(self, tmp_path):
        alerts_file = tmp_path / "missed_alerts.json"
        entry = ws._DLQEntry(chat_id="x@c.us", message="test", priority="alert")

        with patch.object(ws, "MISSED_ALERTS_PATH", alerts_file):
            ws._save_missed_alert(entry)

        assert alerts_file.exists()
        data = json.loads(alerts_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["message"] == "test"

    def test_appends_to_existing_file(self, tmp_path):
        alerts_file = tmp_path / "missed_alerts.json"
        alerts_file.write_text(json.dumps([{"message": "old"}]))

        entry = ws._DLQEntry(chat_id="x@c.us", message="new", priority="report")
        with patch.object(ws, "MISSED_ALERTS_PATH", alerts_file):
            ws._save_missed_alert(entry)

        data = json.loads(alerts_file.read_text(encoding="utf-8"))
        assert len(data) == 2
        assert data[1]["message"] == "new"

    def test_handles_corrupted_json(self, tmp_path):
        alerts_file = tmp_path / "missed_alerts.json"
        alerts_file.write_text("not json!!!")

        entry = ws._DLQEntry(chat_id="x@c.us", message="recover", priority="alert")
        with patch.object(ws, "MISSED_ALERTS_PATH", alerts_file):
            ws._save_missed_alert(entry)

        data = json.loads(alerts_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["message"] == "recover"


# ===========================================================================
# 17. DLQEntry serialization
# ===========================================================================


class TestDLQEntry:
    def test_to_dict(self):
        entry = ws._DLQEntry(chat_id="a@c.us", message="hi", priority="alert", attempts=2)
        d = entry.to_dict()
        assert d["chat_id"] == "a@c.us"
        assert d["priority"] == "alert"
        assert d["attempts"] == 2

    def test_from_dict_roundtrip(self):
        original = ws._DLQEntry(chat_id="b@c.us", message="bye", priority="report")
        restored = ws._DLQEntry.from_dict(original.to_dict())
        assert restored.chat_id == original.chat_id
        assert restored.message == original.message
        assert restored.priority == original.priority
