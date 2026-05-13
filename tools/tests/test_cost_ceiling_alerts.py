"""Tests for the 80% / 100% daily cost-ceiling alert + hard-stop logic.

US-023 (ralph 2026-05-13): today's spend hit $85.40 against the $50
DAILY_COST_LIMIT_USD with no operator notification.  These tests pin
the new behaviour:

* alert_operator fires at 80% (warning) and 100% (critical)
* each threshold fires exactly once per UTC day (file-backed dedup)
* on a new UTC day the dedup resets
* once at/over 100%, the NEXT record_cost call raises
  CostCapExceededError unless OVERRIDE_COST_CAP=1
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.core.cost_tracker import (
    DAILY_COST_LIMIT_USD,
    CostCapExceededError,
    _check_cost_thresholds,
    record_cost,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Redirect both cost-entry files AND alert-state files to a tmp dir.

    Also:
      * resets the in-memory legacy alert flag
      * stubs out the legacy ``_send_budget_alert`` so it does NOT route
        through alert_operator (the legacy path is retained for back-compat
        with existing test_cost_tracker.py tests, but in this file we only
        care about the new threshold path)
      * ensures OVERRIDE_COST_CAP is not bleeding in from the shell
    """
    monkeypatch.setattr("tools.core.cost_tracker.TMP_DIR", tmp_path)
    monkeypatch.setattr("tools.core.cost_tracker._alert_sent_today", "")
    monkeypatch.setattr(
        "tools.core.cost_tracker._send_budget_alert",
        lambda *_a, **_kw: None,
    )
    monkeypatch.delenv("OVERRIDE_COST_CAP", raising=False)
    yield tmp_path


# ---------------------------------------------------------------------------
# Threshold firing & dedup
# ---------------------------------------------------------------------------


def test_below_80_no_alert():
    """Recording 79% of the daily cap must NOT fire any operator alert."""
    cost = DAILY_COST_LIMIT_USD * 0.79
    with patch("tools.integrations.whatsapp_sender.alert_operator") as mock_alert:
        record_cost("gemini", "flash", "below-80", 100, 50, cost, "g1_l1")
    mock_alert.assert_not_called()


def test_at_80_fires_alert():
    """Recording 80% of the cap fires exactly one warning alert."""
    cost = DAILY_COST_LIMIT_USD * 0.80
    with patch("tools.integrations.whatsapp_sender.alert_operator") as mock_alert:
        record_cost("gemini", "flash", "at-80", 100, 50, cost, "g1_l1")
    assert mock_alert.call_count == 1
    msg = mock_alert.call_args[0][0]
    assert "⚠️" in msg, f"expected warning icon, got: {msg!r}"


def test_at_80_then_85_no_second_alert():
    """Once 80% has fired today, an 85% record must NOT fire a second alert."""
    with patch("tools.integrations.whatsapp_sender.alert_operator") as mock_alert:
        record_cost(
            "gemini", "flash", "first", 100, 50,
            DAILY_COST_LIMIT_USD * 0.80, "g1_l1",
        )
        # bump to ~85% — still under 100%, still in the same UTC day
        record_cost(
            "gemini", "flash", "more", 100, 50,
            DAILY_COST_LIMIT_USD * 0.05, "g1_l1",
        )
    assert mock_alert.call_count == 1, (
        f"expected exactly 1 alert call, got {mock_alert.call_count}"
    )


