"""Nightly reconciliation between Pinecone, scores DB, and pipeline state files.

Three sources of truth in this system can drift:
- Pinecone vector index (vectors per lecture)
- data/scores.db (composite scores per lecture)
- .tmp/pipeline_state_g{N}_l{M}.json (lifecycle state per lecture)

When they disagree, the system's recovery logic (nightly catch-all retries)
can wastefully reprocess lectures that are actually fine, OR fail to process
lectures that genuinely need attention.

This module runs nightly at 03:30 Tbilisi (after the 02:00 catch-all and
03:00 Pinecone backup) and reports any drift via WhatsApp alert.
"""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SCORES_DB_PATH = Path("data/scores.db")
_STATE_DIR = Path(".tmp")
_TERMINAL_STATES = {"completed", "done", "finished", "success", "indexed"}


@dataclass(frozen=True)
class ReconciliationReport:
    """Result of one reconciliation pass."""

    in_pinecone_only: list[tuple[int, int]] = field(default_factory=list)
    in_scores_db_only: list[tuple[int, int]] = field(default_factory=list)
    in_both: list[tuple[int, int]] = field(default_factory=list)
    state_file_orphans: list[tuple[int, int]] = field(default_factory=list)
    error: str | None = None

    @property
    def has_drift(self) -> bool:
        return bool(
            self.in_pinecone_only
            or self.in_scores_db_only
            or self.state_file_orphans
        )

    @property
    def total_drift_count(self) -> int:
        return (
            len(self.in_pinecone_only)
            + len(self.in_scores_db_only)
            + len(self.state_file_orphans)
        )


def _scan_pinecone() -> set[tuple[int, int]]:
    """Return set of (group, lecture) pairs present in Pinecone."""
    from tools.core.config import GROUPS, TOTAL_LECTURES
    from tools.integrations.knowledge_indexer import lecture_exists_in_index

    indexed: set[tuple[int, int]] = set()
    for group in GROUPS:
        for lecture in range(1, TOTAL_LECTURES + 1):
            try:
                if lecture_exists_in_index(group, lecture):
                    indexed.add((group, lecture))
            except Exception as exc:
                logger.warning(
                    "Pinecone probe failed for g%d l%d: %s", group, lecture, exc
                )
    return indexed


def _scan_scores_db() -> set[tuple[int, int]]:
    """Return set of (group, lecture) pairs present in scores DB."""
    if not _SCORES_DB_PATH.exists():
        logger.info("scores.db not found at %s; treating as empty", _SCORES_DB_PATH)
        return set()

    conn = sqlite3.connect(str(_SCORES_DB_PATH))
    try:
        rows = conn.execute(
            "SELECT group_number, lecture_number FROM lecture_scores"
        ).fetchall()
    finally:
        conn.close()
    return {(int(g), int(lec)) for g, lec in rows}


