"""DLQ audit and cleanup tool.

US-022 from ralph 2026-05-13. Codex audit flagged 634+ DLQ artifacts in
.tmp/dlq/ with no clean separation of real failures vs test/synthetic
detritus. This script:

  1. Reads every JSON file under the DLQ directory.
  2. Classifies each entry as ``synthetic``, ``real``, or ``corrupt``.
  3. Aggregates counts by date, group, and error class.
  4. Prints (and saves) a recommended-actions report.
  5. With ``--execute``, moves synthetic + corrupt entries to archive
     subdirs. NEVER auto-deletes. Real entries are listed only — actual
     retry stays in ``scripts/whatsapp_catchup_now.py`` etc.

Usage::

    python scripts/audit_dlq.py                       # dry-run report
    python scripts/audit_dlq.py --execute             # archive synthetic/corrupt
    python scripts/audit_dlq.py --filter synthetic    # filter report scope
    python scripts/audit_dlq.py --dlq-dir /custom/path

The script is defensive: permission errors, malformed JSON, and missing
fields never crash it. Empty DLQs print a clean "no entries" message.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_DLQ_DIR: Path = REPO_ROOT / ".tmp" / "dlq"
DEFAULT_REPORT_DIR: Path = REPO_ROOT / ".tmp"

# Markers that indicate a synthetic/test entry. Match against the
# JSON-serialized entry (case-insensitive substring search).
SYNTHETIC_MARKERS: tuple[str, ...] = (
    '"synthetic": true',
    '"test_"',
    '"dryrun"',
    '"dry_run": true',
    "'lecture summary text'",
    "'tttttttttttttttttttttttttttttttttttttttttttttttttttttttttttttt",
    "file-id-abc",
    "doc-id-xyz",
    "chat-id-test",
    "chatid-test",
    "test@c.us",
    "test@g.us",
)

# Known test phone numbers / chat IDs (Green API uses ``<digits>@c.us`` for
# personal chats and ``<digits>@g.us`` for groups; obvious placeholders go
# here).
SYNTHETIC_CHAT_IDS: tuple[str, ...] = (
    "1234567890@c.us",
    "0000000000@c.us",
    "test@c.us",
    "fake@c.us",
)

# Statuses
Status = Literal["real", "synthetic", "corrupt"]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DLQEntry:
    """A single parsed DLQ entry plus its classification."""

    path: Path
    status: Status
    operation: str
    date: str  # YYYY-MM-DD
    group: str  # str so we can use "unknown" without union type
    error_class: str
    raw: dict[str, Any] | None


@dataclass(frozen=True)
class AuditReport:
    """Aggregated audit results."""

    total: int
    by_status: dict[str, int]
    by_date: dict[str, int]
    by_group: dict[str, int]
    by_error_class: dict[str, int]
    oldest_real: list[DLQEntry]
    entries: list[DLQEntry]


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _safe_read_text(path: Path) -> str | None:
    """Read a file's text content; return None on any OSError."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read %s: %s", path.name, exc)
        return None


def _is_synthetic(raw_text: str, parsed: dict[str, Any]) -> bool:
    """Heuristics to decide if an entry is synthetic/test data.

    Args:
        raw_text: Original JSON text of the file (case-insensitive markers
            are matched against the lowercased version).
        parsed: Parsed JSON object (already known to be a dict).

    Returns:
        True if any synthetic marker matches.
    """
    haystack = raw_text.lower()
    for marker in SYNTHETIC_MARKERS:
        if marker.lower() in haystack:
            return True

    # Walk payload for chat IDs that look synthetic.
    payload = parsed.get("payload") or {}
    if isinstance(payload, dict):
        chat_id = str(payload.get("chat_id", "")).lower()
        if any(synthetic.lower() in chat_id for synthetic in SYNTHETIC_CHAT_IDS):
            return True

    # max_retries stored as a string (instead of an int) is a strong signal
    # of hand-rolled / test fixture data — real production code always
    # writes an int.
    if isinstance(parsed.get("max_retries"), str):
        return True

    return False


def _extract_date(parsed: dict[str, Any], path: Path) -> str:
    """Extract a YYYY-MM-DD date string from the entry, falling back to mtime."""
    created_at = parsed.get("created_at")
    if isinstance(created_at, str):
        # Try ISO 8601 first
        try:
            return created_at[:10]  # cheapest path — slice the ISO date
        except (TypeError, IndexError):
            pass
        try:
            return datetime.fromisoformat(created_at).strftime("%Y-%m-%d")
        except ValueError:
            pass
    if isinstance(created_at, (int, float)):
        try:
            return datetime.fromtimestamp(created_at).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            pass

    # Fall back to file mtime
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return "unknown"


