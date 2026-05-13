"""Regression tests for alert_operator deduplication (US-016).

Audit finding: alert_operator() had no rate limiting / dedup. A 50-error
pipeline_retry storm would fire 50 WhatsApp messages to the operator within
60 seconds. We now hash the message body and suppress duplicates within a
300-second window via a bounded OrderedDict.

These tests verify:
1. Identical alerts are suppressed within the window.
2. Different alerts are NOT suppressed.
3. After the window expires, the same alert sends again.
4. _alert_dedup_state size never exceeds _ALERT_DEDUP_MAX_ENTRIES.

Run with:
    pytest tools/tests/test_alert_operator_throttle.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import tools.integrations.whatsapp_sender as ws


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dedup_state():
    """Reset the module-level dedup state before AND after every test.

    This isolates each test — the OrderedDict and suppression counters
    are shared module state.
    """
    with ws._alert_dedup_lock:
        ws._alert_dedup_state.clear()
        ws._alert_suppression_counts.clear()
    yield
    with ws._alert_dedup_lock:
        ws._alert_dedup_state.clear()
        ws._alert_suppression_counts.clear()


@pytest.fixture
def configured_credentials():
    """Patch the module to look 'configured' so alert_operator attempts to send."""
    with patch.object(ws, "WHATSAPP_TORNIKE_PHONE", "995555555555"), \
         patch.object(ws, "GREEN_API_INSTANCE_ID", "12345"), \
         patch.object(ws, "GREEN_API_TOKEN", "tok"):
        yield


# ===========================================================================
# 1. Identical alerts suppressed within window
# ===========================================================================


class TestIdenticalAlertsSuppressed:
    def test_identical_alerts_suppressed_within_window(self, configured_credentials):
        """Send 5 identical messages — only 1 should reach send_message_to_chat."""
        with patch.object(ws, "send_message_to_chat") as mock_send:
            for _ in range(5):
                ws.alert_operator("pipeline_retry storm: 5 errors")

            assert mock_send.call_count == 1, (
                f"Expected 1 send for 5 identical alerts, got {mock_send.call_count}"
            )

    def test_suppression_counter_increments(self, configured_credentials):
        """When suppressed, the per-hash counter should increment."""
        with patch.object(ws, "send_message_to_chat"):
            for _ in range(4):
                ws.alert_operator("identical alert")

        # 1 sent + 3 suppressed → counter should be 3
        assert len(ws._alert_suppression_counts) == 1
        count = list(ws._alert_suppression_counts.values())[0]
        assert count == 3, f"Expected suppression count 3, got {count}"


# ===========================================================================
# 2. Different alerts NOT suppressed
# ===========================================================================


class TestDifferentAlertsNotSuppressed:
    def test_different_alerts_not_suppressed(self, configured_credentials):
        """Send 5 different messages — all 5 should reach send_message_to_chat."""
        with patch.object(ws, "send_message_to_chat") as mock_send:
            for i in range(5):
                ws.alert_operator(f"unique alert #{i}")

            assert mock_send.call_count == 5, (
                f"Expected 5 sends for 5 unique alerts, got {mock_send.call_count}"
            )

    def test_dedup_state_records_all_unique_hashes(self, configured_credentials):
        """Each unique message should leave one entry in _alert_dedup_state."""
        with patch.object(ws, "send_message_to_chat"):
            for i in range(5):
                ws.alert_operator(f"unique alert #{i}")

        assert len(ws._alert_dedup_state) == 5


# ===========================================================================
# 3. Window expiry — same alert sends again
# ===========================================================================


class TestWindowExpiry:
    def test_alert_after_window_expires_sends_again(self, configured_credentials):
        """After 300s window expires, the same message should send again."""
        with patch.object(ws, "send_message_to_chat") as mock_send, \
             patch.object(ws.time, "time") as mock_time:
            # First send at t=1000
            mock_time.return_value = 1000.0
            ws.alert_operator("the same message")
            assert mock_send.call_count == 1

            # Same message at t=1100 (within 300s window) → suppressed
            mock_time.return_value = 1100.0
            ws.alert_operator("the same message")
            assert mock_send.call_count == 1, "Should still be suppressed within window"

            # Same message at t=1301 (just past 300s window) → sends again
            mock_time.return_value = 1301.0
            ws.alert_operator("the same message")
            assert mock_send.call_count == 2, "Should send again after window expires"

    def test_post_window_message_includes_suppression_count(self, configured_credentials):
        """When window expires after N suppressions, the resent message should
        carry the suppressed-count annotation."""
        with patch.object(ws, "send_message_to_chat") as mock_send, \
             patch.object(ws.time, "time") as mock_time:
            mock_time.return_value = 1000.0
            ws.alert_operator("repeated alert")  # send

            mock_time.return_value = 1050.0
            ws.alert_operator("repeated alert")  # suppressed (count=1)
            ws.alert_operator("repeated alert")  # suppressed (count=2)
            ws.alert_operator("repeated alert")  # suppressed (count=3)

            mock_time.return_value = 1500.0  # past 300s window
            ws.alert_operator("repeated alert")  # sends with annotation

            # Last call's full_message argument should mention "+3 duplicates"
            last_call_msg = mock_send.call_args_list[-1][0][1]
            assert "+3 duplicates suppressed" in last_call_msg, (
                f"Expected suppression annotation in resent message, got: {last_call_msg[:200]}"
            )


# ===========================================================================
# 4. State size bounded
# ===========================================================================


class TestStateSizeBounded:
    def test_dedup_state_size_bounded(self, configured_credentials):
        """Send 200 unique messages — state size must never exceed the cap."""
        cap = ws._ALERT_DEDUP_MAX_ENTRIES
        with patch.object(ws, "send_message_to_chat"):
            for i in range(200):
                ws.alert_operator(f"unique alert variant {i}")
                # Check the invariant after every send
                assert len(ws._alert_dedup_state) <= cap, (
                    f"State size {len(ws._alert_dedup_state)} exceeded cap {cap} "
                    f"after sending {i+1} alerts"
                )

        # Final state size should equal the cap exactly
        assert len(ws._alert_dedup_state) == cap

    def test_oldest_entries_evicted_first(self, configured_credentials):
        """Eviction must remove the OLDEST entries (FIFO via OrderedDict)."""
        cap = ws._ALERT_DEDUP_MAX_ENTRIES
        with patch.object(ws, "send_message_to_chat"):
            # Fill to capacity
            for i in range(cap):
                ws.alert_operator(f"msg-{i}")

            # Add one more — oldest ("msg-0") should be evicted
            ws.alert_operator("msg-overflow")

            assert len(ws._alert_dedup_state) == cap
            # The hash of "msg-0" should no longer be present
            import hashlib
            old_hash = hashlib.sha256(b"msg-0").hexdigest()
            assert old_hash not in ws._alert_dedup_state

    def test_suppression_counter_cleared_on_eviction(self, configured_credentials):
        """When a hash is evicted, its suppression counter must also be cleared
        to avoid unbounded growth of _alert_suppression_counts."""
        cap = ws._ALERT_DEDUP_MAX_ENTRIES
        with patch.object(ws, "send_message_to_chat"):
            # Send "msg-0" twice so it has a suppression counter
            ws.alert_operator("msg-0")
            ws.alert_operator("msg-0")  # suppressed

            assert len(ws._alert_suppression_counts) == 1

            # Now fill state past the cap so "msg-0" is evicted
            for i in range(1, cap + 5):
                ws.alert_operator(f"msg-{i}")

            # _alert_suppression_counts should not still hold the evicted hash
            import hashlib
            old_hash = hashlib.sha256(b"msg-0").hexdigest()
            assert old_hash not in ws._alert_suppression_counts
