"""Daily cost tracking and budget enforcement for API calls.

Tracks cumulative Gemini and Claude API costs per day, enforces daily
and per-lecture budget limits, and alerts the operator when thresholds
are reached.  Costs are persisted to JSON files in .tmp/ so they
survive server restarts.

Usage::

    from tools.core.cost_tracker import record_cost, check_daily_budget

    # After each API call:
    daily_total = record_cost(
        service="gemini", model="gemini-2.5-flash",
        purpose="transcription chunk 2/4",
        input_tokens=800_000, output_tokens=12_000,
        cost_usd=4.50, pipeline_key="g1_l7",
    )

    # Before starting a pipeline:
    ok, remaining = check_daily_budget()
    if not ok:
        raise RuntimeError("Daily cost limit reached")
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from tools.core.config import TBILISI_TZ, TMP_DIR

logger = logging.getLogger(__name__)

# Budget defaults (overridable via env vars)
DAILY_COST_LIMIT_USD = float(os.environ.get("DAILY_COST_LIMIT_USD", "50.0"))
LECTURE_COST_LIMIT_USD = float(os.environ.get("LECTURE_COST_LIMIT_USD", "20.0"))
DAILY_COST_ALERT_THRESHOLD = 0.80  # alert at 80% of daily limit

# Thresholds (percent of DAILY_COST_LIMIT_USD) that fire operator alerts.
# Each threshold fires at most once per UTC day; state lives in a JSON file
# under TMP_DIR keyed by date so it survives server restarts.
COST_ALERT_THRESHOLDS_PCT: tuple[int, ...] = (80, 100)

_lock = threading.Lock()
_alert_sent_today: str = ""  # date string when 80% alert was last sent (legacy)


class CostCapExceededError(RuntimeError):
    """Raised when a recorded cost pushes the daily total at/over 100% of the cap.

    Bypass by setting environment variable ``OVERRIDE_COST_CAP=1``.
    """


@dataclass
class CostEntry:
    """Single API call cost record."""

    timestamp: str
    service: str  # "gemini" | "claude"
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    pipeline_key: str


def _today_str() -> str:
    """Current date in Tbilisi timezone as YYYY-MM-DD."""
    return datetime.now(tz=TBILISI_TZ).strftime("%Y-%m-%d")


def _cost_file_path(date_str: str | None = None) -> Path:
    """Path to the daily cost JSON file."""
    if date_str is None:
        date_str = _today_str()
    return TMP_DIR / f"daily_costs_{date_str}.json"


def _load_entries(date_str: str | None = None) -> list[dict]:
    """Load today's cost entries from disk.  Returns empty list if missing."""
    path = _cost_file_path(date_str)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load cost file %s: %s", path, exc)
        return []


def _save_entries(entries: list[dict], date_str: str | None = None) -> None:
    """Atomically write cost entries to disk."""
    path = _cost_file_path(date_str)
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _utc_today_str() -> str:
    """Current UTC date as YYYY-MM-DD — used to key the threshold-fire state.

    Threshold dedup uses UTC (not Tbilisi) so the state file rolls at the same
    instant globally regardless of operator location.
    """
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _alert_state_path(date_str: str | None = None) -> Path:
    """Path to the per-day cost-alert dedup state file."""
    if date_str is None:
        date_str = _utc_today_str()
    return TMP_DIR / f"cost_alerts_{date_str}.json"


def _load_alert_state(date_str: str | None = None) -> dict:
    """Load the dedup state for the given (or today's UTC) date.

    Returns a dict shaped like:
        {"thresholds_fired": [80], "last_total": 42.0, "last_updated": "<iso>"}
    Missing or corrupt files yield an empty default.
    """
    path = _alert_state_path(date_str)
    if not path.exists():
        return {"thresholds_fired": [], "last_total": 0.0, "last_updated": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"thresholds_fired": [], "last_total": 0.0, "last_updated": ""}
        fired_raw = data.get("thresholds_fired", [])
        fired = [int(x) for x in fired_raw if isinstance(x, (int, float))]
        return {
            "thresholds_fired": fired,
            "last_total": float(data.get("last_total", 0.0)),
            "last_updated": str(data.get("last_updated", "")),
        }
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("Failed to load alert state %s: %s", path, exc)
        return {"thresholds_fired": [], "last_total": 0.0, "last_updated": ""}