def _extract_group(parsed: dict[str, Any]) -> str:
    """Extract group number from the entry's payload, or 'unknown'."""
    payload = parsed.get("payload") or {}
    if not isinstance(payload, dict):
        return "unknown"
    group = payload.get("group_number") or payload.get("group")
    if group is None:
        # Some entries put group as the first arg in payload["args"]
        args = payload.get("args")
        if isinstance(args, list) and args:
            first = args[0]
            try:
                return str(int(str(first)))
            except (TypeError, ValueError):
                return "unknown"
        return "unknown"
    try:
        return str(int(str(group)))
    except (TypeError, ValueError):
        return "unknown"


def _extract_error_class(parsed: dict[str, Any]) -> str:
    """Extract an error class string, truncated to 50 chars."""
    err = parsed.get("error_type") or parsed.get("last_error")
    # Some synthetic entries put error text in max_retries (real code uses int).
    if not err and isinstance(parsed.get("max_retries"), str):
        err = parsed["max_retries"]
    if not err:
        return "(none)"
    return str(err)[:50] or "(empty)"


def classify_entry(path: Path) -> DLQEntry:
    """Read and classify a single DLQ file.

    Always returns a DLQEntry — corrupt / unreadable files get
    ``status='corrupt'`` with empty/unknown fields.
    """
    raw_text = _safe_read_text(path)
    if raw_text is None:
        return DLQEntry(
            path=path,
            status="corrupt",
            operation="(unreadable)",
            date="unknown",
            group="unknown",
            error_class="(unreadable)",
            raw=None,
        )

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return DLQEntry(
            path=path,
            status="corrupt",
            operation="(malformed-json)",
            date=_mtime_date(path),
            group="unknown",
            error_class="(malformed-json)",
            raw=None,
        )

    if not isinstance(parsed, dict):
        return DLQEntry(
            path=path,
            status="corrupt",
            operation="(non-object-json)",
            date=_mtime_date(path),
            group="unknown",
            error_class="(non-object-json)",
            raw=None,
        )

    operation = str(parsed.get("operation", "(unknown)"))
    date = _extract_date(parsed, path)
    group = _extract_group(parsed)
    error_class = _extract_error_class(parsed)
    status: Status = "synthetic" if _is_synthetic(raw_text, parsed) else "real"

    return DLQEntry(
        path=path,
        status=status,
        operation=operation,
        date=date,
        group=group,
        error_class=error_class,
        raw=parsed,
    )