def _scan_state_files() -> list[tuple[tuple[int, int], str]]:
    """Return list of ((group, lecture), state) tuples from .tmp/ state files."""
    if not _STATE_DIR.exists():
        return []

    results: list[tuple[tuple[int, int], str]] = []
    for state_file in _STATE_DIR.glob("pipeline_state_g*_l*.json"):
        try:
            data = json.loads(state_file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read state file %s: %s", state_file, exc)
            continue
        try:
            group = int(data.get("group"))
            lecture = int(data.get("lecture"))
        except (TypeError, ValueError):
            logger.warning("State file %s missing group/lecture keys", state_file)
            continue
        state = str(data.get("state", "")).lower()
        results.append(((group, lecture), state))
    return results


def reconcile_state_drift() -> ReconciliationReport:
    """Compare Pinecone, scores DB, and state files; return drift report."""
    try:
        pinecone_set = _scan_pinecone()
    except Exception as exc:
        logger.error("Pinecone scan failed: %s", exc, exc_info=True)
        return ReconciliationReport(error=f"pinecone_scan_failed: {exc}")

    try:
        scores_set = _scan_scores_db()
    except Exception as exc:
        logger.error("Scores DB scan failed: %s", exc, exc_info=True)
        return ReconciliationReport(error=f"scores_scan_failed: {exc}")

    try:
        state_entries = _scan_state_files()
    except Exception as exc:
        logger.error("State file scan failed: %s", exc, exc_info=True)
        return ReconciliationReport(error=f"state_scan_failed: {exc}")

    in_pinecone_only = sorted(pinecone_set - scores_set)
    in_scores_db_only = sorted(scores_set - pinecone_set)
    in_both = sorted(pinecone_set & scores_set)

    # State file orphan: state file says pipeline is still non-terminal
    # (e.g. "downloading", "transcribing") but Pinecone already has vectors.
    state_orphans: list[tuple[int, int]] = []
    for (group, lecture), state in state_entries:
        if (group, lecture) in pinecone_set and state not in _TERMINAL_STATES:
            state_orphans.append((group, lecture))

    return ReconciliationReport(
        in_pinecone_only=in_pinecone_only,
        in_scores_db_only=in_scores_db_only,
        in_both=in_both,
        state_file_orphans=sorted(state_orphans),
    )


def _format_drift_message(report: ReconciliationReport) -> str:
    """Build a human-readable drift summary for WhatsApp alert."""
    lines = ["Data reconciliation drift detected:"]

    def _fmt(pairs: list[tuple[int, int]]) -> str:
        return ", ".join(f"G{g} L{lec}" for g, lec in pairs) or "(none)"

    if report.in_scores_db_only:
        lines.append(
            f"Scored but NOT in Pinecone ({len(report.in_scores_db_only)}): "
            f"{_fmt(report.in_scores_db_only)}"
        )
    if report.in_pinecone_only:
        lines.append(
            f"In Pinecone but NOT scored ({len(report.in_pinecone_only)}): "
            f"{_fmt(report.in_pinecone_only)}"
        )
    if report.state_file_orphans:
        lines.append(
            f"State file active but already indexed ({len(report.state_file_orphans)}): "
            f"{_fmt(report.state_file_orphans)}"
        )
    return "\n".join(lines)


def alert_on_drift(report: ReconciliationReport) -> bool:
    """If the report shows drift, alert the operator via WhatsApp."""
    if report.error:
        try:
            from tools.integrations.whatsapp_sender import alert_operator
            alert_operator(f"Reconciliation job errored: {report.error}")
            return True
        except Exception as exc:
            logger.error("Failed to send reconciliation error alert: %s", exc)
            return False

    if not report.has_drift:
        return False

    message = _format_drift_message(report)
    try:
        from tools.integrations.whatsapp_sender import alert_operator
        alert_operator(message)
        return True
    except Exception as exc:
        logger.error("Failed to send reconciliation drift alert: %s", exc)
        return False


def register_reconciliation_jobs(scheduler) -> None:
    """Register the nightly reconciliation cron job with APScheduler.

    Scheduled at 03:30 Tbilisi (after 02:00 nightly catch-all and 03:00
    Pinecone backup, but before any morning briefings).
    """
    from apscheduler.triggers.cron import CronTrigger

    from tools.core.config import TBILISI_TZ

    def _job() -> None:
        try:
            report = reconcile_state_drift()
            alert_on_drift(report)
            logger.info(
                "Reconciliation complete: drift=%d (pinecone_only=%d, "
                "scores_only=%d, state_orphans=%d)",
                report.total_drift_count,
                len(report.in_pinecone_only),
                len(report.in_scores_db_only),
                len(report.state_file_orphans),
            )
        except Exception as exc:
            logger.error("Reconciliation job failed: %s", exc, exc_info=True)

    scheduler.add_job(
        _job,
        trigger=CronTrigger(hour=3, minute=30, timezone=TBILISI_TZ),
        id="nightly_reconciliation",
        replace_existing=True,
    )
    logger.info("Registered nightly reconciliation (daily 03:30 Tbilisi)")