def _save_alert_state(state: dict, date_str: str | None = None) -> None:
    """Atomically persist the dedup state for the given (or today's UTC) date."""
    path = _alert_state_path(date_str)
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except OSError as exc:
        logger.warning("Failed to save alert state %s: %s", path, exc)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _check_cost_thresholds(daily_total_usd: float) -> None:
    """Fire alert_operator at 80% and 100% of DAILY_COST_LIMIT_USD, dedup'd per day.

    Each threshold in :data:`COST_ALERT_THRESHOLDS_PCT` fires exactly once per
    UTC day.  State is persisted to ``TMP_DIR/cost_alerts_<utc-date>.json`` so
    a server restart in the middle of the day cannot double-alert.

    Safe to call from inside the cost-record lock — purely local file I/O.
    Never raises; alert failures are logged and swallowed.
    """
    if DAILY_COST_LIMIT_USD <= 0:
        return  # cap disabled

    pct = (daily_total_usd / DAILY_COST_LIMIT_USD) * 100.0
    state = _load_alert_state()
    fired = set(state.get("thresholds_fired", []))

    changed = False
    for threshold_pct in COST_ALERT_THRESHOLDS_PCT:
        if pct >= threshold_pct and threshold_pct not in fired:
            try:
                from tools.integrations.whatsapp_sender import alert_operator

                icon = "⚠️" if threshold_pct == 80 else "🚨"
                tail = (
                    "მონიტორი"
                    if threshold_pct == 80
                    else (
                        "ლიმიტი მიღწეულია — შემდეგი API call შეჩერდება თუ "
                        "OVERRIDE_COST_CAP არ არის ჩართული."
                    )
                )
                alert_operator(
                    f"{icon} დღევანდელი API ხარჯი: ${daily_total_usd:.2f} = "
                    f"{pct:.0f}% ლიმიტისგან (${DAILY_COST_LIMIT_USD:.2f}). "
                    + tail
                )
                fired.add(threshold_pct)
                changed = True
                logger.warning(
                    "Cost ceiling alert fired at %d%% threshold: $%.2f / $%.2f",
                    threshold_pct, daily_total_usd, DAILY_COST_LIMIT_USD,
                )
            except Exception as exc:
                logger.warning("cost ceiling alert failed: %s", exc)

    if changed:
        _save_alert_state({
            "thresholds_fired": sorted(fired),
            "last_total": round(daily_total_usd, 4),
            "last_updated": datetime.now(tz=timezone.utc).isoformat(),
        })


def record_cost(
    service: str,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    pipeline_key: str = "",
) -> float:
    """Record an API call cost and return updated daily total.

    Thread-safe.  Persists to disk immediately.

    Args:
        service: "gemini" or "claude".
        model: Model name (e.g. "gemini-2.5-flash").
        purpose: Human-readable purpose (e.g. "transcription chunk 2/4").
        input_tokens: Number of input tokens.
        output_tokens: Number of output tokens.
        cost_usd: Computed cost in USD.
        pipeline_key: Pipeline identifier (e.g. "g1_l7").

    Returns:
        Cumulative daily total in USD after this entry.
    """
    global _alert_sent_today  # noqa: PLW0603

    entry = CostEntry(
        timestamp=datetime.now(tz=TBILISI_TZ).isoformat(),
        service=service,
        model=model,
        purpose=purpose,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=round(cost_usd, 4),
        pipeline_key=pipeline_key,
    )

    with _lock:
        today = _today_str()
        entries = _load_entries(today)

        # Hard-stop guard: if today's total is ALREADY at/over the cap
        # BEFORE we add this entry, refuse the new spend unless
        # OVERRIDE_COST_CAP is set.  This prevents runaway burn while
        # still letting the call that first crosses the threshold complete
        # (its alert is the user's notification that the cap was hit).
        pre_total = sum(e.get("cost_usd", 0) for e in entries)
        if (
            DAILY_COST_LIMIT_USD > 0
            and pre_total >= DAILY_COST_LIMIT_USD
            and not os.environ.get("OVERRIDE_COST_CAP")
        ):
            raise CostCapExceededError(
                f"Daily cost cap reached: ${pre_total:.2f} >= "
                f"${DAILY_COST_LIMIT_USD:.2f}. "
                f"Set OVERRIDE_COST_CAP=1 to bypass."
            )

        entries.append(asdict(entry))
        _save_entries(entries, today)
        daily_total = sum(e.get("cost_usd", 0) for e in entries)

    logger.info(
        "💵 Cost recorded: %s %s [%s] $%.4f — daily total: $%.2f / $%.2f limit",
        service, model, purpose, cost_usd, daily_total, DAILY_COST_LIMIT_USD,
    )

    # Fire 80% / 100% threshold alerts (per-UTC-day dedup, file-backed).
    _check_cost_thresholds(daily_total)

    # Legacy in-memory 80% alert hook — retained for backward compatibility
    # with tests that patch ``_send_budget_alert``.  New deployments rely on
    # ``_check_cost_thresholds`` above for the actual operator notification.
    if (
        DAILY_COST_LIMIT_USD > 0
        and daily_total >= DAILY_COST_LIMIT_USD * DAILY_COST_ALERT_THRESHOLD
        and _alert_sent_today != today
    ):
        _alert_sent_today = today
        _send_budget_alert(daily_total)

    return daily_total