def _mtime_date(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def scan_dlq(dlq_dir: Path) -> list[DLQEntry]:
    """Walk the DLQ dir (non-recursively for archive/) and classify every entry.

    Subdirectories (e.g. ``archive/``, ``failed/``) are skipped.
    """
    if not dlq_dir.exists():
        return []
    entries: list[DLQEntry] = []
    try:
        paths = sorted(p for p in dlq_dir.iterdir() if p.is_file() and p.suffix == ".json")
    except OSError as exc:
        logger.error("Cannot list DLQ dir %s: %s", dlq_dir, exc)
        return []
    for path in paths:
        try:
            entries.append(classify_entry(path))
        except Exception as exc:  # noqa: BLE001 — defensive guard
            logger.warning("Skipping %s due to unexpected error: %s", path.name, exc)
    return entries


def aggregate(entries: Iterable[DLQEntry]) -> AuditReport:
    """Aggregate classified entries into a report struct."""
    entries_list = list(entries)
    by_status = Counter(e.status for e in entries_list)
    by_date = Counter(e.date for e in entries_list)
    by_group = Counter(e.group for e in entries_list)
    by_error_class = Counter(e.error_class for e in entries_list)

    real_entries = [e for e in entries_list if e.status == "real"]
    real_entries.sort(key=lambda e: e.date)
    oldest_real = real_entries[:10]

    return AuditReport(
        total=len(entries_list),
        by_status=dict(by_status),
        by_date=dict(by_date),
        by_group=dict(by_group),
        by_error_class=dict(by_error_class),
        oldest_real=oldest_real,
        entries=entries_list,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(report: AuditReport, dlq_dir: Path) -> str:
    """Format the audit report as a human-readable string."""
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines.append(f"DLQ AUDIT REPORT — {now}")
    lines.append(f"Source: {dlq_dir}")
    lines.append("")

    if report.total == 0:
        lines.append("No DLQ entries found.")
        return "\n".join(lines)

    lines.append(f"Total entries: {report.total}")

    real = report.by_status.get("real", 0)
    synthetic = report.by_status.get("synthetic", 0)
    corrupt = report.by_status.get("corrupt", 0)
    lines.append(f"  by status: real={real}, synthetic={synthetic}, corrupt={corrupt}")

    lines.append("  by date:")
    for date in sorted(report.by_date, reverse=True):
        lines.append(f"    {date}: {report.by_date[date]}")

    lines.append("  by group:")
    for group in sorted(report.by_group, key=lambda g: (g == "unknown", g)):
        lines.append(f"    {group}: {report.by_group[group]}")

    lines.append("  by error class:")
    top_errors = sorted(
        report.by_error_class.items(), key=lambda kv: kv[1], reverse=True
    )[:10]
    for err, count in top_errors:
        lines.append(f"    {err!r}: {count}")

    lines.append("")
    lines.append(f"Top {len(report.oldest_real)} oldest real entries:")
    if report.oldest_real:
        for entry in report.oldest_real:
            lines.append(
                f"  {entry.date}  {entry.operation:<20s}  group={entry.group}  "
                f"{entry.path.name}"
            )
    else:
        lines.append("  (none)")

    lines.append("")
    lines.append("Recommended actions:")
    if synthetic:
        lines.append(
            f"  - Archive {synthetic} synthetic entries to .tmp/dlq/archive/synthetic/"
        )
    if real:
        lines.append(
            f"  - Re-attempt {real} real entries (oldest first) via "
            "`python scripts/whatsapp_catchup_now.py` or equivalent"
        )
    if corrupt:
        lines.append(
            f"  - Move {corrupt} corrupt entries to .tmp/dlq/archive/corrupt/ "
            "for manual review"
        )
    if not (synthetic or real or corrupt):
        lines.append("  (none — DLQ is clean)")

    return "\n".join(lines)


def save_report(text: str, report_dir: Path) -> Path:
    """Write the report to ``.tmp/dlq_audit_report_<TIMESTAMP>.txt``."""
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"dlq_audit_report_{timestamp}.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Execute (archive moves)
# ---------------------------------------------------------------------------


def execute_archive(report: AuditReport, dlq_dir: Path) -> dict[str, int]:
    """Move synthetic + corrupt entries to archive subdirs.

    Real entries are *not* moved here — they should be re-processed via the
    real retry tooling (``scripts/whatsapp_catchup_now.py`` or similar).
    """
    synthetic_dir = dlq_dir / "archive" / "synthetic"
    corrupt_dir = dlq_dir / "archive" / "corrupt"
    synthetic_dir.mkdir(parents=True, exist_ok=True)
    corrupt_dir.mkdir(parents=True, exist_ok=True)

    moved = {"synthetic": 0, "corrupt": 0, "skipped": 0}

    for entry in report.entries:
        if entry.status == "synthetic":
            dest = synthetic_dir / entry.path.name
        elif entry.status == "corrupt":
            dest = corrupt_dir / entry.path.name
        else:
            continue
        try:
            entry.path.replace(dest)
            moved[entry.status] += 1
        except OSError as exc:
            logger.warning("Could not move %s: %s", entry.path.name, exc)
            moved["skipped"] += 1

    return moved


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit_dlq",
        description="Audit and optionally archive entries in the WhatsApp/Drive DLQ.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Move synthetic+corrupt entries to .tmp/dlq/archive/. Without this "
        "flag the script is read-only (dry-run).",
    )
    parser.add_argument(
        "--filter",
        choices=["real", "synthetic", "all"],
        default="all",
        help="Limit the printed report to entries of one status. "
        "Aggregate counts are always computed across ALL entries.",
    )
    parser.add_argument(
        "--dlq-dir",
        type=Path,
        default=DEFAULT_DLQ_DIR,
        help=f"Path to the DLQ directory (default: {DEFAULT_DLQ_DIR}).",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=DEFAULT_REPORT_DIR,
        help=f"Directory for the saved report file (default: {DEFAULT_REPORT_DIR}).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    dlq_dir: Path = args.dlq_dir
    report_dir: Path = args.report_dir

    logger.info("Scanning DLQ directory: %s", dlq_dir)
    entries = scan_dlq(dlq_dir)
    report = aggregate(entries)

    if args.filter != "all":
        filtered = AuditReport(
            total=report.total,
            by_status=report.by_status,
            by_date=report.by_date,
            by_group=report.by_group,
            by_error_class=report.by_error_class,
            oldest_real=report.oldest_real,
            entries=[e for e in report.entries if e.status == args.filter],
        )
    else:
        filtered = report

    text = format_report(filtered, dlq_dir)
    print(text)

    if report.total > 0:
        try:
            saved = save_report(text, report_dir)
            print(f"\nReport saved to: {saved}")
        except OSError as exc:
            logger.warning("Could not save report file: %s", exc)

    if args.execute and report.total > 0:
        moved = execute_archive(report, dlq_dir)
        print(
            f"\nExecuted: moved synthetic={moved['synthetic']}, "
            f"corrupt={moved['corrupt']}, skipped={moved['skipped']}"
        )
        real_count = report.by_status.get("real", 0)
        if real_count:
            print(
                f"\n{real_count} real entries identified for retry — run "
                "`python scripts/whatsapp_catchup_now.py` or equivalent to "
                "actually re-send. This script does NOT trigger re-send."
            )
    elif args.execute:
        print("\n(--execute given, but DLQ is empty — nothing to move.)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
