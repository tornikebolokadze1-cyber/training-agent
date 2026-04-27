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
from datetime import datetime
from pathlib import Path

from tools.core.config import TBILISI_TZ, TMP_DIR

logger = logging.getLogger(__name__)

# Budget defaults (overridable via env vars)
DAILY_COST_LIMIT_USD = float(os.environ.get("DAILY_COST_LIMIT_USD", "50.0"))
LECTURE_COST_LIMIT_USD = float(os.environ.get("LECTURE_COST_LIMIT_USD", "20.0"))
DAILY_COST_ALERT_THRESHOLD = 0.80  # alert at 80% of daily limit

_lock = threading.Lock()
_alert_sent_today: str = ""  # date string when 80% alert was last sent


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
        entries.append(asdict(entry))
        _save_entries(entries, today)
        daily_total = sum(e.get("cost_usd", 0) for e in entries)

    logger.info(
        "💵 Cost recorded: %s %s [%s] $%.4f — daily total: $%.2f / $%.2f limit",
        service, model, purpose, cost_usd, daily_total, DAILY_COST_LIMIT_USD,
    )

    # Alert at 80% threshold (once per day)
    if (
        daily_total >= DAILY_COST_LIMIT_USD * DAILY_COST_ALERT_THRESHOLD
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