def get_daily_total(date_str: str | None = None) -> float:
    """Get cumulative cost for today (or a specific date).

    Args:
        date_str: Optional ISO date string.  Defaults to today (Tbilisi).

    Returns:
        Total USD spent so far today.
    """
    with _lock:
        entries = _load_entries(date_str)
    return sum(e.get("cost_usd", 0) for e in entries)


def check_daily_budget() -> tuple[bool, float]:
    """Check if daily budget allows more spending.

    Returns:
        (is_ok, remaining_usd).  is_ok is False if limit is exceeded.
    """
    total = get_daily_total()
    remaining = max(0.0, DAILY_COST_LIMIT_USD - total)
    return (remaining > 0, remaining)


def get_pipeline_cost(pipeline_key: str) -> float:
    """Get total cost for a specific pipeline today.

    Args:
        pipeline_key: Pipeline identifier (e.g. "g1_l7").

    Returns:
        Total USD spent on this pipeline today.
    """
    with _lock:
        entries = _load_entries()
    return sum(
        e.get("cost_usd", 0)
        for e in entries
        if e.get("pipeline_key") == pipeline_key
    )


def check_lecture_budget(pipeline_key: str) -> tuple[bool, float]:
    """Check if a specific lecture pipeline is within budget.

    Args:
        pipeline_key: Pipeline identifier (e.g. "g1_l7").

    Returns:
        (is_ok, remaining_usd).
    """
    total = get_pipeline_cost(pipeline_key)
    remaining = max(0.0, LECTURE_COST_LIMIT_USD - total)
    return (remaining > 0, remaining)


def get_daily_summary(date_str: str | None = None) -> dict:
    """Get a summary of today's costs for the admin endpoint.

    Returns:
        Dict with date, total, limit, remaining, pct_used, and per-pipeline breakdown.
    """
    if date_str is None:
        date_str = _today_str()
    with _lock:
        entries = _load_entries(date_str)

    total = sum(e.get("cost_usd", 0) for e in entries)

    # Per-pipeline breakdown
    pipelines: dict[str, float] = {}
    for e in entries:
        pk = e.get("pipeline_key", "unknown")
        pipelines[pk] = pipelines.get(pk, 0) + e.get("cost_usd", 0)

    return {
        "date": date_str,
        "total_usd": round(total, 2),
        "limit_usd": DAILY_COST_LIMIT_USD,
        "remaining_usd": round(max(0, DAILY_COST_LIMIT_USD - total), 2),
        "pct_used": round(total / DAILY_COST_LIMIT_USD * 100, 1) if DAILY_COST_LIMIT_USD > 0 else 0,
        "pipelines": {k: round(v, 2) for k, v in sorted(pipelines.items())},
        "entry_count": len(entries),
    }


def cleanup_old_cost_files(max_age_days: int = 30) -> int:
    """Remove daily cost files older than max_age_days.

    Args:
        max_age_days: Maximum age in days.  Files older than this are deleted.

    Returns:
        Number of files deleted.
    """
    deleted = 0
    cutoff = datetime.now(tz=TBILISI_TZ).date()
    for path in TMP_DIR.glob("daily_costs_*.json"):
        try:
            date_part = path.stem.replace("daily_costs_", "")
            file_date = datetime.strptime(date_part, "%Y-%m-%d").date()
            age_days = (cutoff - file_date).days
            if age_days > max_age_days:
                path.unlink(missing_ok=True)
                deleted += 1
                logger.debug("Deleted old cost file: %s (age=%d days)", path.name, age_days)
        except (ValueError, OSError):
            continue
    if deleted:
        logger.info("Cleaned up %d old cost files (>%d days)", deleted, max_age_days)
    return deleted


def _send_budget_alert(daily_total: float) -> None:
    """Send WhatsApp alert when daily budget threshold is reached."""
    try:
        from tools.integrations.whatsapp_sender import alert_operator

        pct = round(daily_total / DAILY_COST_LIMIT_USD * 100)
        alert_operator(
            f"⚠️ Daily API cost alert: ${daily_total:.2f} spent today "
            f"({pct}% of ${DAILY_COST_LIMIT_USD:.0f} limit). "
            f"Remaining: ${max(0, DAILY_COST_LIMIT_USD - daily_total):.2f}"
        )
        logger.warning(
            "Daily cost alert sent: $%.2f / $%.2f (%d%%)",
            daily_total, DAILY_COST_LIMIT_USD, pct,
        )
    except Exception as exc:
        logger.error("Failed to send budget alert: %s", exc)
