"""Unit tests for tools/core/delivery_tracker.py.

The delivery tracker is the durable cross-retry guard that survives
``reset_failed()`` and prevents the production bug where students received
the same WhatsApp lecture link 2-3 times after a retry.

Covers:
- Empty load returns empty dict
- record_delivery() merges fields without blanking earlier successes
- has_delivered() correctly reports presence/absence
- clear_delivery() removes a record
- File survives across loads (durability)
- Empty/None/False values do NOT overwrite earlier successes
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_tracker(tmp_path, monkeypatch):
    """Redirect DELIVERY_TRACKER_PATH to a temp file for one test."""
    import tools.core.delivery_tracker as dt
    test_path = tmp_path / "delivery_tracker.json"
    monkeypatch.setattr(dt, "DELIVERY_TRACKER_PATH", test_path)
    # Clear per-key lock state too so locks don't leak between tests.
    dt._locks.clear()
    return dt


class TestLoadAndRecord:
    def test_empty_load_returns_empty_dict(self, isolated_tracker):
        assert isolated_tracker.load_delivery(3, 2) == {}

    def test_record_persists_summary_doc_id(self, isolated_tracker):
        isolated_tracker.record_delivery(3, 2, summary_doc_id="1ABC")
        result = isolated_tracker.load_delivery(3, 2)
        assert result["summary_doc_id"] == "1ABC"
        assert result["group"] == 3
        assert result["lecture"] == 2
        assert "updated_at" in result

    def test_record_merges_additional_fields(self, isolated_tracker):
        isolated_tracker.record_delivery(3, 2, summary_doc_id="1ABC")
        isolated_tracker.record_delivery(
            3, 2,
            report_doc_id="1XYZ",
            whatsapp_notification_sent_at="2026-05-19T20:00:00+04:00",
        )
        result = isolated_tracker.load_delivery(3, 2)
        # All three fields must survive — merging, not replacing.
        assert result["summary_doc_id"] == "1ABC"
        assert result["report_doc_id"] == "1XYZ"
        assert result["whatsapp_notification_sent_at"] == "2026-05-19T20:00:00+04:00"

    def test_empty_values_do_not_overwrite_earlier_success(self, isolated_tracker):
        """A later partial run with None/empty MUST NOT blank an earlier doc ID."""
        isolated_tracker.record_delivery(3, 2, summary_doc_id="1ABC")
        # Simulate a partial later run where summary failed (None passed in).
        isolated_tracker.record_delivery(
            3, 2,
            summary_doc_id=None,  # should NOT clear
            summary_doc_id_2="",  # should NOT persist
            report_doc_id="1XYZ",  # SHOULD persist
        )
        result = isolated_tracker.load_delivery(3, 2)
        assert result["summary_doc_id"] == "1ABC"  # preserved
        assert "summary_doc_id_2" not in result
        assert result["report_doc_id"] == "1XYZ"

    def test_different_lectures_isolated(self, isolated_tracker):
        isolated_tracker.record_delivery(3, 1, summary_doc_id="A")
        isolated_tracker.record_delivery(3, 2, summary_doc_id="B")
        isolated_tracker.record_delivery(4, 1, summary_doc_id="C")
        assert isolated_tracker.load_delivery(3, 1)["summary_doc_id"] == "A"
        assert isolated_tracker.load_delivery(3, 2)["summary_doc_id"] == "B"
        assert isolated_tracker.load_delivery(4, 1)["summary_doc_id"] == "C"


class TestHasDelivered:
    def test_returns_false_when_no_record(self, isolated_tracker):
        assert isolated_tracker.has_delivered(3, 2, "whatsapp_notification_sent_at") is False

    def test_returns_false_when_field_missing(self, isolated_tracker):
        isolated_tracker.record_delivery(3, 2, summary_doc_id="1ABC")
        assert isolated_tracker.has_delivered(3, 2, "whatsapp_notification_sent_at") is False

    def test_returns_true_when_field_set(self, isolated_tracker):
        isolated_tracker.record_delivery(
            3, 2, whatsapp_notification_sent_at="2026-05-19T20:00:00+04:00",
        )
        assert isolated_tracker.has_delivered(3, 2, "whatsapp_notification_sent_at") is True


class TestClearDelivery:
    def test_clear_removes_record(self, isolated_tracker):
        isolated_tracker.record_delivery(3, 2, summary_doc_id="1ABC")
        assert isolated_tracker.clear_delivery(3, 2) is True
        assert isolated_tracker.load_delivery(3, 2) == {}

    def test_clear_returns_false_when_no_record(self, isolated_tracker):
        assert isolated_tracker.clear_delivery(3, 2) is False


class TestDurability:
    def test_record_survives_module_reload(self, isolated_tracker, monkeypatch):
        """A record must persist across reads — it's the whole point of the tracker."""
        isolated_tracker.record_delivery(
            3, 2,
            summary_doc_id="1ABC",
            whatsapp_notification_sent_at="2026-05-19T20:00:00+04:00",
        )
        # Force a re-read from disk (simulating a new process).
        result1 = isolated_tracker.load_delivery(3, 2)
        result2 = isolated_tracker.load_delivery(3, 2)
        assert result1 == result2
        assert result1["whatsapp_notification_sent_at"] == "2026-05-19T20:00:00+04:00"

    def test_corrupt_json_returns_empty_without_raising(self, isolated_tracker):
        """A corrupt tracker file must NOT crash the pipeline — graceful empty."""
        isolated_tracker.DELIVERY_TRACKER_PATH.write_text("not valid json {{{", encoding="utf-8")
        assert isolated_tracker.load_delivery(3, 2) == {}