def test_at_100_fires_second_alert():
    """80% fires once, then crossing 100% fires a SECOND, distinct alert."""
    with patch("tools.integrations.whatsapp_sender.alert_operator") as mock_alert, \
            patch.dict("os.environ", {"OVERRIDE_COST_CAP": "1"}):
        # first record: 80%
        record_cost(
            "gemini", "flash", "first-80", 100, 50,
            DAILY_COST_LIMIT_USD * 0.80, "g1_l1",
        )
        # second record: jumps past 100% (need override since cap-guard would
        # otherwise refuse this very call — but here we're at 80%, NOT yet
        # at 100% before the call, so no guard. Override kept for safety on
        # subsequent calls in case test order matters.)
        record_cost(
            "gemini", "flash", "push-past-100", 100, 50,
            DAILY_COST_LIMIT_USD * 0.30, "g1_l1",
        )
    assert mock_alert.call_count == 2, (
        f"expected 2 alerts (80% + 100%), got {mock_alert.call_count}"
    )
    msgs = [call.args[0] for call in mock_alert.call_args_list]
    assert any("⚠️" in m for m in msgs), "missing 80% warning icon"
    assert any("🚨" in m for m in msgs), "missing 100% critical icon"


# ---------------------------------------------------------------------------
# Hard-stop & override
# ---------------------------------------------------------------------------


def test_override_flag_skips_hard_stop(monkeypatch):
    """OVERRIDE_COST_CAP=1 must allow recording AFTER cap is reached."""
    monkeypatch.setenv("OVERRIDE_COST_CAP", "1")

    # Push to 100% first (cap not yet reached at call time, so it succeeds).
    with patch("tools.integrations.whatsapp_sender.alert_operator"):
        record_cost(
            "gemini", "flash", "to-100", 100, 50,
            DAILY_COST_LIMIT_USD, "g1_l1",
        )
        # Now cap IS reached — the next call would normally raise.  With the
        # override, it should succeed instead.
        total = record_cost(
            "gemini", "flash", "after-cap", 100, 50, 1.0, "g1_l1",
        )
    assert total >= DAILY_COST_LIMIT_USD


def test_no_override_at_100_raises():
    """Without OVERRIDE_COST_CAP, recording AFTER cap raises CostCapExceededError."""
    with patch("tools.integrations.whatsapp_sender.alert_operator"):
        # First call brings us to 110% — this one succeeds (it's the one that
        # crossed the threshold; its alert tells the operator the cap was hit).
        record_cost(
            "gemini", "flash", "to-110", 100, 50,
            DAILY_COST_LIMIT_USD * 1.10, "g1_l1",
        )
        # Second call: cap is already exceeded — must raise.
        with pytest.raises(CostCapExceededError):
            record_cost("gemini", "flash", "after", 100, 50, 1.0, "g1_l1")


# ---------------------------------------------------------------------------
# Daily dedup roll-over
# ---------------------------------------------------------------------------


def test_daily_dedup_resets_on_new_day(tmp_path):
    """Each UTC day's thresholds_fired list is independent.

    We can't (easily) move the wall clock, so we simulate the day change by
    seeding the dedup state file for "yesterday" with both thresholds already
    fired, then calling _check_cost_thresholds today and observing that today's
    fresh state file still fires the alerts.
    """
    import json

    # Seed a state file for an arbitrary past UTC date with both thresholds
    # already marked as fired.
    yesterday = "2026-04-01"
    (tmp_path / f"cost_alerts_{yesterday}.json").write_text(
        json.dumps({
            "thresholds_fired": [80, 100],
            "last_total": 99.99,
            "last_updated": "2026-04-01T23:59:59Z",
        }),
        encoding="utf-8",
    )

    # Today (whatever UTC date "now" is) has NO state file yet, so a 100%
    # call must fire both alerts fresh.
    with patch("tools.integrations.whatsapp_sender.alert_operator") as mock_alert:
        _check_cost_thresholds(DAILY_COST_LIMIT_USD)

    # Both 80% and 100% fire on the new day.
    assert mock_alert.call_count == 2, (
        f"new UTC day must fire both thresholds afresh, got {mock_alert.call_count}"
    )

    # And yesterday's seed file remains untouched (we never overwrite
    # a different day's state).
    yesterday_state = json.loads(
        (tmp_path / f"cost_alerts_{yesterday}.json").read_text(encoding="utf-8")
    )
    assert yesterday_state["thresholds_fired"] == [80, 100]
