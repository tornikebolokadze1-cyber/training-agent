"""Tests for the daily cost tracking and budget enforcement system."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from tools.core.cost_tracker import (
    DAILY_COST_LIMIT_USD,
    LECTURE_COST_LIMIT_USD,
    check_daily_budget,
    check_lecture_budget,
    cleanup_old_cost_files,
    get_daily_summary,
    get_daily_total,
    get_pipeline_cost,
    record_cost,
)


@pytest.fixture(autouse=True)
def _isolate_cost_files(tmp_path, monkeypatch):
    """Redirect cost files to a temp directory for test isolation."""
    monkeypatch.setattr("tools.core.cost_tracker.TMP_DIR", tmp_path)
    # Reset alert state
    monkeypatch.setattr("tools.core.cost_tracker._alert_sent_today", "")


class TestRecordCost:
    def test_records_and_returns_daily_total(self, tmp_path):
        total = record_cost(
            service="gemini", model="gemini-2.5-flash",
            purpose="transcription chunk 1/4",
            input_tokens=800_000, output_tokens=12_000,
            cost_usd=4.50, pipeline_key="g1_l7",
        )
        assert total == pytest.approx(4.50)

        total2 = record_cost(
            service="claude", model="claude-sonnet-4-6",
            purpose="combined analysis",
            input_tokens=200_000, output_tokens=8_000,
            cost_usd=3.80, pipeline_key="g1_l7",
        )
        assert total2 == pytest.approx(8.30)

    def test_persists_to_disk(self, tmp_path):
        record_cost(
            service="gemini", model="gemini-2.5-flash",
            purpose="test", input_tokens=100, output_tokens=50,
            cost_usd=1.23, pipeline_key="g1_l5",
        )
        cost_files = list(tmp_path.glob("daily_costs_*.json"))
        assert len(cost_files) == 1
        data = json.loads(cost_files[0].read_text())
        assert len(data) == 1
        assert data[0]["cost_usd"] == 1.23
        assert data[0]["pipeline_key"] == "g1_l5"
        assert data[0]["service"] == "gemini"

    def test_multiple_entries_accumulate(self, tmp_path):
        for i in range(5):
            record_cost(
                service="gemini", model="gemini-2.5-flash",
                purpose=f"chunk {i}", input_tokens=100, output_tokens=50,
                cost_usd=2.0, pipeline_key="g2_l6",
            )
        assert get_daily_total() == pytest.approx(10.0)


class TestBudgetChecks:
    def test_daily_budget_ok_when_under_limit(self):
        record_cost("gemini", "flash", "test", 100, 50, 5.0, "g1_l1")
        ok, remaining = check_daily_budget()
        assert ok is True
        assert remaining == pytest.approx(DAILY_COST_LIMIT_USD - 5.0)

    def test_daily_budget_exceeded(self):
        record_cost("gemini", "flash", "test", 100, 50, DAILY_COST_LIMIT_USD + 1, "g1_l1")
        ok, remaining = check_daily_budget()
        assert ok is False
        assert remaining == 0.0

    def test_lecture_budget_ok(self):
        record_cost("gemini", "flash", "chunk1", 100, 50, 5.0, "g1_l7")
        ok, remaining = check_lecture_budget("g1_l7")
        assert ok is True
        assert remaining == pytest.approx(LECTURE_COST_LIMIT_USD - 5.0)

    def test_lecture_budget_exceeded(self):
        record_cost("gemini", "flash", "chunk1", 100, 50, LECTURE_COST_LIMIT_USD + 1, "g1_l7")
        ok, remaining = check_lecture_budget("g1_l7")
        assert ok is False
        assert remaining == 0.0

    def test_lecture_budgets_are_independent(self):
        record_cost("gemini", "flash", "t", 100, 50, 20.0, "g1_l7")
        record_cost("gemini", "flash", "t", 100, 50, 3.0, "g2_l7")
        assert get_pipeline_cost("g1_l7") == pytest.approx(20.0)
        assert get_pipeline_cost("g2_l7") == pytest.approx(3.0)


class TestDailySummary:
    def test_summary_structure(self):
        record_cost("gemini", "flash", "t1", 100, 50, 5.0, "g1_l7")
        record_cost("claude", "sonnet", "t2", 100, 50, 3.0, "g1_l7")
        summary = get_daily_summary()
        assert "date" in summary
        assert summary["total_usd"] == pytest.approx(8.0)
        assert summary["limit_usd"] == DAILY_COST_LIMIT_USD
        assert summary["remaining_usd"] == pytest.approx(DAILY_COST_LIMIT_USD - 8.0)
        assert "g1_l7" in summary["pipelines"]
        assert summary["entry_count"] == 2

    def test_empty_day(self):
        summary = get_daily_summary()
        assert summary["total_usd"] == 0
        assert summary["entry_count"] == 0


class TestAlertThreshold:
    @patch("tools.core.cost_tracker._send_budget_alert")
    def test_alert_fires_at_80_percent(self, mock_alert):
        threshold_amount = DAILY_COST_LIMIT_USD * 0.80
        record_cost("gemini", "flash", "big", 100, 50, threshold_amount, "g1_l7")
        mock_alert.assert_called_once_with(threshold_amount)

    @patch("tools.core.cost_tracker._send_budget_alert")
    def test_alert_fires_only_once_per_day(self, mock_alert):
        threshold_amount = DAILY_COST_LIMIT_USD * 0.80
        record_cost("gemini", "flash", "big", 100, 50, threshold_amount, "g1_l7")
        record_cost("gemini", "flash", "more", 100, 50, 5.0, "g1_l7")
        assert mock_alert.call_count == 1


class TestCleanup:
    def test_cleanup_removes_old_files(self, tmp_path):
        # Create a "30+ day old" cost file
        old_file = tmp_path / "daily_costs_2026-02-01.json"
        old_file.write_text("[]")
        # Create today's file
        record_cost("gemini", "flash", "t", 100, 50, 1.0, "g1_l1")

        deleted = cleanup_old_cost_files(max_age_days=30)
        assert deleted == 1
        assert not old_file.exists()
        # Today's file still exists
        assert len(list(tmp_path.glob("daily_costs_*.json"))) == 1
